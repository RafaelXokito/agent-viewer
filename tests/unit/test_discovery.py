"""WS1: file discovery under the Claude and Oh My Pi roots, and sidecar reading."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
import loader  # noqa: E402

from agent_viewer.discovery import discover, omp_session_id_from_stem, read_sidecar  # noqa: E402


class DiscoverFixturesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = os.path.realpath(loader.copy_fixtures())
        cls.files = discover(os.path.join(cls.root, "claude"), os.path.join(cls.root, "omp"))

    @classmethod
    def tearDownClass(cls):
        loader.remove_fixtures(cls.root)

    def summary(self):
        return [(f.source, f.kind, f.project, f.session_hint, f.agent_hint) for f in self.files]

    def test_all_fixture_files_are_found_with_hints(self):
        stem = "2026-09-22T09-58-35-301Z_o-root"
        self.assertEqual(sorted(self.summary()), sorted([
            ("claude", "main", "-tmp-broken", "s-broken", "main"),
            ("claude", "main", "-tmp-demo", "s-main", "main"),
            ("claude", "subagent", "-tmp-demo", "s-main", "a1111111111111111"),
            ("claude", "meta", "-tmp-demo", "s-main", "a1111111111111111"),
            ("claude", "subagent", "-tmp-demo", "s-main", "a2222222222222222"),
            ("claude", "meta", "-tmp-demo", "s-main", "a2222222222222222"),
            ("claude", "subagent", "-tmp-demo", "s-main", "a3333333333333333"),
            ("claude", "subagent", "-tmp-demo", "s-main", "a4444444444444444"),
            ("omp", "omp_root", "-tmp-demo", stem, "main"),
            ("omp", "omp_child", "-tmp-demo", stem, "Checker"),
        ]))

    def test_paths_are_absolute_realpaths_under_root(self):
        for f in self.files:
            self.assertTrue(os.path.isabs(f.path))
            self.assertTrue(f.path.startswith(self.root + os.sep))

    def test_output_is_sorted_and_stable(self):
        self.assertEqual(self.files, discover(os.path.join(self.root, "claude"), os.path.join(self.root, "omp")))

    def test_read_sidecar(self):
        meta = [f for f in self.files if f.kind == "meta" and f.agent_hint == "a2222222222222222"][0]
        self.assertEqual(read_sidecar(meta.path)["parentAgentId"], "a1111111111111111")


class DiscoverEdgeCasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        loader.remove_fixtures(self.tmp)

    def touch(self, rel, data=b"{}\n"):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_missing_roots_give_empty_lists(self):
        self.assertEqual(discover(None, None), [])
        self.assertEqual(discover(os.path.join(self.tmp, "nope"), os.path.join(self.tmp, "nope2")), [])

    def test_ignored_and_forked_skill_files(self):
        self.touch("c/-p/s1/subagents/agent-a1.forked-skill.json")
        self.touch("c/-p/s1/subagents/agent-a1.forked-skill.marker.json")
        self.touch("c/-p/s1/subagents/agent-aare-you-ablet-015ad.jsonl")
        self.touch("c/-p/s1/tool-results/x.jsonl")
        self.touch("c/-p/memory/MEMORY.md")
        self.touch("c/-p/notes.txt")
        kinds = sorted((f.kind, f.agent_hint) for f in discover(os.path.join(self.tmp, "c"), None))
        self.assertEqual(kinds, [("forked_skill", "a1"), ("subagent", "aare-you-ablet-015ad")])

    def test_symlink_escaping_root_is_skipped_and_reported(self):
        outside = self.touch("outside/secret.jsonl")
        os.makedirs(os.path.join(self.tmp, "c/-p"))
        os.symlink(outside, os.path.join(self.tmp, "c/-p/s9.jsonl"))
        skipped = []
        self.assertEqual(discover(os.path.join(self.tmp, "c"), None, skipped=skipped), [])
        self.assertEqual([reason for _p, reason in skipped], ["outside_root"])

    def test_nested_omp_children(self):
        self.touch("o/-p/2026_x-1.jsonl")
        self.touch("o/-p/2026_x-1/A.jsonl")
        self.touch("o/-p/2026_x-1/A/B.jsonl")
        self.touch("o/-p/2026_x-1/A.json")
        self.touch("o/-p/2026_x-1/1.bash.log")
        found = sorted((f.kind, f.session_hint, f.agent_hint) for f in discover(None, os.path.join(self.tmp, "o")))
        self.assertEqual(found, [("omp_child", "2026_x-1", "A"), ("omp_child", "2026_x-1", "A/B"), ("omp_root", "2026_x-1", "main")])

    def test_read_sidecar_never_raises(self):
        self.assertIsNone(read_sidecar(os.path.join(self.tmp, "none.json")))
        self.assertIsNone(read_sidecar(self.touch("bad.json", b"{nope")))
        self.assertIsNone(read_sidecar(self.touch("list.json", b"[1]")))
        self.assertIsNone(read_sidecar(self.touch("bin.json", b"\xff")))
        self.assertIsNone(read_sidecar(self.tmp))

    def test_omp_session_id_from_stem(self):
        self.assertEqual(omp_session_id_from_stem("2026-09-22T09-58-35-301Z_01a0c88d-8e25"), "01a0c88d-8e25")
        self.assertEqual(omp_session_id_from_stem("plain"), "plain")


if __name__ == "__main__":
    unittest.main()
