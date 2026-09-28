"""WS1: agent and subtree stats aggregation against the totals of SPEC 13.2."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
import loader  # noqa: E402

from agent_viewer.model import Stats  # noqa: E402
from agent_viewer.stats import agent_stats, merge, subtree_stats  # noqa: E402

S = "claude:s-main"
STATS_KEYS = {"scope", "tokens", "costUsd", "byModel", "tools", "skills", "slashCommands", "subagentTypes", "agents",
              "durationMs", "activeDurationMs", "errors", "turns", "parse"}


class FixtureStatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = loader.copy_fixtures()
        cls.index = loader.index_roots(os.path.join(cls.root, "claude"), os.path.join(cls.root, "omp"), now=time.time())
        cls.by_key = {key: state.stats() for key, (_sf, state) in cls.index.states.items()}

    @classmethod
    def tearDownClass(cls):
        loader.remove_fixtures(cls.root)

    def test_agent_scope_of_nested_leaf(self):
        stats = agent_stats(self.index.tree, S + ":a2222222222222222", self.by_key[S + ":a2222222222222222"]).to_dict()
        self.assertEqual(set(stats), STATS_KEYS)
        self.assertEqual(stats["scope"], "agent")
        self.assertEqual(stats["tokens"], {"input": 4, "output": 6, "cacheRead": 300, "cacheCreation": 300, "reasoning": 0, "total": 610})
        self.assertEqual(stats["tools"], {"Bash": {"calls": 1, "errors": 1, "totalDurationMs": 200}})
        self.assertEqual(stats["agents"], {"total": 1, "maxDepth": 2, "running": 0})
        self.assertEqual((stats["durationMs"], stats["activeDurationMs"], stats["turns"]), (800, None, 1))
        self.assertEqual(stats["errors"], {"toolErrors": 1, "apiErrors": 0, "aborted": 0})
        self.assertEqual(stats["parse"], {"lines": 4, "skipped": 0, "unknownTypes": {}})
        self.assertEqual(stats["byModel"], {"claude-haiku-4-5-20251001": {"messages": 2, "input": 4, "output": 6, "cacheRead": 300, "cacheCreation": 300}})

    def test_main_agent_scope_matches_expected_totals(self):
        stats = self.by_key[S + ":main"].to_dict()
        self.assertEqual(stats["tokens"]["total"], 5235)
        self.assertEqual(stats["subagentTypes"], {"general-purpose": 1, "ecc:code-explorer": 1})
        self.assertEqual(stats["parse"]["unknownTypes"], {"brand-new-type": 1})
        self.assertEqual(stats["durationMs"], 32100)

    def test_session_subtree(self):
        stats = subtree_stats(self.index.tree, S + ":main", self.by_key).to_dict()
        self.assertEqual(stats["scope"], "subtree")
        self.assertEqual(stats["tokens"], {"input": 25 + 9 + 4 + 1, "output": 110 + 12 + 6 + 1, "cacheRead": 4900 + 1000 + 300,
                                           "cacheCreation": 200 + 500 + 300, "reasoning": 0,
                                           "total": 5235 + 1521 + 610 + 2})
        self.assertEqual(stats["agents"]["total"], 5)
        self.assertEqual(stats["agents"]["maxDepth"], 2)
        self.assertIn(stats["agents"]["running"], (0, 1))
        self.assertEqual(stats["tools"]["Agent"]["calls"], 3)
        self.assertEqual(stats["tools"]["Bash"]["errors"], 1)
        self.assertEqual(stats["subagentTypes"], {"general-purpose": 2, "ecc:code-explorer": 1})
        self.assertEqual(stats["durationMs"], 60000)
        self.assertEqual(stats["activeDurationMs"], 32000)
        self.assertEqual(stats["turns"], 1 + 2 + 1)
        self.assertEqual(sorted(stats["byModel"]), ["claude-haiku-4-5-20251001", "claude-opus-5-5", "claude-sonnet-4-6"])

    def test_omp_subtree(self):
        stats = subtree_stats(self.index.tree, "omp:o-root:main", self.by_key).to_dict()
        tokens = stats["tokens"]
        self.assertEqual((tokens["input"], tokens["output"], tokens["cacheRead"], tokens["cacheCreation"]), (9, 204, 19726, 19726))
        self.assertAlmostEqual(stats["costUsd"], 0.1)
        self.assertEqual(stats["skills"], {"jira-integration": {"count": 1, "via": {"tool": 0, "slash": 0, "fork": 0, "read": 1, "preload": 0}}})
        self.assertEqual({k: v["calls"] for k, v in stats["tools"].items()}, {"read": 1, "task": 1})
        self.assertEqual(stats["errors"]["aborted"], 1)
        self.assertEqual(stats["agents"], {"total": 2, "maxDepth": 1, "running": 0})

    def test_broken_file(self):
        _sf, state = self.index.states["claude:s-broken:main"]
        parse = state.parse_stats
        self.assertEqual((parse.lines, parse.parsed, parse.skipped, parse.partial_tail), (6, 3, 3, True))
        self.assertEqual(sorted(s["reason"] for s in parse.skipped_samples), ["bad_utf8", "not_json", "not_object"])
        stats = state.stats()
        self.assertEqual(state.usages.keys(), {"msg_h"})
        self.assertEqual(stats.errors["apiErrors"], 1)
        self.assertEqual(stats.by_model.keys(), {"claude-opus-5-5"})


class MergeTest(unittest.TestCase):
    def test_merge_of_nothing_is_empty_subtree(self):
        stats = merge([]).to_dict()
        self.assertEqual(stats["scope"], "subtree")
        self.assertEqual(stats["tokens"]["total"], 0)
        self.assertIsNone(stats["costUsd"])
        self.assertIsNone(stats["durationMs"])
        self.assertEqual(stats["agents"]["total"], 0)

    def test_merge_does_not_mutate_inputs(self):
        a = Stats.empty()
        a.tools["Bash"] = {"calls": 1, "errors": 0, "totalDurationMs": 5}
        merged = merge([a, a])
        self.assertEqual(merged.tools["Bash"]["calls"], 2)
        self.assertEqual(a.tools["Bash"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
