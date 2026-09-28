"""WS3: optional browser smoke test (SPEC 13.5).

Skipped unless Playwright for Python and its Chromium are installed:
    pip install playwright && python3 -m playwright install chromium
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "tests"))

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # optional dev dependency
    sync_playwright = None

try:
    from fixtures import loader
except ImportError:  # WS1 fixture loader not importable yet
    loader = None

TIMEOUT_MS = 10000
LISTENING = "listening on http://127.0.0.1:"


@unittest.skipIf(sync_playwright is None, "Playwright for Python is not installed")
@unittest.skipIf(loader is None, "WS1 fixture loader not available")
class UiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = loader.copy_fixtures()
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "agent_viewer", "--port", "0",
             "--claude-root", os.path.join(cls.root, "claude"),
             "--omp-root", os.path.join(cls.root, "omp")],
            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first = cls.proc.stdout.readline().decode("utf-8").strip()
        if not first.startswith(LISTENING):
            cls.proc.kill()
            raise AssertionError("unexpected first line %r" % first)
        cls.base = "http://127.0.0.1:%d" % int(first[len(LISTENING):])
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:  # Chromium not downloaded
            cls.pw.stop()
            cls.proc.kill()
            loader.remove_fixtures(cls.root)
            raise unittest.SkipTest("Playwright Chromium is not available: %s" % exc)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        if cls.proc.poll() is None:
            cls.proc.kill()
            cls.proc.wait(5)
        cls.proc.stdout.close()
        cls.proc.stderr.close()
        loader.remove_fixtures(cls.root)

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 800})
        self.page.set_default_timeout(TIMEOUT_MS)
        self.problems = []
        self.dialogs = []
        self.page.on("console", lambda m: m.type in ("error", "warning") and self.problems.append(m.text))
        self.page.on("pageerror", lambda e: self.problems.append(str(e)))
        self.page.on("dialog", self._on_dialog)

    def tearDown(self):
        self.page.close()

    def _on_dialog(self, dialog):
        self.dialogs.append(dialog.message)
        dialog.dismiss()

    def test_list_tree_timeline_and_escaping(self):
        page = self.page
        # The fixtures are days old, so ask for every status, not just recent ones.
        page.goto(self.base + "/#/?status=all")
        page.wait_for_function("document.querySelectorAll('tr.session-row').length === 3")

        page.click("tr.session-row[data-key='claude:s-main'] .col-title a")
        page.wait_for_function("document.querySelectorAll('.tree-node').length === 5")
        types = page.eval_on_selector_all(".tree-node .node-type", "els => els.map(e => e.textContent)")
        self.assertEqual(types, ["main", "general-purpose", "general-purpose", "ecc:code-explorer", "unknown type"])
        self.assertEqual(page.text_content(".tree-panel .session-id code"), "s-main")
        self.assertIsNotNone(page.query_selector(".tree-node[data-key$='a4444444444444444'] .warn"))

        # The main timeline holds the fixture's <img onerror> and <script> text.
        page.wait_for_selector("#timeline .ev-prompt")
        self.assertIn("<img src=x onerror=alert(1)>", page.text_content("#timeline"))
        page.wait_for_function("document.querySelector('#timeline').textContent.includes('<script>alert(1)</script>')")
        self.assertEqual(page.eval_on_selector_all("#timeline img, #timeline script", "els => els.length"), 0)

        page.click(".tree-node[data-key$='a2222222222222222']")
        page.wait_for_selector("#timeline .ev-tool summary")
        page.click("#timeline .ev-tool summary")
        result = page.wait_for_selector("#timeline .tool-section.is-error pre")
        self.assertEqual(result.text_content(), "Exit code 1")
        self.assertEqual(page.text_content("#timeline .tool summary .chip-error"), "error")

        self.assertEqual(page.eval_on_selector_all("#timeline img, #timeline script", "els => els.length"), 0)
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.problems, [])

    def _graph_url(self, path=""):
        return self.base + "/#/s/claude/s-main" + path

    def _focused_card(self):
        return self.page.evaluate("document.activeElement && document.activeElement.dataset.id || null")

    def test_graph_layout_drawer_and_tool_filter(self):
        """FEATURE-graph-canvas 11.4 steps 1 to 6, 8 and 9."""
        page = self.page
        page.goto(self._graph_url())
        page.wait_for_selector(".tree-node")
        self.assertEqual(page.get_attribute(".layout-btn[data-layout=tree]", "aria-pressed"), "true")

        # 1. The toggle switches to the graph and the URL carries it.
        page.click(".layout-btn[data-layout=graph]")
        page.wait_for_function("location.hash === '#/s/claude/s-main?layout=graph'")
        page.wait_for_function("document.querySelectorAll('.gcard').length === 5")
        self.assertIsNone(page.query_selector(".tree-panel"))

        # 2. Orphan warning and one resume edge.
        self.assertIsNotNone(page.query_selector(".gcard[data-id$='a4444444444444444'] .gcard-warn"))
        self.assertEqual(page.eval_on_selector_all(".gedge-group[data-id^='resume|']", "els => els.length"), 1)

        # 3. Selecting a2222222 shows its Bash pill with one call and one error.
        page.click(".gcard[data-id$='a2222222222222222']")
        bash = ".gtool[data-id$='a2222222222222222|Bash']"
        page.wait_for_selector(bash)
        self.assertEqual(page.text_content(bash + " .gpill-count"), "x1")
        self.assertEqual(page.text_content(bash + " .gpill-err"), "1 err")

        # 4. The click opened the drawer on its timeline.
        page.wait_for_function("location.hash.endsWith('/a/a2222222222222222?layout=graph&drawer=timeline')")
        page.wait_for_selector(".drawer #timeline .ev-tool summary")
        page.click(".drawer #timeline .ev-tool summary")
        self.assertEqual(page.text_content(".drawer #timeline .tool-section.is-error pre"), "Exit code 1")

        # 4a. The Bash pill filters the drawer timeline, survives a reload, and clears.
        page.click(bash)
        page.wait_for_function("location.hash.endsWith('drawer=timeline&tool=Bash')")
        page.wait_for_selector(".filter-chip")
        self.assertEqual(page.text_content(".filter-chip > span"), "filtered: Bash")
        page.wait_for_function("document.querySelectorAll('#timeline > li').length === 1")
        self.assertEqual(page.eval_on_selector_all("#timeline .tool-name", "els => els.map(e => e.textContent)"), ["Bash"])
        page.reload()
        page.wait_for_selector(".filter-chip")
        page.wait_for_selector("#timeline .ev-tool")
        page.click(".filter-clear")
        page.wait_for_function("!location.hash.includes('tool=')")
        page.wait_for_function("document.querySelector('.drawer-filter').hidden")

        # 5. Keyboard: Up to a1111111, Left to main, Escape closes and refocuses.
        page.focus(".gcard[data-id$='a2222222222222222']")
        page.keyboard.press("ArrowUp")
        page.wait_for_function("location.hash.includes('/a/a1111111111111111')")
        for _ in range(2):  # Left collapses an expanded card first, then moves to the parent.
            if (self._focused_card() or "").endswith(":main"):
                break
            page.keyboard.press("ArrowLeft")
        page.wait_for_function("location.hash.startsWith('#/s/claude/s-main?')")
        page.keyboard.press("Escape")
        page.wait_for_function("document.querySelector('.drawer').hidden && !location.hash.includes('drawer=')")
        self.assertTrue(self._focused_card().endswith(":main"))

        # 6. Reload restores the graph layout and the selected agent.
        page.click(".gcard[data-id$='a3333333333333333']")
        page.wait_for_function("location.hash.includes('/a/a3333333333333333')")
        page.reload()
        page.wait_for_selector(".gcard[data-id$='a3333333333333333'][aria-selected=true]")
        self.assertEqual(page.get_attribute(".layout-btn[data-layout=graph]", "aria-pressed"), "true")

        # 8 and 9. Fixture markup stays text, no dialog, no console or CSP problem.
        page.goto(self._graph_url("?layout=graph&drawer=timeline"))
        page.wait_for_function("document.querySelector('#timeline').textContent.includes('<script>alert(1)</script>')")
        self.assertEqual(page.eval_on_selector_all(".graph-row img, .graph-row script", "els => els.length"), 0)
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.problems, [])


if __name__ == "__main__":
    unittest.main()
