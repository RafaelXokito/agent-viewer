"""G-PERF-1: build_graph for 300 agents with 20 distinct tools each in under 20 ms.
G-PERF-12: a toolName-filtered events page of 200 over a 30 MB transcript in under 50 ms.

Excluded from the default run; enable with AGENT_VIEWER_PERF=1.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from agent_viewer import graph  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_index_budget import PROJECT, SID, generate  # noqa: E402

ENABLED = os.environ.get("AGENT_VIEWER_PERF") == "1"
SESSION = "claude:perf"
AGENTS = 300
TOOLS = 20
FAN_OUT = 30
BUDGET_S = 0.020
PAGE_BUDGET_MS = 50.0
RUNS = 7


def synthetic_inputs():
    """A root with 30 children, each with up to 9 children, 300 agents in all."""
    keys = ["%s:main" % SESSION] + ["%s:a%04d" % (SESSION, i) for i in range(1, AGENTS)]
    parents = {keys[i]: keys[0] if i <= FAN_OUT else keys[1 + (i - FAN_OUT - 1) % FAN_OUT]
               for i in range(1, AGENTS)}
    children = {k: [] for k in keys}
    for key in keys[1:]:
        children[parents[key]].append(key)
    nodes = {}
    for key in keys:
        parent = parents.get(key)
        nodes[key] = {"key": key, "agentId": key.rsplit(":", 1)[1], "parentKey": parent,
                      "depth": 0 if parent is None else 1, "agentType": "general-purpose",
                      "description": "agent %s" % key, "name": None,
                      "linkedBy": "root" if parent is None else "meta",
                      "spawnToolCallId": "toolu_%s" % key, "spawnedAt": "2026-09-25T10:00:00.000Z",
                      "lastActivityAt": "2026-09-25T10:05:00.000Z", "status": "running",
                      "stoppedByUser": False, "isFork": False, "forkedSkill": None,
                      "resumes": [{"byAgentKey": keys[0], "toolCallId": "r_%s" % key,
                                   "timestamp": "2026-09-25T10:01:00.000Z"}] if parent else [],
                      "models": ["claude-opus-5-5"], "eventCount": 500, "missing": False,
                      "children": children[key], "warnings": [], "tokensTotal": 12345}
    usage = {key: {"tools": {"Tool%02d" % t: {"calls": 10 + t, "errors": t % 3,
                                              "totalDurationMs": 100 * t} for t in range(TOOLS)},
                   "skills": {"skill-%d" % s: {"count": s + 1, "via": {"tool": s + 1}}
                              for s in range(2)},
                   "errors": {"toolErrors": 3, "apiErrors": 0, "aborted": 0},
                   "pending": {"Tool00": 1}}
             for key in keys}
    summaries = {SESSION: {"key": SESSION, "indexed": True, "continuedFrom": None,
                           "continuedIn": None}}
    return {"sessionKey": SESSION, "rootKey": keys[0], "nodes": nodes}, summaries, usage


@unittest.skipUnless(ENABLED, "set AGENT_VIEWER_PERF=1 to run the performance budgets")
class GraphBudgetTest(unittest.TestCase):
    def median_ms(self, tree, summaries, usage, limits=None):
        timings = []
        for _ in range(RUNS):
            start = time.perf_counter()
            graph.build_graph(SESSION, tree, summaries, usage, limits=limits)
            timings.append(time.perf_counter() - start)
        return sorted(timings)[RUNS // 2]

    def test_build_graph_300_agents_20_tools(self):
        tree, summaries, usage = synthetic_inputs()
        result = graph.build_graph(SESSION, tree, summaries, usage)
        self.assertEqual(sum(1 for n in result["nodes"] if n["kind"] == "agent"), AGENTS)
        self.assertTrue(result["truncated"], "6,600 nodes exceed MAX_NODES")
        self.assertEqual(len(result["nodes"]), graph.MAX_NODES)
        median = self.median_ms(tree, summaries, usage)
        print("\nbuild_graph 300 agents x 20 tools: median %.1f ms" % (median * 1000))
        self.assertLess(median, BUDGET_S)

    def test_build_graph_uncapped(self):
        tree, summaries, usage = synthetic_inputs()
        limits = {"maxNodes": 100000}
        result = graph.build_graph(SESSION, tree, summaries, usage, limits=limits)
        self.assertEqual(sum(1 for n in result["nodes"] if n["kind"] == "tool"), AGENTS * TOOLS)
        median = self.median_ms(tree, summaries, usage, limits)
        print("\nbuild_graph uncapped (6,600 nodes): median %.1f ms" % (median * 1000))
        self.assertLess(median, 2 * BUDGET_S)


@unittest.skipUnless(ENABLED, "set AGENT_VIEWER_PERF=1 to run the performance budgets")
class ToolFilterBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agent_viewer.api import Api
        from agent_viewer.store import Store, StoreConfig
        cls.root = tempfile.mkdtemp(prefix="av-perf-filter-")
        claude = os.path.join(cls.root, "claude")
        os.makedirs(os.path.join(claude, PROJECT))
        os.makedirs(os.path.join(cls.root, "omp"))
        generate(os.path.join(claude, PROJECT, SID + ".jsonl"))
        store = Store(StoreConfig(claude_root=claude, omp_root=os.path.join(cls.root, "omp")))
        store.scan()
        while store.index_step(None):
            pass
        store.refresh()
        cls.api = Api(store)
        cls.path = "/api/agents/claude/%s/main/events" % SID

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root)

    def page_ms(self, query, runs=10):
        status, page = self.api.handle(self.path, query)
        self.assertEqual(status, 200, page)
        start = time.perf_counter()
        for _ in range(runs):
            self.api.handle(self.path, query)
        return (time.perf_counter() - start) * 1000 / runs, page

    def test_filtered_pages_of_200(self):
        last_ms, page = self.page_ms("limit=200&toolName=Bash")
        self.assertEqual(len(page["items"]), 200)
        self.assertEqual({e["toolName"] for e in page["items"]}, {"Bash"})
        middle_ms, _ = self.page_ms("limit=200&toolName=Bash&before=%d" % (page["fromSeq"] // 2))
        rare_ms, rare = self.page_ms("limit=200&toolName=NoSuchTool", runs=3)
        self.assertEqual(rare["items"], [])
        sys.stderr.write("\nperf: toolName page last %.1f ms, middle %.1f ms, no match (full scan) %.1f ms\n"
                         % (last_ms, middle_ms, rare_ms))
        self.assertLess(last_ms, PAGE_BUDGET_MS)
        self.assertLess(middle_ms, PAGE_BUDGET_MS)


if __name__ == "__main__":
    unittest.main()
