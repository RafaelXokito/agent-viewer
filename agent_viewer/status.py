"""Agent and session status rules (SPEC 7.3), as pure functions."""
from dataclasses import dataclass

from .model import StatusSignal

IDLE_WINDOW_S = 30 * 60
STALE_AFTER_S = 10 * 60
CONVERSATIONAL = frozenset(("user", "assistant", "tool_result", "session_exit"))
STATUS_ORDER = ("running", "idle", "stale", "finished")


@dataclass(frozen=True)
class StatusConfig:
    idle_window: float = IDLE_WINDOW_S
    stale_after: float = STALE_AFTER_S


def decide_status(signals, mtime, now, is_root, config=None):
    """Status of one agent: `running`, `idle`, `finished` or `stale`.

    `signals` are the agent's own signals (as `AgentState.status_signals`
    gives them) plus, for a Claude subagent, the matching `task_notification`
    and `stopped_by_user` signals (see `signals_for_agent`). `mtime` and `now`
    are epoch seconds. The first matching row of the SPEC 7.3 table wins.
    """
    config = config or StatusConfig()
    age = now - mtime
    last, pending, notified, stopped = _summarise(signals)
    if last is not None and last.kind == "session_exit":
        return "finished"
    if not is_root and (notified or stopped):
        return "finished"
    if last is not None and last.kind == "assistant" and last.final and not pending:
        if not is_root:
            return "finished"
        return "idle" if age < config.idle_window else "finished"
    if last is not None:
        return "running" if age < config.stale_after else "stale"
    return "running" if age < config.stale_after else "finished"


def _summarise(signals):
    last = None
    calls, results = set(), set()
    notified = stopped = False
    for signal in signals:
        kind = signal.kind
        if kind in CONVERSATIONAL:
            last = signal
        if kind == "tool_call" and signal.tool_call_id:
            calls.add(signal.tool_call_id)
        elif kind == "tool_result" and signal.tool_call_id:
            results.add(signal.tool_call_id)
        elif kind == "task_notification" and signal.status not in (None, "running"):
            notified = True
        elif kind == "stopped_by_user":
            stopped = True
    return last, calls - results, notified, stopped


def signals_for_agent(agent_id, own_signals, session_signals, meta=None):
    """Own signals plus the session's task notifications for `agent_id` and the sidecar's `stoppedByUser`."""
    out = list(own_signals)
    out.extend(s for s in session_signals if s.kind == "task_notification" and s.agent_id == agent_id)
    if isinstance(meta, dict) and meta.get("stoppedByUser") is True:
        out.append(StatusSignal("stopped_by_user"))
    return out


def session_status(agent_statuses, root_status):
    """`running` if any agent runs, else `idle` if the root is idle, else `stale` if any is stale, else `finished`."""
    statuses = set(agent_statuses) | {root_status}
    if "running" in statuses:
        return "running"
    if root_status == "idle":
        return "idle"
    if "stale" in statuses:
        return "stale"
    return "finished"
