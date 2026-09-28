"""Contract mock: the graph endpoint data, the toolName filter and the graph.update script."""
import importlib.util
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contract_shape import EXAMPLES, REPO, load_example  # noqa: E402


def load_mock_module():
    path = os.path.join(REPO, "contract", "mock_server.py")
    spec = importlib.util.spec_from_file_location("agent_viewer_mock_server_t", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOCK = load_mock_module()

A2 = "claude:s-main:a2222222222222222"


def seqs(page):
    return [e["seq"] for e in page["items"]]


class ExampleStoreToolFilterTest(unittest.TestCase):
    def setUp(self):
        self.store = MOCK.ExampleStore()

    def test_tool_name_keeps_only_that_tools_call_and_result(self):
        page = self.store.events(A2, None, None, 200, None, False, tool_name="Bash")
        self.assertEqual(page, load_example("events_page_tool_filter.json"))

    def test_tool_name_is_exact_and_case_sensitive(self):
        page = self.store.events(A2, None, None, 200, None, False, tool_name="bash")
        self.assertEqual(page["items"], [])
        self.assertIsNone(page["fromSeq"])
        self.assertIsNone(page["toSeq"])

    def test_tool_name_combines_with_kinds(self):
        page = self.store.events(A2, None, None, 200, {"tool_result"}, False, tool_name="Bash")
        self.assertEqual(seqs(page), [2])

    def test_total_is_the_unfiltered_count(self):
        filtered = self.store.events(A2, None, None, 200, None, False, tool_name="Bash")
        by_kind = self.store.events(A2, None, None, 200, {"tool_call"}, False)
        self.assertEqual(filtered["total"], 4)
        self.assertEqual(by_kind["total"], 4)

    def test_no_tool_name_is_unchanged(self):
        page = self.store.events(A2, None, None, 200, None, False)
        self.assertEqual(seqs(page), [0, 1, 2, 3])


class ExampleStoreGraphTest(unittest.TestCase):
    def setUp(self):
        self.store = MOCK.ExampleStore()

    def test_graph_serves_the_example_verbatim(self):
        self.assertEqual(self.store.graph("claude:s-main"), load_example("graph.json"))

    def test_graph_of_other_sessions_is_none(self):
        self.assertIsNone(self.store.graph("claude:s-other"))
        self.assertIsNone(self.store.graph("claude:nope"))

    def test_graph_reports_indexing(self):
        self.store.set_indexed("claude:s-main", False)
        self.assertFalse(self.store.graph("claude:s-main")["indexed"])

    def test_graph_is_a_copy(self):
        self.store.graph("claude:s-main")["nodes"].clear()
        self.assertTrue(self.store.graph("claude:s-main")["nodes"])


class ScriptTest(unittest.TestCase):
    def test_graph_update_is_scoped_to_its_session(self):
        data = {"sessionKey": "claude:s-main", "rev": 2, "graph": None}
        self.assertEqual(MOCK.session_of("graph.update", data), "claude:s-main")

    def test_script_graph_updates_follow_tree_updates(self):
        script = MOCK.load_script()
        events = [step["event"] for step in script]
        graph_steps = [step for step in script if step["event"] == "graph.update"]
        self.assertEqual([s["data"]["rev"] for s in graph_steps], [2, 3, 4])
        first = events.index("graph.update")
        self.assertEqual(events[first - 1], "tree.update")
        self.assertTrue(graph_steps[2]["data"]["overflow"])
        self.assertIsNone(graph_steps[2]["data"]["graph"])

    def test_script_graphs_differ_from_the_example_only_as_documented(self):
        example = load_example("graph.json")
        wanted = {2: ("running", 5), 3: ("finished", 4)}
        for step in MOCK.load_script():
            data = step["data"]
            if step["event"] != "graph.update" or data["graph"] is None:
                continue
            graph = data["graph"]
            self.assertEqual(graph["rev"], data["rev"])
            node = next(n for n in graph["nodes"] if n["id"] == A2)
            self.assertEqual((node["status"], node["eventCount"]), wanted[data["rev"]])
            expected = json.loads(json.dumps(example))
            expected["rev"] = data["rev"]
            for item in expected["nodes"]:
                if item["id"] == A2:
                    item["status"], item["eventCount"] = wanted[data["rev"]]
            self.assertEqual(graph, expected)

    def test_chain_example_is_not_served(self):
        self.assertTrue(os.path.exists(os.path.join(EXAMPLES, "graph_chain.json")))
        self.assertIsNone(MOCK.ExampleStore().graph("claude:s-chain-2"))


if __name__ == "__main__":
    unittest.main()
