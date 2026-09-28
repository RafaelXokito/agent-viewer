"""WS1: Oh My Pi record mapping, usage, skill://, task progress, session_exit, irc:incoming."""
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
import loader  # noqa: E402

from agent_viewer.accumulate import AgentState  # noqa: E402
from agent_viewer.omp_parser import OmpParser  # noqa: E402

ROOT_REL = "omp/-tmp-demo/2026-09-22T09-58-35-301Z_o-root.jsonl"
CHILD_REL = "omp/-tmp-demo/2026-09-22T09-58-35-301Z_o-root/Checker.jsonl"
ROOT_KEY = "omp:o-root:main"
CHILD_KEY = "omp:o-root:Checker"


def state_for(root, rel, key):
    path = os.path.join(root, rel)
    return loader.feed_file(AgentState(key, OmpParser(key), file=path), path)


class OmpFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = loader.copy_fixtures()
        cls.main = state_for(cls.root, ROOT_REL, ROOT_KEY)
        cls.child = state_for(cls.root, CHILD_REL, CHILD_KEY)

    @classmethod
    def tearDownClass(cls):
        loader.remove_fixtures(cls.root)

    def test_event_kinds(self):
        self.assertEqual([e.kind for e in self.main.all_events()], [
            "meta", "meta", "system", "prompt", "thinking", "skill", "tool_call", "meta",
            "tool_result", "tool_result", "text", "system"])
        self.assertEqual([e.kind for e in self.child.all_events()], ["meta", "system", "notification", "error", "system"])

    def test_thinking_text_is_kept(self):
        thinking = [e for e in self.main.all_events() if e.kind == "thinking"][0]
        self.assertEqual(thinking.preview, "Load the skill, then delegate.")
        self.assertFalse(thinking.redacted)

    def test_usage_mapping_and_cost(self):
        u = self.main.usages["p2"]
        self.assertEqual((u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_creation_tokens,
                          u.reasoning_tokens, u.cost_usd, u.duration_ms), (3, 194, 0, 19726, 119, 0.1, 4000))
        stats = self.main.stats()
        self.assertEqual(stats.tokens, {"input": 8, "output": 204, "cacheRead": 19726, "cacheCreation": 19726,
                                        "reasoning": 119, "total": 39664})
        self.assertAlmostEqual(stats.cost_usd, 0.1)
        self.assertEqual(stats.active_duration_ms, 6000)
        self.assertEqual(stats.turns, 1)

    def test_skill_from_read_path(self):
        self.assertEqual([(s.skill, s.via, s.tool_call_id) for s in self.main.skills],
                         [("jira-integration", "read", "call_1|x")])
        self.assertEqual(self.main.stats().skills["jira-integration"]["via"]["read"], 1)

    def test_tools_and_start_time_from_tool_execution_start(self):
        stats = self.main.stats()
        self.assertEqual({k: v["calls"] for k, v in stats.tools.items()}, {"read": 1, "task": 1})
        read = self.main.tool_calls["call_1|x"]
        self.assertEqual((read.intent, read.duration_ms, read.status), ("Reading Jira guidance", 200, "ok"))
        self.assertEqual(stats.tools["read"]["totalDurationMs"], 200)

    def test_task_progress_emits_spawn_link(self):
        links = [l for l in self.main.links if l.kind == "spawn"]
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertEqual((link.from_key, link.to_agent_id, link.tool_call_id), (ROOT_KEY, "Checker", "call_2|y"))
        self.assertEqual(link.evidence, {"agentType": "reviewer", "description": "Delegating check"})
        self.assertEqual(self.main.stats().subagent_types, {"reviewer": 1})
        task_call = [e for e in self.main.all_events() if e.tool_call_id == "call_2|y" and e.kind == "tool_call"][0]
        self.assertEqual(task_call.spawned_agent_key, CHILD_KEY)

    def test_session_exit_signal_and_meta(self):
        self.assertEqual(self.main.status_signals[-1].kind, "session_exit")
        meta = self.main.session_meta
        self.assertEqual((meta["sessionId"], meta["cwd"], meta["title"], meta["version"]), ("o-root", "/tmp/demo", "OMP demo", 3))
        self.assertIsNone(meta.get("gitBranch"))
        self.assertEqual(self.child.session_meta["parentSession"], os.path.join(self.root, ROOT_REL))

    def test_child_aborted_and_irc_notification(self):
        stats = self.child.stats()
        self.assertEqual(stats.errors, {"toolErrors": 0, "apiErrors": 0, "aborted": 1})
        error = [e for e in self.child.all_events() if e.kind == "error"][0]
        self.assertEqual(error.preview, "Request was aborted")
        note = [e for e in self.child.all_events() if e.kind == "notification"][0]
        self.assertIn("hurry", note.preview)

    def test_models(self):
        self.assertEqual(self.main.models, ["gpt-5.6-sol"])
        self.assertEqual(self.child.models, ["gemini-3.8-flash"])

    def test_full_content_of_tool_call(self):
        with open(os.path.join(self.root, ROOT_REL), "rb") as handle:
            record = json.loads(handle.read().split(b"\n")[4])
        seq = [e for e in self.main.all_events() if e.kind == "skill"][0].seq
        full = self.main.full_content(seq, record)
        self.assertEqual(full["input"], {"path": "skill://jira-integration"})
        self.assertEqual(full["kind"], "skill")


class OmpRecordTest(unittest.TestCase):
    def setUp(self):
        self.state = AgentState("omp:s:A", OmpParser("omp:s:A"))
        self.n = 0

    def feed(self, record):
        self.n += 1
        return self.state.feed_line(json.dumps(record).encode(), self.n * 100, self.n)

    def test_title_change_wins_over_session_title(self):
        self.feed({"type": "session", "id": "s", "title": "first"})
        self.feed({"type": "title_change", "id": "a", "title": "renamed"})
        self.assertEqual(self.state.session_meta["title"], "renamed")

    def test_error_message_without_abort_is_api_error(self):
        self.feed({"type": "message", "id": "r", "message": {"role": "assistant", "stopReason": "error",
                                                              "errorMessage": "429", "content": []}})
        self.assertEqual(self.state.stats().errors["apiErrors"], 1)

    def test_nested_child_spawn_key_uses_relative_path(self):
        self.feed({"type": "message", "id": "a", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "c", "name": "task", "arguments": {}}]}})
        result = self.feed({"type": "message", "id": "b", "message": {"role": "toolResult", "toolCallId": "c", "toolName": "task",
                                                                      "details": {"progress": [{"id": "B", "agent": "x"}]}}})
        self.assertEqual(self.state.tool_calls["c"].spawned_agent_key, "omp:s:A/B")
        self.assertEqual(result.links[0].to_agent_id, "B")

    def test_tool_result_error(self):
        self.feed({"type": "message", "id": "b", "message": {"role": "toolResult", "toolCallId": "c", "toolName": "bash", "isError": True,
                                                              "content": [{"type": "text", "text": "fail"}]}})
        self.assertEqual(self.state.stats().errors["toolErrors"], 1)

    def test_random_records_never_raise(self):
        rng = random.Random(99)
        pool = [None, 1, "x", [], {}, {"role": "assistant"}, {"role": "toolResult", "details": {"progress": [1, {"id": 2}]}},
                {"role": "user", "content": "s"}, {"customType": "session_exit"}]
        types = ["session", "title", "message", "custom", "custom_message", "model_change", "zz"]
        keys = ["message", "data", "content", "id", "timestamp", "customType", "usage", "title"]
        for i in range(10000):
            if i % 4 == 0:
                raw = bytes(rng.randrange(256) for _ in range(rng.randint(0, 30)))
            else:
                rec = {k: rng.choice(pool) for k in rng.sample(keys, rng.randint(0, 5))}
                rec["type"] = rng.choice(types)
                raw = json.dumps(rec).encode()
            self.state.feed_line(raw, i, i + 1)
        self.state.stats()


if __name__ == "__main__":
    unittest.main()
