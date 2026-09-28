"""WS2: HTTP-level end-to-end smoke test (SPEC 13.4)."""
import hashlib
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "tests"))

try:
    from fixtures import loader  # WS1: copies fixtures to a temp dir
except ImportError:  # WS1 modules not importable yet
    loader = None

S_MAIN = "claude:s-main"
A1, A2, A3, A4 = ("%s:a%s" % (S_MAIN, c * 16) for c in "1234")
APPENDED = "Appended by the smoke test"


def snapshot(root):
    """{path: (sha256, mtime_ns)} of every file under root."""
    out = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            out[path] = (digest, os.stat(path).st_mtime_ns)
    return out


class SseReader:
    """Collects SSE messages from a streaming response on a background thread."""

    def __init__(self, port, query):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.conn.request("GET", "/api/stream?" + query, headers={"Host": "127.0.0.1:%d" % port})
        self.resp = self.conn.getresponse()
        self.messages = []
        self.lock = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        fields = {}
        try:
            for raw in self.resp.fp:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith(":"):
                    continue
                if line == "":
                    if "event" in fields:
                        fields["data"] = json.loads(fields.get("data", "null"))
                        with self.lock:
                            self.messages.append(fields)
                            self.lock.notify_all()
                    fields = {}
                    continue
                name, _, value = line.partition(": ")
                fields[name] = value
        except (OSError, ValueError):
            pass

    def wait_for(self, predicate, timeout):
        deadline = time.time() + timeout
        with self.lock:
            while True:
                hits = [m for m in self.messages if predicate(m)]
                if hits or time.time() >= deadline:
                    return hits
                self.lock.wait(max(0.0, deadline - time.time()))

    def close(self):
        self.conn.close()


@unittest.skipIf(loader is None, "WS1 fixture loader not available")
class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = loader.copy_fixtures()
        cls.before = snapshot(cls.root)
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "agent_viewer", "--port", "0",
             "--claude-root", os.path.join(cls.root, "claude"),
             "--omp-root", os.path.join(cls.root, "omp")],
            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first = cls.proc.stdout.readline().decode("utf-8").strip()
        prefix = "listening on http://127.0.0.1:"
        if not first.startswith(prefix):
            cls.proc.kill()
            raise AssertionError("unexpected first line %r, stderr: %s"
                                 % (first, cls.proc.stderr.read().decode("utf-8", "replace")))
        cls.port = int(first[len(prefix):])

    @classmethod
    def tearDownClass(cls):
        if cls.proc.poll() is None:
            cls.proc.kill()
            cls.proc.wait(5)
        cls.proc.stdout.close()
        cls.proc.stderr.close()
        loader.remove_fixtures(cls.root)

    # ------------------------------------------------------------ helpers

    def request(self, method, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, headers={"Host": host or "127.0.0.1:%d" % self.port})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, (json.loads(body) if body else None)

    def get(self, path):
        status, body = self.request("GET", path)
        self.assertEqual(status, 200, "%s -> %s %s" % (path, status, body))
        return body

    def wait_until(self, fn, timeout, what):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = fn()
                if last:
                    return last
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        self.fail("timed out waiting for %s (last: %r)" % (what, last))

    def stats(self, source, sid, agent, scope="agent"):
        return self.get("/api/agents/%s/%s/%s/stats?scope=%s" % (source, sid, agent, scope))

    # --------------------------------------------------------------- test

    def test_smoke(self):
        # Step 3: health within 5 s, indexing drained within 10 s.
        self.wait_until(lambda: self.request("GET", "/api/health")[1].get("ok"), 5, "health")
        self.wait_until(lambda: self.get("/api/health")["indexing"]["queued"] == 0, 10, "indexing")
        self.wait_until(lambda: all(s["indexed"] for s in self.get("/api/sessions")["items"]),
                        10, "all sessions indexed")

        # Step 4: sessions, trees and stats of section 13.2.
        sessions = {s["key"]: s for s in self.get("/api/sessions")["items"]}
        self.assertEqual(set(sessions), {S_MAIN, "claude:s-broken", "omp:o-root"})
        self.check_claude_demo(sessions[S_MAIN])
        self.check_broken()
        self.check_omp(sessions["omp:o-root"])
        self.check_graph()

        # Step 5: live append in two writes produces exactly one events.append.
        self.check_live_append()
        self.check_graph_live_update()

        # Step 6: host check and method check.
        self.assertEqual(self.request("GET", "/api/health", host="evil.example:%d" % self.port)[0], 403)
        self.assertEqual(self.request("POST", "/api/sessions")[0], 405)

        # Step 7: stop, then nothing but the appended file changed.
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(10), 0)
        after = snapshot(self.root)
        appended = os.path.join(self.root, "claude", "-tmp-demo", "s-main", "subagents",
                                "agent-a2222222222222222.jsonl")
        self.assertEqual(set(after), set(self.before))
        for path, value in self.before.items():
            if path != appended:
                self.assertEqual(after[path], value, "server modified %s" % path)

    def check_claude_demo(self, summary):
        self.assertEqual(summary["title"], "Demo session")
        self.assertEqual(summary["gitBranch"], "feature/DEMO-1")
        self.assertEqual(summary["agentCount"], 5)
        tree = self.get("/api/sessions/claude/s-main/tree")
        nodes = tree["nodes"]
        root = nodes[tree["rootKey"]]
        self.assertEqual(tree["rootKey"], S_MAIN + ":main")
        self.assertEqual(root["children"], [A1, A3, A4])
        self.assertEqual(nodes[A1]["children"], [A2])
        self.assertEqual((nodes[A1]["linkedBy"], len(nodes[A1]["resumes"]), nodes[A1]["status"]),
                         ("meta", 1, "finished"))
        self.assertEqual((nodes[A2]["linkedBy"], nodes[A2]["depth"]), ("meta", 2))
        self.assertEqual((nodes[A3]["linkedBy"], nodes[A3]["agentType"]),
                         ("transcript", "ecc:code-explorer"))
        self.assertIn(nodes[A3]["status"], ("running", "stale"))
        self.assertEqual((nodes[A4]["linkedBy"], nodes[A4]["agentType"]), ("orphan", None))
        self.assertIn("orphan", nodes[A4]["warnings"])

        main = self.stats("claude", "s-main", "main")
        self.assertEqual({k: main["tokens"][k] for k in ("input", "output", "cacheRead", "cacheCreation")},
                         {"input": 25, "output": 110, "cacheRead": 4900, "cacheCreation": 200})
        self.assertEqual({k: v["calls"] for k, v in main["tools"].items()},
                         {"Agent": 2, "Skill": 1, "SendMessage": 1})
        self.assertEqual(main["skills"]["code-review"]["via"]["tool"], 1)
        self.assertEqual(main["slashCommands"], {"model": 1})
        self.assertNotIn("model", main["skills"])
        self.assertEqual(main["parse"]["unknownTypes"], {"brand-new-type": 1})
        self.assertEqual(main["activeDurationMs"], 32000)

        leaf = self.stats("claude", "s-main", "a2222222222222222")
        self.assertEqual((leaf["tools"]["Bash"]["calls"], leaf["tools"]["Bash"]["errors"]), (1, 1))
        events = self.get("/api/agents/claude/s-main/main/events?kinds=thinking")["items"]
        self.assertTrue(events and events[0]["redacted"])

    def check_broken(self):
        stats = self.stats("claude", "s-broken", "main")
        self.assertEqual((stats["parse"]["lines"], stats["parse"]["skipped"]), (6, 3))
        self.assertEqual(stats["errors"]["apiErrors"], 1)
        self.assertNotIn("<synthetic>", stats["byModel"])

    def check_omp(self, summary):
        self.assertIsNone(summary["gitBranch"])
        self.assertEqual(summary["status"], "finished")
        tree = self.get("/api/sessions/omp/o-root/tree")
        child = tree["nodes"]["omp:o-root:Checker"]
        self.assertEqual((child["agentType"], child["linkedBy"], child["spawnToolCallId"], child["status"]),
                         ("reviewer", "parentSession", "call_2|y", "finished"))
        root = self.stats("omp", "o-root", "main")
        self.assertEqual(root["skills"]["jira-integration"]["via"]["read"], 1)
        self.assertEqual({k: v["calls"] for k, v in root["tools"].items()}, {"read": 1, "task": 1})
        sub = self.stats("omp", "o-root", "main", scope="subtree")
        self.assertEqual({k: sub["tokens"][k] for k in ("input", "output", "cacheRead", "cacheCreation")},
                         {"input": 9, "output": 204, "cacheRead": 19726, "cacheCreation": 19726})
        self.assertAlmostEqual(sub["costUsd"], 0.1)
        self.assertEqual(self.stats("omp", "o-root", "Checker")["errors"]["aborted"], 1)

    def check_graph(self):
        """Graph endpoint equals contract/examples/graph.json except rev and volatile fields."""
        with open(os.path.join(REPO, "contract", "examples", "graph.json"), encoding="utf-8") as fh:
            example = json.load(fh)
        got = self.get("/api/sessions/claude/s-main/graph")
        self.assertIsInstance(got["rev"], int)
        volatile = ("status", "lastActivityAt")

        def strip(graph):
            nodes = [{k: v for k, v in n.items() if k not in volatile} for n in graph["nodes"]]
            rest = {k: v for k, v in graph.items() if k not in ("rev", "nodes")}
            return dict(rest, nodes=nodes)
        self.assertEqual(strip(got), strip(example))
        filtered = self.get("/api/agents/claude/s-main/a2222222222222222/events?toolName=Bash")
        self.assertEqual([e["seq"] for e in filtered["items"]], [1, 2])

    def check_graph_live_update(self):
        """A graph=1 client gets one graph.update for a new tool call; a tree client gets none."""
        graph_reader = SseReader(self.port, "session=%s&graph=1" % S_MAIN)
        tree_reader = SseReader(self.port, "session=" + S_MAIN)
        try:
            for reader in (graph_reader, tree_reader):
                self.assertTrue(reader.wait_for(lambda m: m["event"] == "hello", 3))
            path = os.path.join(self.root, "claude", "-tmp-demo", "s-main", "subagents",
                                "agent-a2222222222222222.jsonl")
            line = json.dumps({
                "type": "assistant", "uuid": "d6", "parentUuid": "d5", "isSidechain": True,
                "agentId": "a2222222222222222", "sessionId": "s-main",
                "timestamp": "2026-09-25T10:00:10.000Z",
                "message": {"id": "msg_d10", "model": "claude-haiku-4-5-20251001",
                            "stop_reason": "tool_use",
                            "content": [{"type": "tool_use", "id": "toolu_SMOKE", "name": "Grep",
                                         "input": {"pattern": "x"}}],
                            "usage": {"input_tokens": 1, "output_tokens": 1}}}).encode("utf-8")
            with open(path, "ab") as fh:  # the test, not the server, writes the fixture copy
                fh.write(line + b"\n")

            def grep_node(m):
                if m["event"] != "graph.update" or not m["data"]["graph"]:
                    return None
                wanted = "tool|%s|Grep" % A2
                return next((n for n in m["data"]["graph"]["nodes"] if n["id"] == wanted), None)
            hits = graph_reader.wait_for(lambda m: grep_node(m) is not None, 3)
            self.assertEqual(len(hits), 1, graph_reader.messages)
            self.assertEqual(grep_node(hits[0])["pending"], 1)
            self.assertEqual(hits[0]["data"]["rev"], hits[0]["data"]["graph"]["rev"])
            time.sleep(1.5)
            updates = [m for m in graph_reader.messages if m["event"] == "graph.update"]
            self.assertEqual(len(updates), 1)
            self.assertEqual([m for m in tree_reader.messages if m["event"] == "graph.update"], [])
        finally:
            graph_reader.close()
            tree_reader.close()

    def check_live_append(self):
        reader = SseReader(self.port, "session=" + S_MAIN)
        try:
            self.assertTrue(reader.wait_for(lambda m: m["event"] == "hello", 3))
            path = os.path.join(self.root, "claude", "-tmp-demo", "s-main", "subagents",
                                "agent-a2222222222222222.jsonl")
            line = json.dumps({
                "type": "assistant", "uuid": "d5", "parentUuid": "d4", "isSidechain": True,
                "agentId": "a2222222222222222", "sessionId": "s-main",
                "timestamp": "2026-09-25T10:00:09.000Z",
                "message": {"id": "msg_d9", "model": "claude-haiku-4-5-20251001",
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": APPENDED}],
                            "usage": {"input_tokens": 1, "output_tokens": 1}}}).encode("utf-8")
            half = len(line) // 2
            with open(path, "ab") as fh:  # the test, not the server, writes the fixture copy
                fh.write(line[:half])
            time.sleep(0.3)
            with open(path, "ab") as fh:
                fh.write(line[half:] + b"\n")

            def is_append(m):
                return m["event"] == "events.append" and m["data"]["agentKey"] == A2
            hits = reader.wait_for(is_append, 3)
            self.assertEqual(len(hits), 1)
            texts = [e["preview"] for e in hits[0]["data"]["events"]]
            self.assertIn(APPENDED, texts)
            time.sleep(1.5)
            self.assertEqual(len([m for m in reader.messages if is_append(m)]), 1)
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
