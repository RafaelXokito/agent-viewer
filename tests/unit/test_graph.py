"""graph.build_graph: the pure graph builder (docs/FEATURE-graph-canvas.md 8.3)."""
import copy
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
from contract_shape import load_example  # noqa: E402
import loader  # noqa: E402

from agent_viewer import graph  # noqa: E402

S_MAIN = "claude:s-main"
MAIN = S_MAIN + ":main"
A1 = S_MAIN + ":a1111111111111111"
A2 = S_MAIN + ":a2222222222222222"


def usage_of(state):
    stats = state.stats()
    return {"tools": stats.tools, "skills": stats.skills, "errors": stats.errors,
            "pending": state.pending_by_tool()}


def without_rev(value):
    value = dict(value)
    value.pop("rev", None)
    return value


def by_id(result):
    return {n["id"]: n for n in result["nodes"]}


def agent_tree_node(key, parent=None, children=(), resumes=(), **extra):
    session = key.rsplit(":", 1)[0]
    node = {"key": key, "agentId": key.rsplit(":", 1)[1], "sessionId": session.split(":", 1)[1],
            "parentKey": parent, "depth": 0 if parent is None else 1,
            "agentType": "main" if parent is None else "general-purpose", "description": None,
            "name": None, "linkedBy": "root" if parent is None else "meta",
            "spawnToolCallId": None, "spawnedAt": None, "lastActivityAt": None,
            "status": "finished", "stoppedByUser": False, "isFork": False, "forkedSkill": None,
            "resumes": list(resumes), "models": [], "eventCount": 0, "missing": False,
            "children": list(children), "warnings": [], "tokensTotal": 0}
    node.update(extra)
    return node


def tree_of(session_key, nodes):
    return {"sessionKey": session_key, "rootKey": session_key + ":main",
            "nodes": {n["key"]: n for n in nodes}}


def summary(key, **extra):
    base = {"key": key, "title": None, "status": "finished", "startedAt": None,
            "lastActivityAt": None, "agentCount": 1, "indexed": True,
            "continuedFrom": None, "continuedIn": None}
    base.update(extra)
    return base


def tools_usage(tools, skills=None, pending=None):
    return {"tools": {name: {"calls": calls, "errors": 0, "totalDurationMs": 0}
                      for name, calls in tools.items()},
            "skills": skills or {}, "errors": {"toolErrors": 0, "apiErrors": 0, "aborted": 0},
            "pending": pending or {}}


class FixtureGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = loader.copy_fixtures()
        cls.index = loader.index_roots(os.path.join(cls.root, "claude"), os.path.join(cls.root, "omp"))
        cls.usage = {key: usage_of(state) for key, (_, state) in cls.index.states.items()}
        cls.summaries = {s["key"]: s for s in load_example("sessions.json")["items"]}

    @classmethod
    def tearDownClass(cls):
        loader.remove_fixtures(cls.root)

    def build(self, **kwargs):
        return graph.build_graph(S_MAIN, load_example("tree.json"), self.summaries, self.usage, **kwargs)

    def test_contract_tree_and_fixture_stats_give_the_example(self):
        self.assertEqual(self.build(), without_rev(load_example("graph.json")))

    def test_builder_does_not_set_rev(self):
        self.assertNotIn("rev", self.build())

    def test_relation_tools_are_counted_but_not_nodes(self):
        nodes = by_id(self.build())
        self.assertEqual((nodes[MAIN]["toolCalls"], nodes[MAIN]["relationToolCalls"]), (4, 4))
        self.assertFalse([n for n in nodes.values() if n["kind"] == "tool" and n["ownerId"] == MAIN])
        self.assertEqual(nodes[A1]["relationToolCalls"], 1)

    def test_orphan_spawn_edge(self):
        edges = {e["id"]: e for e in self.build()["edges"]}
        orphan = edges["spawn|%s|%s:a4444444444444444" % (MAIN, S_MAIN)]
        self.assertEqual((orphan["linkedBy"], orphan["toolCallId"]), ("orphan", None))

    def test_omp_fixture(self):
        key = "omp:o-root"
        tree = self.index.tree.session_tree(key)
        result = graph.build_graph(key, tree, {}, self.usage)
        nodes = by_id(result)
        main = nodes["omp:o-root:main"]
        self.assertEqual(main["relationToolCalls"], 1, "task is a relation tool")
        self.assertIn("tool|omp:o-root:main|read", nodes)
        self.assertNotIn("tool|omp:o-root:main|task", nodes)
        skill = nodes["skill|omp:o-root:main|jira-integration"]
        self.assertEqual(skill["via"]["read"], 1)
        spawn = [e for e in result["edges"] if e["kind"] == "spawn"]
        self.assertEqual([(e["from"], e["to"], e["linkedBy"]) for e in spawn],
                         [("omp:o-root:main", "omp:o-root:Checker", "parentSession")])
        self.assertFalse(result["indexed"], "no summary means not indexed")


class ResumeTest(unittest.TestCase):
    def test_resumes_are_grouped_per_ordered_pair(self):
        key = "claude:r"
        m, a, b = key + ":main", key + ":a", key + ":b"
        resumes = [{"byAgentKey": m, "toolCallId": "t2", "timestamp": "2026-09-25T10:00:02.000Z"},
                   {"byAgentKey": b, "toolCallId": "t3", "timestamp": "2026-09-25T10:00:03.000Z"},
                   {"byAgentKey": m, "toolCallId": "t1", "timestamp": "2026-09-25T10:00:01.000Z"},
                   {"byAgentKey": "claude:other:main", "toolCallId": "tx", "timestamp": None}]
        tree = tree_of(key, [agent_tree_node(m, children=[a, b]),
                             agent_tree_node(a, m, resumes=resumes), agent_tree_node(b, m)])
        result = graph.build_graph(key, tree, {}, {})
        resume = [e for e in result["edges"] if e["kind"] == "resume"]
        self.assertEqual(resume, [
            {"id": "resume|%s|%s" % (m, a), "kind": "resume", "from": m, "to": a, "count": 2,
             "toolCallIds": ["t1", "t2"], "lastAt": "2026-09-25T10:00:02.000Z"},
            {"id": "resume|%s|%s" % (b, a), "kind": "resume", "from": b, "to": a, "count": 1,
             "toolCallIds": ["t3"], "lastAt": "2026-09-25T10:00:03.000Z"}])
        self.assertEqual(by_id(result)[a]["resumeCount"], 4, "resumeCount is the tree's list length")


class ChainTest(unittest.TestCase):
    def chain_inputs(self):
        example = load_example("graph_chain.json")
        root = example["nodes"][0]
        node = agent_tree_node(root["key"], models=root["models"], status=root["status"],
                               tokensTotal=root["tokensTotal"], eventCount=root["eventCount"],
                               spawnedAt=root["spawnedAt"], lastActivityAt=root["lastActivityAt"])
        tree = tree_of("claude:s-chain-2", [node])
        summaries = {
            "claude:s-chain-1": summary("claude:s-chain-1", title="Chain part one",
                                        startedAt="2026-09-25T11:00:00.000Z",
                                        lastActivityAt="2026-09-25T11:59:00.000Z", agentCount=2,
                                        continuedIn="claude:s-chain-2"),
            "claude:s-chain-2": summary("claude:s-chain-2", status="running",
                                        continuedFrom="claude:s-chain-1",
                                        continuedIn="claude:s-chain-3"),
        }
        usage = {root["key"]: {"tools": {"Read": {"calls": 3, "errors": 0, "totalDurationMs": 90}},
                               "skills": {}, "errors": dict(root["errors"]), "pending": {"Read": 1}}}
        return example, tree, summaries, usage

    def test_reproduces_the_chain_example(self):
        example, tree, summaries, usage = self.chain_inputs()
        self.assertEqual(graph.build_graph("claude:s-chain-2", tree, summaries, usage),
                         without_rev(example))

    def chain(self, length, cycle=False):
        keys = ["claude:c%d" % i for i in range(length)]
        summaries = {}
        for i, key in enumerate(keys):
            nxt = keys[(i + 1) % length] if cycle or i + 1 < length else None
            prev = keys[i - 1] if cycle or i > 0 else None
            summaries[key] = summary(key, continuedIn=nxt, continuedFrom=prev)
        return keys, summaries

    def sessions(self, result):
        return [(n["sessionKey"], n["relation"], n["hops"]) for n in result["nodes"]
                if n["kind"] == "session"]

    def test_five_hops_each_way(self):
        keys, summaries = self.chain(13)
        current = keys[6]
        tree = tree_of(current, [agent_tree_node(current + ":main")])
        got = self.sessions(graph.build_graph(current, tree, summaries, {}))
        self.assertEqual(got, [(keys[6 - h], "predecessor", h) for h in range(1, 6)]
                         + [(keys[6 + h], "successor", h) for h in range(1, 6)])

    def test_cycle_stops_at_a_repeated_key(self):
        keys, summaries = self.chain(3, cycle=True)
        tree = tree_of(keys[0], [agent_tree_node(keys[0] + ":main")])
        result = graph.build_graph(keys[0], tree, summaries, {})
        got = self.sessions(result)
        self.assertEqual(len(got), 2)
        self.assertEqual(len({k for k, _, _ in got}), 2)
        self.assertEqual(len({n["id"] for n in result["nodes"]}), len(result["nodes"]))

    def test_chain_edges_link_neighbours_in_order(self):
        keys, summaries = self.chain(5)
        current = keys[2]
        root = current + ":main"
        tree = tree_of(current, [agent_tree_node(root)])
        edges = [(e["from"], e["to"]) for e in graph.build_graph(current, tree, summaries, {})["edges"]]
        s = lambda k: "session|" + k  # noqa: E731
        self.assertEqual(sorted(edges), sorted([(s(keys[0]), s(keys[1])), (s(keys[1]), root),
                                                (root, s(keys[3])), (s(keys[3]), s(keys[4]))]))

    def test_missing_neighbour_stops_that_direction(self):
        summaries = {"claude:x": summary("claude:x", continuedFrom="claude:gone",
                                         continuedIn="claude:y"),
                     "claude:y": summary("claude:y", continuedIn="claude:z")}
        tree = tree_of("claude:x", [agent_tree_node("claude:x:main")])
        result = graph.build_graph("claude:x", tree, summaries, {})
        nodes = [n for n in result["nodes"] if n["kind"] == "session"]
        self.assertEqual([(n["sessionKey"], n["missing"]) for n in nodes],
                         [("claude:gone", True), ("claude:y", False), ("claude:z", True)])
        self.assertIsNone(nodes[0]["title"])
        self.assertIsNone(nodes[0]["agentCount"])


class LimitsTest(unittest.TestCase):
    def test_tools_per_agent_cap(self):
        key = "claude:t"
        m = key + ":main"
        tools = {"T%02d" % i: 100 - i for i in range(55)}
        tools["Agent"] = 7
        tree = tree_of(key, [agent_tree_node(m)])
        result = graph.build_graph(key, tree, {}, {m: tools_usage(tools)})
        tool_nodes = [n for n in result["nodes"] if n["kind"] == "tool"]
        self.assertEqual([n["name"] for n in tool_nodes], ["T%02d" % i for i in range(50)])
        main = by_id(result)[m]
        self.assertEqual((main["toolsOmitted"], main["toolsOmittedCalls"]), (5, sum(range(46, 51))))
        self.assertEqual(main["toolCalls"], sum(tools.values()))
        self.assertFalse(result["truncated"])

    def test_tools_order_by_calls_then_name(self):
        key = "claude:t"
        m = key + ":main"
        tree = tree_of(key, [agent_tree_node(m)])
        skills = {"b": {"count": 1, "via": {}}, "a": {"count": 1, "via": {}},
                  "c": {"count": 5, "via": {}}}
        result = graph.build_graph(key, tree, {}, {m: tools_usage({"Read": 2, "Bash": 2, "Grep": 9},
                                                                   skills, {"Bash": 1})})
        names = [(n["kind"], n["name"]) for n in result["nodes"][1:]]
        self.assertEqual(names, [("tool", "Grep"), ("tool", "Bash"), ("tool", "Read"),
                                 ("skill", "c"), ("skill", "a"), ("skill", "b")])
        self.assertEqual(by_id(result)["tool|%s|Bash" % m]["pending"], 1)

    def test_max_nodes_drops_tools_and_skills_from_the_end(self):
        key = "claude:m"
        m, a = key + ":main", key + ":a"
        tree = tree_of(key, [agent_tree_node(m, children=[a]), agent_tree_node(a, m)])
        usage = {m: tools_usage({"X": 3, "Y": 2}), a: tools_usage({"Z": 4}, {"s": {"count": 1, "via": {}}})}
        result = graph.build_graph(key, tree, {}, usage, limits={"maxNodes": 4})
        self.assertTrue(result["truncated"])
        self.assertEqual([n["id"] for n in result["nodes"]],
                         [m, a, "tool|%s|X" % m, "tool|%s|Y" % m])
        self.assertEqual(result["limits"], {"toolsPerAgent": 50, "chainHops": 5, "maxNodes": 4})
        nodes = by_id(result)
        self.assertEqual((nodes[a]["toolsOmitted"], nodes[a]["toolsOmittedCalls"]), (1, 4))
        ids = set(nodes)
        self.assertTrue(all(e["from"] in ids and e["to"] in ids for e in result["edges"]))

    def test_max_nodes_never_drops_agents(self):
        key = "claude:m"
        m = key + ":main"
        children = ["%s:a%d" % (key, i) for i in range(5)]
        tree = tree_of(key, [agent_tree_node(m, children=children)]
                       + [agent_tree_node(c, m) for c in children])
        result = graph.build_graph(key, tree, {}, {m: tools_usage({"X": 1})}, limits={"maxNodes": 2})
        self.assertEqual(len(result["nodes"]), 6)
        self.assertTrue(result["truncated"])


class OrderAndRobustnessTest(unittest.TestCase):
    def test_shuffled_insertion_order_gives_an_identical_result(self):
        tree = load_example("tree.json")
        root = FixtureGraphTest
        root.setUpClass()
        try:
            summaries, usage = root.summaries, root.usage
            expected = graph.build_graph(S_MAIN, tree, summaries, usage)
            rng = random.Random(7)
            for _ in range(5):
                items = list(tree["nodes"].items())
                rng.shuffle(items)
                shuffled = dict(tree, nodes=dict(items))
                use = list(usage.items())
                rng.shuffle(use)
                shuffled_usage = {}
                for k, v in use:
                    tools = list(v["tools"].items())
                    rng.shuffle(tools)
                    shuffled_usage[k] = dict(v, tools=dict(tools))
                got = graph.build_graph(S_MAIN, shuffled, summaries, shuffled_usage)
                self.assertEqual(json.dumps(got), json.dumps(expected))
        finally:
            root.tearDownClass()

    def test_unreachable_nodes_follow_in_key_order(self):
        key = "claude:u"
        m = key + ":main"
        nodes = [agent_tree_node(m, children=[key + ":b"]), agent_tree_node(key + ":b", m),
                 agent_tree_node(key + ":z", key + ":y", children=[key + ":c"]),
                 agent_tree_node(key + ":c", key + ":z"), agent_tree_node(key + ":d", key + ":y")]
        result = graph.build_graph(key, tree_of(key, nodes), {}, {})
        self.assertEqual([n["id"] for n in result["nodes"]],
                         [m, key + ":b", key + ":c", key + ":d", key + ":z"])
        spawn_to = [e["to"] for e in result["edges"] if e["kind"] == "spawn"]
        self.assertEqual(spawn_to, [key + ":b", key + ":c"], "edges from missing parents are dropped")

    def test_empty_and_missing_inputs_do_not_raise(self):
        for tree in (None, {}, {"nodes": {}}, {"rootKey": "claude:e:main", "nodes": None}):
            result = graph.build_graph("claude:e", tree, None, None)
            self.assertEqual((result["nodes"], result["edges"]), ([], []))
            self.assertEqual(result["rootKey"], "claude:e:main")

    def test_missing_stats_and_unknown_fields(self):
        key = "claude:k"
        m = key + ":main"
        node = agent_tree_node(m, surprise={"x": 1})
        del node["models"], node["warnings"]
        result = graph.build_graph(key, tree_of(key, [node]), {key: {"indexed": True, "extra": 1}},
                                   {m: {"tools": None, "unknown": 1}})
        main = result["nodes"][0]
        self.assertEqual((main["toolCalls"], main["models"], main["warnings"]), (0, [], []))
        self.assertEqual(main["errors"], {"toolErrors": 0, "apiErrors": 0, "aborted": 0})
        self.assertTrue(result["indexed"])

    def test_inputs_are_not_mutated(self):
        tree = load_example("tree.json")
        before = copy.deepcopy(tree)
        graph.build_graph(S_MAIN, tree, {}, {})
        self.assertEqual(tree, before)


if __name__ == "__main__":
    unittest.main()
