"""AgentState.events(..., tool_name=...): the /events toolName filter (feature doc 8.8)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
import loader  # noqa: E402


def seqs(events):
    return [e.seq for e in events]


class ToolNameFilterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = loader.copy_fixtures()
        cls.index = loader.index_roots(os.path.join(cls.root, "claude"), os.path.join(cls.root, "omp"))

    @classmethod
    def tearDownClass(cls):
        loader.remove_fixtures(cls.root)

    def events(self, key, tool_name, kinds=None, include_meta=False, before=None, after=None,
               limit=200):
        state = self.index.states[key][1]
        return state.events(before, after, limit, kinds, include_meta, tool_name=tool_name)

    def test_exact_match_keeps_call_and_result(self):
        got = self.events("claude:s-main:a2222222222222222", "Bash")
        self.assertEqual([(e.seq, e.kind) for e in got], [(1, "tool_call"), (2, "tool_result")])
        self.assertEqual(got[0].result_seq, 2, "decoration still applies")

    def test_match_is_case_sensitive(self):
        self.assertEqual(self.events("claude:s-main:a2222222222222222", "bash"), [])
        self.assertEqual(self.events("claude:s-main:a2222222222222222", "Bas"), [])

    def test_none_means_no_filter(self):
        self.assertEqual(seqs(self.events("claude:s-main:a2222222222222222", None)), [0, 1, 2, 3])

    def test_and_with_kinds(self):
        got = self.events("claude:s-main:main", "Agent", kinds={"tool_result"})
        self.assertEqual(seqs(got), [5, 6])
        self.assertEqual(self.events("claude:s-main:main", "Agent", kinds={"text"}), [])

    def test_several_calls_of_one_tool(self):
        self.assertEqual(seqs(self.events("claude:s-main:main", "Agent")), [3, 4, 5, 6])
        self.assertEqual(seqs(self.events("claude:s-main:main", "SendMessage")), [9, 10])

    def test_before_and_after_walk_the_filtered_sequence(self):
        key = "claude:s-main:main"
        self.assertEqual(seqs(self.events(key, "Agent", limit=2)), [5, 6])
        self.assertEqual(seqs(self.events(key, "Agent", limit=2, before=5)), [3, 4])
        self.assertEqual(seqs(self.events(key, "Agent", limit=1, after=3)), [4])
        self.assertEqual(seqs(self.events(key, "Agent", limit=5, after=6)), [])

    def test_omp_read_tool_and_include_meta(self):
        key = "omp:o-root:main"
        self.assertEqual(seqs(self.events(key, "read")), [5, 8])
        self.assertEqual(seqs(self.events(key, "read", include_meta=True)), [5, 7, 8])
        self.assertEqual(seqs(self.events(key, "read", kinds={"tool_result"})), [8])
        self.assertEqual(seqs(self.events(key, "task")), [6, 9])


if __name__ == "__main__":
    unittest.main()
