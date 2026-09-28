"""WS1: every row of the status table in SPEC 7.3, with an injected `now`."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
import loader  # noqa: E402,F401

from agent_viewer.model import StatusSignal  # noqa: E402
from agent_viewer.status import StatusConfig, decide_status, session_status, signals_for_agent  # noqa: E402

NOW = 1_000_000.0
CFG = StatusConfig(idle_window=1800, stale_after=600)


def sig(kind, **kw):
    return StatusSignal(kind=kind, timestamp=None, **kw)


def status(signals, age, is_root=False):
    return decide_status(signals, NOW - age, NOW, is_root, CFG)


FINAL = sig("assistant", final=True, message_id="m")
PENDING = [sig("tool_call", tool_call_id="t1"), sig("assistant", final=False, message_id="m")]


class DecideStatusTest(unittest.TestCase):
    def test_omp_session_exit_is_finished_even_when_fresh(self):
        self.assertEqual(status([sig("user"), sig("session_exit")], 1, is_root=True), "finished")

    def test_messages_after_session_exit_reopen_the_session(self):
        self.assertEqual(status([sig("session_exit"), sig("user")], 1, is_root=True), "running")

    def test_task_notification_finishes_subagent(self):
        signals = PENDING + [sig("task_notification", agent_id="a1", status="completed")]
        self.assertEqual(status(signals, 1), "finished")

    def test_running_task_notification_does_not_finish(self):
        signals = PENDING + [sig("task_notification", agent_id="a1", status="running")]
        self.assertEqual(status(signals, 1), "running")

    def test_stopped_by_user_finishes_subagent(self):
        self.assertEqual(status(PENDING + [sig("stopped_by_user")], 1), "finished")

    def test_notification_and_stop_do_not_apply_to_root(self):
        self.assertEqual(status(PENDING + [sig("stopped_by_user")], 1, is_root=True), "running")

    def test_final_answer_finishes_subagent(self):
        self.assertEqual(status([sig("user"), FINAL], 1), "finished")

    def test_final_answer_main_is_idle_then_finished(self):
        self.assertEqual(status([FINAL], 1799, is_root=True), "idle")
        self.assertEqual(status([FINAL], 1800, is_root=True), "finished")

    def test_final_answer_with_pending_tool_call_is_mid_turn(self):
        signals = [sig("tool_call", tool_call_id="t1"), FINAL]
        self.assertEqual(status(signals, 1, is_root=True), "running")

    def test_mid_turn_running_then_stale(self):
        for last in (sig("user"), sig("tool_result", tool_call_id="t1")):
            self.assertEqual(status([last], 599), "running")
            self.assertEqual(status([last], 600), "stale")
        self.assertEqual(status(PENDING, 599, is_root=True), "running")
        self.assertEqual(status(PENDING, 600, is_root=True), "stale")

    def test_resolved_tool_call_is_not_pending(self):
        signals = [sig("tool_call", tool_call_id="t1"), sig("tool_result", tool_call_id="t1"), FINAL]
        self.assertEqual(status(signals, 1), "finished")

    def test_no_conversational_record(self):
        self.assertEqual(status([], 10), "running")
        self.assertEqual(status([], 600), "finished")

    def test_default_config(self):
        self.assertEqual(decide_status([FINAL], NOW - 60, NOW, True), "idle")


class HelpersTest(unittest.TestCase):
    def test_signals_for_agent_picks_matching_notifications_and_meta_stop(self):
        own = [sig("user")]
        session = [sig("task_notification", agent_id="aX", status="completed"),
                   sig("task_notification", agent_id="aY", status="completed")]
        picked = signals_for_agent("aX", own, session, {"stoppedByUser": True})
        self.assertEqual([(s.kind, s.agent_id) for s in picked],
                         [("user", None), ("task_notification", "aX"), ("stopped_by_user", None)])
        self.assertEqual(signals_for_agent("aZ", own, session, None), own)

    def test_session_status_precedence(self):
        self.assertEqual(session_status(["finished", "running", "stale"], "idle"), "running")
        self.assertEqual(session_status(["finished", "stale"], "idle"), "idle")
        self.assertEqual(session_status(["finished", "stale"], "finished"), "stale")
        self.assertEqual(session_status(["finished"], "finished"), "finished")


if __name__ == "__main__":
    unittest.main()
