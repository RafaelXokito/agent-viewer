"""WS2: watcher scheduling and CLI argument parsing."""
import argparse
import unittest

from agent_viewer.__main__ import build_parser, parse_duration
from agent_viewer.watcher import Watcher


class FakeStore:
    def __init__(self, backlog=0):
        self.calls = []
        self.backlog = backlog

    def scan(self):
        self.calls.append("scan")

    def poll(self, now, hot_only):
        self.calls.append("poll-hot" if hot_only else "poll-all")

    def refresh(self, now):
        self.calls.append("refresh")

    def index_step(self, budget):
        self.calls.append("index")
        self.backlog = max(0, self.backlog - 1)
        return self.backlog > 0


class WatcherTest(unittest.TestCase):
    def test_first_step_scans_and_polls_everything(self):
        store = FakeStore()
        Watcher(store).run_once(now=100.0)
        self.assertEqual(store.calls[:3], ["scan", "poll-all", "refresh"])

    def test_hot_poll_every_second_full_scan_every_five(self):
        store = FakeStore()
        watcher = Watcher(store)
        watcher.run_once(now=100.0)
        store.calls.clear()
        watcher.run_once(now=100.5)
        self.assertNotIn("scan", store.calls)
        self.assertNotIn("poll-hot", store.calls)
        watcher.run_once(now=101.1)
        self.assertIn("poll-hot", store.calls)
        self.assertNotIn("scan", store.calls)
        store.calls.clear()
        watcher.run_once(now=105.1)
        self.assertEqual(store.calls[:2], ["scan", "poll-all"])

    def test_reports_remaining_index_work_and_refreshes_between_slices(self):
        store = FakeStore(backlog=3)
        watcher = Watcher(store)
        self.assertTrue(watcher.run_once(now=100.0))
        store.calls.clear()
        self.assertTrue(watcher.run_once(now=100.2))
        self.assertEqual(store.calls, ["index", "refresh"])
        self.assertFalse(watcher.run_once(now=100.3))

    def test_thread_survives_store_errors(self):
        class Broken(FakeStore):
            def scan(self):
                raise RuntimeError("boom")
        watcher = Watcher(Broken())
        watcher.start()
        watcher.stop()
        self.assertGreaterEqual(watcher.errors, 1)


class CliTest(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("10s"), 10)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("45"), 45)
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_duration("soon")

    def test_defaults_and_no_host_flag(self):
        args = build_parser().parse_args([])
        self.assertEqual((args.port, args.idle_window, args.stale_after), (8765, 1800, 600))
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--host", "0.0.0.0"])


if __name__ == "__main__":
    unittest.main()
