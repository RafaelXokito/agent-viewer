"""WS2: performance budgets of SPEC 12, on a generated 30 MB fixture.

Excluded from the default run; enable with AGENT_VIEWER_PERF=1.
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGET_BYTES = 30 * 1024 * 1024
ENABLED = os.environ.get("AGENT_VIEWER_PERF") == "1"
SID = "perf-session"
PROJECT = "-tmp-perf"


def generate(path, target=TARGET_BYTES):
    """A realistic main transcript: prompts, split assistant messages, tool results."""
    n = 0
    with open(path, "wb") as fh:
        size = 0
        while size < target:
            n += 1
            ts = "2026-09-25T%02d:%02d:%02d.000Z" % ((n // 3600) % 24, (n // 60) % 60, n % 60)
            base = {"sessionId": SID, "isSidechain": False, "cwd": "/tmp/perf",
                    "gitBranch": "feature/PERF-1", "timestamp": ts}
            usage = {"input_tokens": 3, "output_tokens": 40, "cache_read_input_tokens": 9000,
                     "cache_creation_input_tokens": 120}
            records = [
                dict(base, type="user", uuid="u%d" % n, parentUuid=None,
                     message={"role": "user", "content": "Step %d: " % n + "please do the thing " * 20}),
                dict(base, type="assistant", uuid="a%d" % n, parentUuid="u%d" % n,
                     message={"id": "msg_%d" % n, "model": "claude-opus-5-5", "stop_reason": "tool_use",
                              "content": [{"type": "text", "text": "Working on it. " * 30}], "usage": usage}),
                dict(base, type="assistant", uuid="b%d" % n, parentUuid="a%d" % n,
                     message={"id": "msg_%d" % n, "model": "claude-opus-5-5", "stop_reason": "tool_use",
                              "content": [{"type": "tool_use", "id": "toolu_%d" % n, "name": "Bash",
                                           "input": {"command": "ls -la /tmp/%d" % n, "description": "list"}}],
                              "usage": usage}),
                dict(base, type="user", uuid="r%d" % n, parentUuid="b%d" % n,
                     message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_%d" % n,
                                                           "content": "file.txt\n" * 150}]}),
            ]
            for record in records:
                line = (json.dumps(record) + "\n").encode("utf-8")
                fh.write(line)
                size += len(line)
    return n


@unittest.skipUnless(ENABLED, "set AGENT_VIEWER_PERF=1 to run the performance budgets")
class IndexBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="av-perf-")
        cls.claude = os.path.join(cls.root, "claude")
        os.makedirs(os.path.join(cls.claude, PROJECT))
        cls.omp = os.path.join(cls.root, "omp")
        os.makedirs(cls.omp)
        cls.path = os.path.join(cls.claude, PROJECT, SID + ".jsonl")
        generate(cls.path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root)

    def test_index_time_memory_and_page_latency(self):
        # Run in a child process so the RSS figure belongs to this file only.
        code = r"""
import json, sys, time
sys.path.insert(0, %(repo)r)
from agent_viewer.api import Api
from agent_viewer.store import Store, StoreConfig
def rss_mb():
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
before = rss_mb()
store = Store(StoreConfig(claude_root=%(claude)r, omp_root=%(omp)r))
t0 = time.perf_counter()
store.scan()
while store.index_step(None):
    pass
store.refresh()
index_s = time.perf_counter() - t0
rss = rss_mb() - before
api = Api(store)
path = "/api/agents/claude/%(sid)s/main/events"
status, page = api.handle(path, "limit=200")
t0 = time.perf_counter()
for _ in range(10):
    api.handle(path, "limit=200")
last_ms = (time.perf_counter() - t0) * 100
middle = page["fromSeq"] // 2
t0 = time.perf_counter()
for _ in range(10):
    api.handle(path, "limit=200&before=%%d" %% middle)
middle_ms = (time.perf_counter() - t0) * 100
print(json.dumps({"index_s": index_s, "rss_mb": rss, "last_ms": last_ms, "middle_ms": middle_ms,
                  "status": status, "items": len(page["items"])}))
""" % {"repo": REPO, "claude": self.claude, "omp": self.omp, "sid": SID}
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, timeout=300)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        result = json.loads(out.stdout.decode().strip().splitlines()[-1])
        sys.stderr.write("\nperf: %s\n" % result)
        self.assertEqual((result["status"], result["items"]), (200, 200))
        self.assertLess(result["index_s"], 3.0, "full index of 30 MB")
        self.assertLess(result["rss_mb"], 150.0, "resident memory for the file")
        self.assertLess(result["last_ms"], 50.0, "last events page of 200")
        self.assertLess(result["middle_ms"], 50.0, "middle events page of 200")

    def test_append_visible_end_to_end_within_2s(self):
        proc = subprocess.Popen([sys.executable, "-m", "agent_viewer", "--port", "0",
                                 "--claude-root", self.claude, "--omp-root", self.omp],
                                cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            port = int(proc.stdout.readline().decode().strip().rsplit(":", 1)[1])
            host = {"Host": "127.0.0.1:%d" % port}
            deadline = time.time() + 60
            while time.time() < deadline:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/api/health", headers=host)
                health = json.loads(conn.getresponse().read())
                conn.close()
                if health["indexing"]["queued"] == 0:
                    break
                time.sleep(0.2)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/stream?session=claude:" + SID, headers=host)
            resp = conn.getresponse()
            seen = threading.Event()

            def read():
                for raw in resp.fp:
                    if raw.startswith(b"data: ") and b"perf-append-marker" in raw:
                        seen.set()
                        return
            threading.Thread(target=read, daemon=True).start()
            time.sleep(0.5)
            line = json.dumps({"type": "assistant", "uuid": "zz", "parentUuid": None, "sessionId": SID,
                               "timestamp": "2026-09-26T00:00:00.000Z",
                               "message": {"id": "msg_zz", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                                           "content": [{"type": "text", "text": "perf-append-marker"}],
                                           "usage": {"input_tokens": 1, "output_tokens": 1}}})
            t0 = time.time()
            with open(self.path, "ab") as fh:  # the test appends to its own generated file
                fh.write(line.encode() + b"\n")
            self.assertTrue(seen.wait(2.0), "appended line not delivered within 2 s")
            sys.stderr.write("\nperf: append visible after %.0f ms\n" % ((time.time() - t0) * 1000))
            conn.close()
        finally:
            proc.terminate()
            proc.wait(10)
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
