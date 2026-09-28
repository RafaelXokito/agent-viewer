"""WS2: store - incremental apply equals full parse, SSE message generation, coalescing."""
import json
import re
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))

try:
    import loader  # WS1 fixture loader, imports the WS1 modules
    from agent_viewer.store import Store, StoreConfig
except ImportError as exc:  # WS1 not importable yet
    loader = None
    IMPORT_ERROR = exc

from agent_viewer import sse  # noqa: E402

S_MAIN = "claude:s-main"
A2 = S_MAIN + ":a2222222222222222"
A2_PATH = os.path.join("claude", "-tmp-demo", "s-main", "subagents", "agent-a2222222222222222.jsonl")


class Recorder:
    """Publisher that records submissions instead of coalescing them."""

    def __init__(self):
        self.messages = []

    def submit(self, key, event, data, session_key=None, merge=None, min_interval=0.0):
        self.messages.append((event, data, session_key))

    def reset_key(self, key):
        pass

    def of(self, event):
        return [data for name, data, _ in self.messages if name == event]

    def clear(self):
        self.messages = []


def assistant_line(uuid, text, msg_id):
    return (json.dumps({
        "type": "assistant", "uuid": uuid, "parentUuid": "d4", "isSidechain": True,
        "agentId": "a2222222222222222", "sessionId": "s-main",
        "timestamp": "2026-09-25T10:00:09.000Z",
        "message": {"id": msg_id, "model": "claude-haiku-4-5-20251001", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}}}) + "\n").encode("utf-8")


def snapshot_store(store):
    """Everything observable through the store protocol, for equality checks."""
    out = {"sessions": sorted((dict(s, mtime=None) for s in store.sessions()), key=lambda s: s["key"])}
    for summary in out["sessions"]:
        key = summary["key"]
        out[key] = {"tree": store.tree(key), "stats": store.session_stats(key)}
        for agent_key in store.tree(key)["nodes"]:
            page = store.events(agent_key, None, None, 1000, None, True)
            out[agent_key] = {"events": page["items"], "stats": store.agent_stats(agent_key, "agent")}
    return out


def index_all(store):
    store.scan()
    while store.index_step(None):
        pass
    store.refresh()


@unittest.skipIf(loader is None, "WS1 modules not importable: %s" % (None if loader else IMPORT_ERROR))
class StoreTest(unittest.TestCase):
    def setUp(self):
        self.root = loader.copy_fixtures()
        self.recorder = Recorder()
        self.store = self.make_store(self.root, self.recorder)

    def tearDown(self):
        loader.remove_fixtures(self.root)

    @staticmethod
    def make_store(root, publisher=None, **kw):
        config = StoreConfig(claude_root=os.path.join(root, "claude"),
                             omp_root=os.path.join(root, "omp"), **kw)
        return Store(config, publisher)

    def append(self, rel, data):
        with open(os.path.join(self.root, rel), "ab") as fh:  # test writes its own temp copy
            fh.write(data)

    # ------------------------------------------------------------ indexing

    def test_quick_summary_before_full_index(self):
        self.store.scan()
        self.store.refresh()
        summary = self.store.session(S_MAIN)
        self.assertFalse(summary["indexed"])
        self.assertEqual(summary["title"], "Demo session")
        self.assertEqual(summary["cwd"], "/tmp/demo")
        self.assertIsNone(summary["tokens"])
        self.assertIsNone(self.store.session_stats(S_MAIN))
        self.assertEqual(self.store.health()["indexing"]["done"], 0)

    def test_full_index_matches_fixture_expectations(self):
        index_all(self.store)
        health = self.store.health()
        self.assertEqual(health["indexing"]["queued"], 0)
        self.assertEqual(health["indexing"]["done"], 8)
        keys = sorted(s["key"] for s in self.store.sessions())
        self.assertEqual(keys, ["claude:s-broken", S_MAIN, "omp:o-root"])
        main = self.store.session(S_MAIN)
        self.assertTrue(main["indexed"])
        self.assertEqual((main["agentCount"], main["gitBranch"]), (5, "feature/DEMO-1"))
        self.assertEqual(main["tokens"]["input"], 25 + 9 + 4 + 1)
        tree = self.store.tree(S_MAIN)
        self.assertEqual(tree["nodes"][S_MAIN + ":main"]["children"],
                         [S_MAIN + ":a1111111111111111", S_MAIN + ":a3333333333333333",
                          S_MAIN + ":a4444444444444444"])
        omp = self.store.session("omp:o-root")
        self.assertEqual((omp["status"], omp["gitBranch"], omp["title"]), ("finished", None, "OMP demo"))

    def test_incremental_apply_equals_full_parse(self):
        index_all(self.store)
        expected = snapshot_store(self.store)

        # Rebuild every file byte-range by byte-range, polling in between.
        grow_root = tempfile.mkdtemp(prefix="av-grow-")
        try:
            sources = []
            for dirpath, _, files in os.walk(self.root):
                for name in files:
                    src = os.path.join(dirpath, name)
                    dst = os.path.join(grow_root, os.path.relpath(src, self.root))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(src, "rb") as fh:
                        data = fh.read()
                    if name.endswith(".jsonl"):
                        data = data.replace(self.root.encode(), grow_root.encode())
                    sources.append((dst, data))
            # Sidecars and the first bytes of each transcript exist from the start.
            for dst, data in sources:
                with open(dst, "wb") as fh:
                    fh.write(data if not dst.endswith(".jsonl") else data[:1])
            grow = self.make_store(grow_root)
            index_all(grow)
            step = 37
            for start in range(1, max(len(d) for _, d in sources), step):
                for dst, data in sources:
                    if dst.endswith(".jsonl") and start < len(data):
                        with open(dst, "ab") as fh:
                            fh.write(data[start:start + step])
                grow.poll(hot_only=False)
                grow.refresh()
            grow.refresh(force=True)
            got = snapshot_store(grow)
        finally:
            shutil.rmtree(grow_root)

        def normalize(value):
            text = json.dumps(value, sort_keys=True).replace(grow_root, "<root>")
            text = text.replace(self.root, "<root>")
            # Statuses depend on file mtimes, which differ between the two copies.
            text = re.sub(r'"running": \d+', '"running": 0', text)
            return json.loads(text)
        expected, got = normalize(expected), normalize(got)
        for key in expected:
            if isinstance(expected[key], dict) and "tree" in expected[key]:
                for node in list(expected[key]["tree"]["nodes"].values()) + list(got[key]["tree"]["nodes"].values()):
                    node.pop("status", None)  # mtimes differ between the two copies
            if key == "sessions":
                for s in expected[key] + got[key]:
                    s.pop("status", None)
                    s.pop("runningAgents", None)
        self.assertEqual(got, expected)

    # ---------------------------------------------------------- change feed

    def test_append_publishes_one_events_append(self):
        index_all(self.store)
        self.recorder.clear()
        line = assistant_line("d5", "fresh text", "msg_new")
        self.append(A2_PATH, line[:40])
        self.store.poll(hot_only=False)
        self.assertEqual(self.recorder.of("events.append"), [], "partial line must be held back")
        self.append(A2_PATH, line[40:])
        self.store.poll(hot_only=False)
        self.store.refresh()
        appends = self.recorder.of("events.append")
        self.assertEqual(len(appends), 1)
        self.assertEqual(appends[0]["agentKey"], A2)
        self.assertEqual([e["preview"] for e in appends[0]["events"]], ["fresh text"])
        self.assertEqual(appends[0]["fromSeq"], 4)
        self.assertNotIn("ref", appends[0]["events"][0])
        scoped = [s for name, _, s in self.recorder.messages if name == "events.append"]
        self.assertEqual(scoped, [S_MAIN])
        self.assertTrue(self.recorder.of("session.upsert"))
        stats = [d for d in self.recorder.of("stats.update") if d["key"] == A2]
        self.assertEqual(stats[-1]["stats"]["tokens"]["output"], 6 + 1)

    def test_files_of_watched_sessions_are_hot_even_when_old_and_finished(self):
        watched = set()
        store = Store(StoreConfig(claude_root=os.path.join(self.root, "claude"),
                                  omp_root=os.path.join(self.root, "omp")),
                      self.recorder, watched_sessions=lambda: watched)
        old = time.time() - 3600
        for dirpath, _, files in os.walk(self.root):
            for name in files:
                os.utime(os.path.join(dirpath, name), (old, old))  # age the temp copy
        index_all(store)
        self.assertEqual(store.agents[A2].status, "finished")
        self.recorder.clear()
        self.append(A2_PATH, assistant_line("d5", "late", "m-late"))
        os.utime(os.path.join(self.root, A2_PATH), (old, old))
        store.poll(hot_only=True)
        self.assertEqual(self.recorder.of("events.append"), [], "cold, unwatched: waits for the scan")
        watched.add(S_MAIN)
        store.poll(hot_only=True)
        self.assertEqual([e["preview"] for d in self.recorder.of("events.append") for e in d["events"]],
                         ["late"])

    def test_initial_index_does_not_flood_events_append(self):
        index_all(self.store)
        self.assertEqual(self.recorder.of("events.append"), [])
        self.assertEqual(len(self.recorder.of("session.upsert")), 3)

    def test_truncation_resets_and_reindexes(self):
        index_all(self.store)
        self.recorder.clear()
        path = os.path.join(self.root, A2_PATH)
        with open(path, "rb") as fh:
            first = fh.readline()
        with open(path, "wb") as fh:  # test-only rewrite of the temp copy
            fh.write(first)
        self.store.poll(hot_only=False)
        self.store.refresh()
        self.assertEqual(self.recorder.of("agent.reset"), [{"agentKey": A2}])
        page = self.store.events(A2, None, None, 100, None, True)
        self.assertEqual([e["kind"] for e in page["items"]], ["prompt"])
        self.assertEqual(self.store.agent_stats(A2, "agent")["tools"], {})

    def test_deleted_file_keeps_last_content_and_server_keeps_going(self):
        index_all(self.store)
        os.remove(os.path.join(self.root, A2_PATH))
        self.store.scan()
        self.store.poll(hot_only=False)
        self.store.refresh()
        page = self.store.events(A2, None, None, 100, None, True)
        self.assertEqual(len(page["items"]), 4)
        self.assertTrue(self.store.agent(A2)["file"].endswith("agent-a2222222222222222.jsonl"))

    def test_garbage_is_counted_not_fatal(self):
        index_all(self.store)
        self.append(A2_PATH, b"\x00\xffnot json\n[1]\n")
        self.store.poll(hot_only=False)
        self.store.refresh()
        self.assertEqual(self.store.agent_stats(A2, "agent")["parse"]["skipped"], 2)

    def test_prioritize_moves_session_to_front(self):
        self.store.scan()
        self.store.prioritize({"omp:o-root"})
        self.assertTrue(self.store.queue[0].startswith("omp:o-root:"))

    def test_event_full_reads_the_source_line(self):
        index_all(self.store)
        body = self.store.event_full(A2, 2)
        self.assertEqual((body["seq"], body["kind"], body["isError"]), (2, "tool_result", True))
        self.assertIn("Exit code 1", body["content"])
        self.assertIsNone(self.store.event_full(A2, 999))

    def test_coalescing_through_real_hub(self):
        hub = sse.Hub()
        client = hub.subscribe({S_MAIN})
        now = [1000.0]
        coalescer = sse.Coalescer(hub, clock=lambda: now[0])
        store = self.make_store(self.root, coalescer)
        index_all(store)
        coalescer.flush(force=True)
        while client.next(0) is not None:
            pass
        self.append(A2_PATH, assistant_line("d5", "one", "m1"))
        store.poll(hot_only=False)
        self.append(A2_PATH, assistant_line("d6", "two", "m2"))
        store.poll(hot_only=False)
        store.refresh()
        now[0] += 0.3
        coalescer.flush()
        appends = []
        while True:
            message = client.next(0)
            if message is None:
                break
            if b"event: events.append" in message:
                appends.append(message)
        self.assertEqual(len(appends), 1)
        self.assertIn(b'"fromSeq":4,"toSeq":5', appends[0])


MAIN_PATH = os.path.join("claude", "-tmp-demo", "s-main.jsonl")
A3 = S_MAIN + ":a3333333333333333"


def tool_use_line(tool_id, name, ts="2026-09-25T10:00:09.000Z"):
    return (json.dumps({
        "type": "assistant", "uuid": "g-" + tool_id, "parentUuid": "d4", "isSidechain": True,
        "agentId": "a2222222222222222", "sessionId": "s-main", "timestamp": ts,
        "message": {"id": "msg-" + tool_id, "model": "claude-haiku-4-5-20251001",
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}}}) + "\n").encode("utf-8")


@unittest.skipIf(loader is None, "WS1 modules not importable")
class StoreGraphTest(unittest.TestCase):
    """graph(key), rev and graph.update publishing (docs/FEATURE-graph-canvas.md 8, G-PERF-4)."""

    def setUp(self):
        self.root = loader.copy_fixtures()
        self.recorder = Recorder()
        self.graph_watch = set()
        self.now = time.time()
        self.store = Store(StoreConfig(claude_root=os.path.join(self.root, "claude"),
                                       omp_root=os.path.join(self.root, "omp")),
                           self.recorder, clock=lambda: self.now,
                           watched_graph_sessions=lambda: set(self.graph_watch))

    def tearDown(self):
        loader.remove_fixtures(self.root)

    def append(self, rel, data):
        with open(os.path.join(self.root, rel), "ab") as fh:  # test writes its own temp copy
            fh.write(data)

    def graph_updates(self, session_key=S_MAIN):
        return [d for name, d, key in self.recorder.messages
                if name == "graph.update" and key == session_key]

    def expected_graph(self, key):
        from agent_viewer import graph as graph_mod
        store = self.store
        summaries = {s["key"]: s for s in store.sessions()}
        usage = {}
        for agent_key in store.tree(key)["nodes"]:
            state = store.agents[agent_key].state
            stats = state.stats()
            usage[agent_key] = {"tools": stats.tools, "skills": stats.skills, "errors": stats.errors,
                                "pending": state.pending_by_tool()}
        return graph_mod.build_graph(key, store.tree(key), summaries, usage)

    def test_graph_equals_build_graph_of_the_store_state(self):
        index_all(self.store)
        got = self.store.graph(S_MAIN)
        self.assertEqual(got["rev"], 1)
        self.assertEqual({k: v for k, v in got.items() if k != "rev"}, self.expected_graph(S_MAIN))
        self.assertIsNone(self.store.graph("claude:nope"))

    def test_graph_matches_the_example_except_volatile_fields(self):
        index_all(self.store)
        got = self.store.graph(S_MAIN)
        example = load_example_graph()
        strip = lambda g: [{k: v for k, v in n.items() if k not in ("status", "lastActivityAt")}  # noqa: E731
                           for n in g["nodes"]]
        self.assertEqual(strip(got), strip(example))
        self.assertEqual(got["edges"], example["edges"])

    def test_rev_increments_only_on_change(self):
        index_all(self.store)
        self.assertEqual(self.store.graph(S_MAIN)["rev"], 1)
        self.assertEqual(self.store.graph(S_MAIN)["rev"], 1)
        self.append(A2_PATH, tool_use_line("toolu_G1", "Grep"))
        self.store.poll(hot_only=False)
        self.store.refresh(now=self.now)
        self.assertEqual(self.store.graph(S_MAIN)["rev"], 2)
        self.assertEqual(self.store.graph(S_MAIN)["rev"], 2)

    def test_no_graph_is_built_without_a_graph_subscriber(self):
        from unittest import mock
        from agent_viewer import graph as graph_mod
        with mock.patch.object(graph_mod, "build_graph", wraps=graph_mod.build_graph) as spy:
            index_all(self.store)
            self.append(A2_PATH, tool_use_line("toolu_G1", "Grep"))
            self.store.poll(hot_only=False)
            self.store.refresh(now=self.now)
            self.assertEqual(spy.call_count, 0)
            self.assertEqual(self.graph_updates(), [])
            self.graph_watch.add(S_MAIN)
            self.store.refresh(now=self.now, force=True)
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(self.graph_updates("omp:o-root"), [])

    def test_new_tool_call_publishes_one_graph_update(self):
        self.graph_watch.add(S_MAIN)
        index_all(self.store)
        self.store.refresh(now=self.now)
        self.recorder.clear()
        self.append(A2_PATH, tool_use_line("toolu_G1", "Grep"))
        self.store.poll(hot_only=False)
        self.store.refresh(now=self.now)
        updates = self.graph_updates()
        self.assertEqual(len(updates), 1)
        data = updates[0]
        self.assertEqual(data["rev"], data["graph"]["rev"])
        node = [n for n in data["graph"]["nodes"] if n["id"] == "tool|%s|Grep" % A2]
        self.assertEqual([(n["calls"], n["pending"]) for n in node], [(1, 1)])
        self.store.refresh(now=self.now, force=True)
        self.assertEqual(len(self.graph_updates()), 1, "nothing changed, nothing published")

    def test_status_change_publishes_a_graph_update(self):
        self.graph_watch.add(S_MAIN)
        index_all(self.store)
        self.append(A2_PATH, tool_use_line("toolu_G1", "Grep"))
        self.store.poll(hot_only=False)
        self.store.refresh(now=self.now)
        status = lambda g: next(n["status"] for n in g["nodes"] if n["id"] == A2)  # noqa: E731
        before = status(self.store.graph(S_MAIN))
        self.recorder.clear()
        self.store.refresh(now=self.now + 7 * 24 * 3600)  # only time moves
        updates = self.graph_updates()
        self.assertEqual(len(updates), 1)
        self.assertNotEqual(status(updates[0]["graph"]), before)

    def test_new_agent_file_publishes_a_graph_update(self):
        self.graph_watch.add(S_MAIN)
        index_all(self.store)
        self.store.refresh(now=self.now)
        self.recorder.clear()
        rel = os.path.join("claude", "-tmp-demo", "s-main", "subagents", "agent-a5555555555555555.jsonl")
        line = json.dumps({"type": "user", "uuid": "n1", "parentUuid": None, "isSidechain": True,
                           "agentId": "a5555555555555555", "sessionId": "s-main",
                           "timestamp": "2026-09-25T10:02:00.000Z",
                           "message": {"role": "user", "content": "New"}}) + "\n"
        with open(os.path.join(self.root, rel), "w") as fh:  # temp copy only
            fh.write(line)
        self.store.scan()
        while self.store.index_step(None):
            pass
        self.store.refresh(now=self.now)
        updates = self.graph_updates()
        self.assertEqual(len(updates), 1)
        ids = [n["id"] for n in updates[0]["graph"]["nodes"]]
        self.assertIn(S_MAIN + ":a5555555555555555", ids)

    def test_chain_neighbour_summary_change_refreshes_the_graph(self):
        record = {"type": "continued-in", "sessionId": "s-main", "continuedInSessionId": "s-broken",
                  "timestamp": "2026-09-25T10:00:40.000Z"}
        self.append(MAIN_PATH, (json.dumps(record) + "\n").encode("utf-8"))
        self.graph_watch.add("claude:s-broken")
        index_all(self.store)
        self.store.refresh(now=self.now)
        graph = self.store.graph("claude:s-broken")
        pred = [n for n in graph["nodes"] if n["kind"] == "session"]
        self.assertEqual([(n["sessionKey"], n["relation"]) for n in pred], [(S_MAIN, "predecessor")])
        self.recorder.clear()
        later = dict(json.loads(assistant_line("m9", "later", "msg_m9")), isSidechain=False)
        later.pop("agentId")
        later["timestamp"] = "2026-09-25T11:00:00.000Z"
        self.append(MAIN_PATH, (json.dumps(later) + "\n").encode("utf-8"))
        self.store.poll(hot_only=False)
        self.store.refresh(now=self.now)
        updates = self.graph_updates("claude:s-broken")
        self.assertEqual(len(updates), 1)
        node = [n for n in updates[0]["graph"]["nodes"] if n["kind"] == "session"][0]
        self.assertEqual(node["lastActivityAt"], "2026-09-25T11:00:00.000Z")

    def test_overflow_payload_for_huge_graphs(self):
        from unittest import mock
        self.graph_watch.add(S_MAIN)
        with mock.patch.object(sse, "MAX_INLINE_GRAPH_BYTES", 10):
            index_all(self.store)
        data = self.graph_updates()[-1]
        self.assertEqual((data["graph"], data["overflow"]), (None, True))
        self.assertEqual(data["rev"], self.store.graph(S_MAIN)["rev"])

    def test_tool_name_passes_through_events(self):
        index_all(self.store)
        page = self.store.events(A2, None, None, 100, None, False, tool_name="Bash")
        self.assertEqual([e["seq"] for e in page["items"]], [1, 2])
        self.assertEqual(page["total"], 4)

    def test_graph_update_through_the_real_hub_is_opt_in(self):
        hub = sse.Hub()
        graph_client = hub.subscribe({S_MAIN}, graph=True)
        tree_client = hub.subscribe({S_MAIN})
        now = [1000.0]
        coalescer = sse.Coalescer(hub, clock=lambda: now[0])
        store = Store(StoreConfig(claude_root=os.path.join(self.root, "claude"),
                                  omp_root=os.path.join(self.root, "omp")),
                      coalescer, watched_graph_sessions=hub.watched_graph_sessions)
        index_all(store)
        coalescer.flush(force=True)
        drain = lambda c: [m for m in iter(lambda: c.next(0), None)]  # noqa: E731
        self.assertEqual(sum(b"event: graph.update" in m for m in drain(graph_client)), 1)
        self.assertEqual(sum(b"event: graph.update" in m for m in drain(tree_client)), 0)


def load_example_graph():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "contract", "examples", "graph.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    unittest.main()
