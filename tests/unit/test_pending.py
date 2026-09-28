"""AgentState.pending_by_tool(): tool calls without a result, per tool name (G-PERF-2)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
import loader  # noqa: E402,F401  (puts the repo on sys.path)

from agent_viewer.accumulate import AgentState  # noqa: E402
from agent_viewer.claude_parser import ClaudeParser  # noqa: E402

KEY = "claude:s-p:main"


def tool_use(uuid, calls, ts):
    content = [{"type": "tool_use", "id": cid, "name": name, "input": {}} for cid, name in calls]
    return {"type": "assistant", "uuid": uuid, "sessionId": "s-p", "timestamp": ts,
            "message": {"id": "msg_" + uuid, "model": "claude-opus-5-5", "stop_reason": "tool_use",
                        "content": content, "usage": {"input_tokens": 1, "output_tokens": 1}}}


def tool_result(uuid, cid, ts):
    return {"type": "user", "uuid": uuid, "sessionId": "s-p", "timestamp": ts,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": cid, "content": "ok"}]}}


class PendingByToolTest(unittest.TestCase):
    def setUp(self):
        self.state = AgentState(KEY, ClaudeParser(KEY))
        self.offset = 0

    def feed(self, record):
        raw = json.dumps(record).encode("utf-8")
        self.state.feed_line(raw, self.offset, self.offset + 1)
        self.offset += len(raw) + 1

    def test_empty_state_has_nothing_pending(self):
        self.assertEqual(self.state.pending_by_tool(), {})

    def test_counts_calls_without_result_per_name(self):
        self.feed(tool_use("u1", [("t1", "Bash"), ("t2", "Bash"), ("t3", "Read")],
                           "2026-09-25T10:00:00.000Z"))
        self.assertEqual(self.state.pending_by_tool(), {"Bash": 2, "Read": 1})

    def test_result_drops_the_call(self):
        self.feed(tool_use("u1", [("t1", "Bash"), ("t2", "Read")], "2026-09-25T10:00:00.000Z"))
        self.feed(tool_result("u2", "t1", "2026-09-25T10:00:01.000Z"))
        self.assertEqual(self.state.pending_by_tool(), {"Read": 1})
        self.feed(tool_result("u3", "t2", "2026-09-25T10:00:02.000Z"))
        self.assertEqual(self.state.pending_by_tool(), {})

    def test_reset_forgets_pending_calls(self):
        self.feed(tool_use("u1", [("t1", "Bash")], "2026-09-25T10:00:00.000Z"))
        self.state.reset()
        self.assertEqual(self.state.pending_by_tool(), {})

    def test_returns_a_new_dict(self):
        self.feed(tool_use("u1", [("t1", "Bash")], "2026-09-25T10:00:00.000Z"))
        self.state.pending_by_tool()["Bash"] = 99
        self.assertEqual(self.state.pending_by_tool(), {"Bash": 1})


class PendingOnFixturesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = loader.copy_fixtures()
        cls.index = loader.index_roots(os.path.join(cls.root, "claude"), os.path.join(cls.root, "omp"))

    @classmethod
    def tearDownClass(cls):
        loader.remove_fixtures(cls.root)

    def pending(self, key):
        return self.index.states[key][1].pending_by_tool()

    def test_fixture_agents(self):
        self.assertEqual(self.pending("claude:s-main:a3333333333333333"), {"Read": 1})
        self.assertEqual(self.pending("claude:s-main:a2222222222222222"), {})


if __name__ == "__main__":
    unittest.main()
