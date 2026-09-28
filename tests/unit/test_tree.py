"""WS1: tree reconstruction rules of SPEC 6.1 and 6.2."""
import os
import random
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
import loader  # noqa: E402

from agent_viewer.model import Link, NodeInput  # noqa: E402
from agent_viewer.tree import build_tree  # noqa: E402

T0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
S = "claude:s-main"


def ts(seconds):
    return T0 + timedelta(seconds=seconds)


def node(agent_id, session="s-main", meta=None, start=0, source="claude", **kw):
    return NodeInput(source=source, session_id=session, agent_id=agent_id, project="-p",
                     file="/r/%s/%s.jsonl" % (session, agent_id), meta=meta, started_at=ts(start), **kw)


def spawn(from_key, to, tool_call_id="t", at=0, **evidence):
    return Link(kind="spawn", from_key=from_key, to_agent_id=to, tool_call_id=tool_call_id, timestamp=ts(at), evidence=evidence)


class FixtureTreeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = loader.copy_fixtures()
        cls.index = loader.index_roots(os.path.join(cls.root, "claude"), os.path.join(cls.root, "omp"), now=time.time())

    @classmethod
    def tearDownClass(cls):
        loader.remove_fixtures(cls.root)

    def test_claude_demo_tree(self):
        tree = self.index.tree.session_tree(S)
        nodes = tree["nodes"]
        self.assertEqual(tree["rootKey"], S + ":main")
        main = nodes[S + ":main"]
        self.assertEqual(main["children"], [S + ":a1111111111111111", S + ":a3333333333333333", S + ":a4444444444444444"])
        self.assertEqual((main["linkedBy"], main["agentType"], main["depth"], main["eventCount"], main["tokensTotal"]),
                         ("root", "main", 0, 16, 5235))
        a1 = nodes[S + ":a1111111111111111"]
        self.assertEqual((a1["agentType"], a1["linkedBy"], a1["status"], a1["spawnToolCallId"], a1["description"]),
                         ("general-purpose", "meta", "finished", "toolu_A", "Helper one"))
        self.assertEqual(a1["resumes"], [{"byAgentKey": S + ":main", "toolCallId": "toolu_M", "timestamp": "2026-09-25T10:00:04.500Z"}])
        self.assertEqual(a1["children"], [S + ":a2222222222222222"])
        self.assertEqual(a1["spawnedAt"], "2026-09-25T10:00:01.100Z")
        a2 = nodes[S + ":a2222222222222222"]
        self.assertEqual((a2["linkedBy"], a2["depth"], a2["warnings"], a2["spawnedAt"]), ("meta", 2, [], "2026-09-25T10:00:02.500Z"))
        a3 = nodes[S + ":a3333333333333333"]
        self.assertEqual((a3["agentType"], a3["linkedBy"], a3["description"], a3["spawnToolCallId"]),
                         ("ecc:code-explorer", "transcript", "Helper legacy", "toolu_B"))
        self.assertIn(a3["status"], ("running", "stale"))
        a4 = nodes[S + ":a4444444444444444"]
        self.assertEqual((a4["agentType"], a4["linkedBy"], a4["warnings"]), (None, "orphan", ["orphan"]))
        self.assertNotIn("file", a4)

    def test_a3333_parse_stats_partial_tail(self):
        _sf, state = self.index.states[S + ":a3333333333333333"]
        self.assertTrue(state.parse_stats.partial_tail)
        self.assertEqual(state.parse_stats.skipped, 0)

    def test_tree_json_node_fields_match_contract(self):
        node_json = self.index.tree.session_tree(S)["nodes"][S + ":main"]
        self.assertEqual(set(node_json), {
            "key", "agentId", "sessionId", "parentKey", "depth", "agentType", "description", "name", "linkedBy",
            "spawnToolCallId", "spawnedAt", "lastActivityAt", "status", "stoppedByUser", "isFork", "forkedSkill",
            "resumes", "models", "eventCount", "missing", "children", "warnings", "tokensTotal"})

    def test_omp_tree(self):
        tree = self.index.tree.session_tree("omp:o-root")
        root = tree["nodes"]["omp:o-root:main"]
        self.assertEqual(root["children"], ["omp:o-root:Checker"])
        checker = tree["nodes"]["omp:o-root:Checker"]
        self.assertEqual((checker["agentType"], checker["linkedBy"], checker["spawnToolCallId"], checker["name"],
                          checker["status"], checker["description"]),
                         ("reviewer", "parentSession", "call_2|y", "Checker", "finished", "Delegating check"))
        self.assertEqual(root["status"], "finished")

    def test_broken_session_is_a_lone_root(self):
        tree = self.index.tree.session_tree("claude:s-broken")
        self.assertEqual(list(tree["nodes"]), ["claude:s-broken:main"])

    def test_sessions_listing(self):
        self.assertEqual(self.index.tree.session_keys(), ["claude:s-broken", "claude:s-main", "omp:o-root"])

    def test_deterministic_under_shuffle(self):
        expected = {k: self.index.tree.session_tree(k) for k in self.index.tree.session_keys()}
        rng = random.Random(7)
        for _ in range(20):
            nodes, links = list(self.index.nodes), list(self.index.links)
            rng.shuffle(nodes)
            rng.shuffle(links)
            tree = build_tree(nodes, links)
            self.assertEqual({k: tree.session_tree(k) for k in tree.session_keys()}, expected)


class ClaudeRulesTest(unittest.TestCase):
    def test_parent_agent_id_looked_up_project_wide(self):
        nodes = [node("main"), node("main", session="s2"), node("aP", session="s2", meta={}), node("aC", meta={"parentAgentId": "aP", "spawnDepth": 2})]
        tree = build_tree(nodes, [])
        self.assertEqual(tree.nodes[S + ":aC"].parent_key, "claude:s2:aP")
        self.assertEqual(tree.nodes[S + ":aC"].depth, 2)

    def test_missing_parent_gets_placeholder(self):
        tree = build_tree([node("main"), node("aC", meta={"parentAgentId": "aGone", "spawnDepth": 2})], [])
        placeholder = tree.nodes[S + ":aGone"]
        self.assertTrue(placeholder.missing)
        self.assertEqual((placeholder.parent_key, placeholder.linked_by), (S + ":main", "orphan"))
        self.assertEqual(sorted(placeholder.warnings), ["missing", "orphan"])
        self.assertEqual(tree.nodes[S + ":aC"].parent_key, S + ":aGone")
        self.assertEqual(tree.nodes[S + ":aC"].warnings, ())

    def test_spawn_link_from_unknown_agent_gets_placeholder(self):
        tree = build_tree([node("main"), node("aC")], [spawn(S + ":aGone", "aC")])
        self.assertEqual(tree.nodes[S + ":aC"].parent_key, S + ":aGone")
        self.assertEqual(tree.nodes[S + ":aC"].linked_by, "transcript")
        self.assertTrue(tree.nodes[S + ":aGone"].missing)

    def test_missing_main_gets_placeholder(self):
        tree = build_tree([node("aX", meta={})], [])
        self.assertTrue(tree.nodes[S + ":main"].missing)
        self.assertEqual(tree.nodes[S + ":main"].children, (S + ":aX",))

    def test_parent_mismatch_warning(self):
        nodes = [node("main"), node("aP", meta={}), node("aC", meta={})]
        tree = build_tree(nodes, [spawn(S + ":aP", "aC")])
        self.assertEqual(tree.nodes[S + ":aC"].parent_key, S + ":main")
        self.assertEqual(tree.nodes[S + ":aC"].warnings, ("parent_mismatch",))

    def test_continuation_spawn_seen_in_successor(self):
        nodes = [node("main"), node("main", session="s2"), node("aC", meta={"toolUseId": "t1"})]
        tree = build_tree(nodes, [spawn("claude:s2:main", "aC", tool_call_id="t1", at=5)])
        child = tree.nodes[S + ":aC"]
        self.assertEqual((child.parent_key, child.warnings, child.spawn_seen_in), (S + ":main", (), "claude:s2:main"))
        self.assertEqual(child.spawned_at, ts(5))

    def test_link_in_own_session_preferred_over_successor_copy(self):
        nodes = [node("main"), node("main", session="s2"), node("aC")]
        links = [spawn("claude:s2:main", "aC", at=1), spawn(S + ":main", "aC", at=9)]
        child = build_tree(nodes, links).nodes[S + ":aC"]
        self.assertEqual((child.parent_key, child.linked_by, child.spawn_seen_in), (S + ":main", "transcript", None))

    def test_cycle_is_broken(self):
        nodes = [node("main"), node("aA", meta={"parentAgentId": "aB"}), node("aB", meta={"parentAgentId": "aA"})]
        tree = build_tree(nodes, [])
        warned = [k for k, n in tree.nodes.items() if "cycle" in n.warnings]
        self.assertEqual(len(warned), 1)
        self.assertEqual(tree.nodes[warned[0]].parent_key, S + ":main")
        for key in (S + ":aA", S + ":aB"):
            self.assertIn(tree.nodes[key].depth, (1, 2))

    def test_self_parent_is_a_cycle(self):
        tree = build_tree([node("main"), node("aA", meta={"parentAgentId": "aA"})], [])
        self.assertEqual((tree.nodes[S + ":aA"].parent_key, tree.nodes[S + ":aA"].warnings), (S + ":main", ("cycle",)))

    def test_depth_mismatch(self):
        tree = build_tree([node("main"), node("aA", meta={"spawnDepth": 3})], [])
        self.assertEqual(tree.nodes[S + ":aA"].warnings, ("depth_mismatch",))

    def test_parallel_spawns_ordered_by_call_time_then_key(self):
        nodes = [node("main"), node("aZ", meta={"toolUseId": "t1"}), node("aY", meta={"toolUseId": "t2"}),
                 node("aB", meta={"toolUseId": "t3"}), node("aA", meta={"toolUseId": "t4"})]
        links = [spawn(S + ":main", "aZ", "t1", at=1), spawn(S + ":main", "aY", "t2", at=2),
                 spawn(S + ":main", "aB", "t3", at=3), spawn(S + ":main", "aA", "t4", at=3)]
        tree = build_tree(nodes, links)
        self.assertEqual(tree.nodes[S + ":main"].children, (S + ":aZ", S + ":aY", S + ":aA", S + ":aB"))

    def test_resume_never_reparents_and_is_deduplicated(self):
        nodes = [node("main"), node("main", session="s2"), node("aA", meta={})]
        resume = dict(kind="resume", to_agent_id="aA", tool_call_id="m1", timestamp=ts(4), evidence={})
        links = [Link(from_key=S + ":main", **resume), Link(from_key="claude:s2:main", **resume)]
        child = build_tree(nodes, links).nodes[S + ":aA"]
        self.assertEqual(child.parent_key, S + ":main")
        self.assertEqual([(r.by_agent_key, r.tool_call_id) for r in child.resumes], [(S + ":main", "m1")])

    def test_meta_fields_are_carried(self):
        meta = {"agentType": "fork", "name": "n", "isFork": True, "stoppedByUser": True, "description": "d"}
        tree = build_tree([node("main"), node("aF", meta=meta, forked_skill={"skillName": "code-review"})], [])
        agent = tree.nodes[S + ":aF"]
        self.assertEqual((agent.agent_type, agent.name, agent.is_fork, agent.stopped_by_user, agent.forked_skill, agent.description),
                         ("fork", "n", True, True, "code-review", "d"))

    def test_bad_inputs_do_not_raise(self):
        weird = [node("main"), node("aA", meta={"parentAgentId": 5, "spawnDepth": "x", "toolUseId": []}),
                 NodeInput(source="claude", session_id="s-main", agent_id="aB")]
        build_tree(weird, [Link(kind="spawn", from_key="nonsense", to_agent_id="aB", tool_call_id=None, timestamp=None, evidence={})])


class OmpRulesTest(unittest.TestCase):
    def omp(self, agent_id, file, parent_session=None):
        return NodeInput(source="omp", session_id="o", agent_id=agent_id, project="-p", file=file, parent_session=parent_session)

    def test_folder_fallback_and_orphan(self):
        nodes = [self.omp("main", "/r/-p/o.jsonl"),
                 self.omp("A", "/r/-p/o/A.jsonl", parent_session="/does/not/exist.jsonl"),
                 self.omp("A/B", "/r/-p/o/A/B.jsonl"),
                 self.omp("X/Y", "/r/-p/o/X/Y.jsonl")]
        tree = build_tree(nodes, [])
        self.assertEqual((tree.nodes["omp:o:A"].parent_key, tree.nodes["omp:o:A"].linked_by), ("omp:o:main", "folder"))
        self.assertEqual((tree.nodes["omp:o:A/B"].parent_key, tree.nodes["omp:o:A/B"].depth), ("omp:o:A", 2))
        orphan = tree.nodes["omp:o:X/Y"]
        self.assertEqual((orphan.parent_key, orphan.linked_by, orphan.warnings), ("omp:o:main", "orphan", ("orphan",)))
        self.assertEqual(tree.nodes["omp:o:A/B"].name, "B")

    def test_parent_session_wins_and_spawn_link_matched_by_stem(self):
        nodes = [self.omp("main", "/r/-p/o.jsonl"), self.omp("A", "/r/-p/o/A.jsonl"),
                 self.omp("A/B", "/r/-p/o/A/B.jsonl", parent_session="/r/-p/o.jsonl")]
        link = Link(kind="spawn", from_key="omp:o:main", to_agent_id="B", tool_call_id="c1", timestamp=None,
                    evidence={"agentType": "reviewer", "description": "why"})
        child = build_tree(nodes, [link]).nodes["omp:o:A/B"]
        self.assertEqual((child.parent_key, child.linked_by, child.agent_type, child.spawn_tool_call_id),
                         ("omp:o:main", "parentSession", "reviewer", "c1"))


if __name__ == "__main__":
    unittest.main()
