"""AgentState: applies ParseResults for one agent file (SPEC 12, 14.5.1).

Keeps a compact event index, the tool calls, usage by message id, links, a
compacted list of status signals and incrementally maintained stats, so a
stats request is a copy, not a rescan. It never opens files: the caller feeds
complete lines with their byte offsets.
"""
from dataclasses import replace

from .model import NodeInput, SkillInvocation, Stats, ToolCall, duration_ms, empty_errors, empty_tokens, split_key
from .parse_common import ParseResult, ParseStats, decode_line

TITLE_PROMPT_CHARS = 120
FIRST_WINS_META = frozenset(("cwd", "firstPrompt", "sessionId", "parentSession"))
CONVERSATIONAL_SIGNALS = frozenset(("user", "assistant", "tool_result", "session_exit"))
SKILL_VIAS = ("tool", "slash", "fork", "read", "preload")


def resolve_title(source, meta):
    """Session title rules of SPEC 5.2."""
    if source == "omp":
        return meta.get("titleChange") or meta.get("sessionTitle") or meta.get("titleRecord")
    prompt = meta.get("firstPrompt")
    return meta.get("customTitle") or meta.get("aiTitle") or (prompt[:TITLE_PROMPT_CHARS] if prompt else None)


class AgentState:
    def __init__(self, agent_key, parser, file=None):
        self.agent_key = agent_key
        self.parser = parser
        self.file = file
        parts = split_key(agent_key) or (getattr(parser, "source", ""), "", "main")
        self.source, self.session_id, self.agent_id = parts
        self.reset()

    def reset(self):
        """Forget everything (file shrank or was replaced) and start again from offset 0."""
        self.parser.reset()
        self.parse_stats = ParseStats(file=self.file)
        self.links = []
        self.skills = []
        self.usages = {}
        self.tool_calls = {}
        self._events = []
        self._models = []
        self._meta = {}
        self._first_ts = None
        self._last_ts = None
        self._pending = {}           # tool_call_id -> StatusSignal("tool_call")
        self._notifications = []     # task_notification signals
        self._last_conversational = None
        self._final_messages = set()
        self._tokens = empty_tokens()
        self._cost = None
        self._by_model = {}
        self._tools = {}
        self._skill_stats = {}
        self._slash = {}
        self._subagent_types = {}
        self._errors = empty_errors()
        self._active_ms = None

    # -- feeding ----------------------------------------------------------

    def feed_line(self, raw, offset, line_no):
        """Decode, parse and apply one complete line; returns what it contributed."""
        record = decode_line(raw, line_no, offset, self.parse_stats)
        if record is None:
            return ParseResult()
        result = self.parser.parse_record(record, offset, len(raw), line_no)
        if result.error:
            self.parse_stats.parsed -= 1
            self.parse_stats.skip(result.error, line_no, offset)
            return result
        if result.unknown_type:
            self.parse_stats.count_unknown(result.unknown_type)
        self._apply(result)
        return result

    def add_forked_skill(self, skill, timestamp=None):
        """Record a forked-skill invocation read from `agent-<id>.forked-skill.json` (SPEC 5.6 `fork`)."""
        invocation = SkillInvocation(skill, "fork", self.agent_key, timestamp, None, None)
        self.skills.append(invocation)
        self._count_skill(invocation)

    def _apply(self, res):
        self._track_time(res)
        for event in res.events:
            self._events.append(event)
            if event.role == "assistant" and event.model and event.model != "<synthetic>" and event.model not in self._models:
                self._models.append(event.model)
        for update in res.tool_call_updates:
            self._apply_tool_update(update)
        for usage in res.usages:
            self._apply_usage(usage)
        for invocation in res.skills:
            self.skills.append(invocation)
            self._count_skill(invocation)
        for name in res.slash_commands:
            self._slash[name] = self._slash.get(name, 0) + 1
        for name in res.subagent_types:
            self._subagent_types[name] = self._subagent_types.get(name, 0) + 1
        self.links.extend(res.links)
        for signal in res.status_signals:
            self._apply_signal(signal)
        for key, value in res.session_meta.items():
            if key not in FIRST_WINS_META or key not in self._meta:
                self._meta[key] = value
        if res.active_duration_ms is not None:
            self._active_ms = (self._active_ms or 0) + res.active_duration_ms
        self._errors["apiErrors"] += res.api_errors
        self._errors["aborted"] += res.aborted

    def _track_time(self, res):
        stamps = [e.timestamp for e in res.events if e.timestamp is not None]
        stamps += [s.timestamp for s in res.status_signals if s.timestamp is not None]
        if not stamps:
            return
        if self._first_ts is None:
            self._first_ts = stamps[0]
        latest = max(stamps)
        if self._last_ts is None or latest > self._last_ts:
            self._last_ts = latest

    def _apply_tool_update(self, update):
        call = self.tool_calls.get(update.tool_call_id)
        if update.kind == "start":
            if call is None:
                call = ToolCall(id=update.tool_call_id, agent_key=self.agent_key, name=update.name or "unknown",
                                input=update.input or {}, intent=update.intent, started_at=update.timestamp,
                                call_seq=update.seq)
                self.tool_calls[call.id] = call
                self._tool_entry(call.name)["calls"] += 1
            return
        if update.kind == "started_at":
            if call is not None and update.timestamp is not None:
                self._set_call(call, replace(call, started_at=update.timestamp,
                                             duration_ms=duration_ms(update.timestamp, call.ended_at)))
            return
        if update.is_error:
            self._errors["toolErrors"] += 1
        if call is None:
            if update.is_error and update.name:
                self._tool_entry(update.name)["errors"] += 1
            return
        if update.is_error:
            self._tool_entry(call.name)["errors"] += 1
        self._set_call(call, replace(call, ended_at=update.timestamp, result_seq=update.seq,
                                     duration_ms=duration_ms(call.started_at, update.timestamp),
                                     status="error" if update.is_error else "ok",
                                     spawned_agent_key=update.spawned_agent_key or call.spawned_agent_key))

    def _set_call(self, old, new):
        entry = self._tool_entry(old.name)
        entry["totalDurationMs"] += max(new.duration_ms or 0, 0) - max(old.duration_ms or 0, 0)
        self.tool_calls[new.id] = new

    def _tool_entry(self, name):
        return self._tools.setdefault(name, {"calls": 0, "errors": 0, "totalDurationMs": 0})

    def _apply_usage(self, usage):
        previous = self.usages.get(usage.message_id)
        if previous is not None:
            self.parse_stats.usage_conflicts += 1
            self._add_usage(previous, -1)
        self.usages[usage.message_id] = usage
        self._add_usage(usage, 1)

    def _add_usage(self, usage, sign):
        t = self._tokens
        t["input"] += sign * usage.input_tokens
        t["output"] += sign * usage.output_tokens
        t["cacheRead"] += sign * usage.cache_read_tokens
        t["cacheCreation"] += sign * usage.cache_creation_tokens
        t["reasoning"] += sign * usage.reasoning_tokens
        t["total"] = t["input"] + t["output"] + t["cacheRead"] + t["cacheCreation"]
        if usage.cost_usd is not None:
            self._cost = (self._cost or 0.0) + sign * usage.cost_usd
        model = self._by_model.setdefault(usage.model or "unknown",
                                          {"messages": 0, "input": 0, "output": 0, "cacheRead": 0, "cacheCreation": 0})
        model["messages"] += sign
        model["input"] += sign * usage.input_tokens
        model["output"] += sign * usage.output_tokens
        model["cacheRead"] += sign * usage.cache_read_tokens
        model["cacheCreation"] += sign * usage.cache_creation_tokens

    def _count_skill(self, invocation):
        entry = self._skill_stats.setdefault(invocation.skill, {"count": 0, "via": {v: 0 for v in SKILL_VIAS}})
        entry["count"] += 1
        entry["via"][invocation.via] = entry["via"].get(invocation.via, 0) + 1

    def _apply_signal(self, signal):
        if signal.kind == "tool_call":
            if signal.tool_call_id:
                self._pending[signal.tool_call_id] = signal
            return
        if signal.kind == "task_notification":
            self._notifications.append(signal)
            return
        if signal.kind == "tool_result" and signal.tool_call_id:
            self._pending.pop(signal.tool_call_id, None)
        if signal.kind == "assistant" and signal.final and signal.message_id:
            self._final_messages.add(signal.message_id)
        if signal.kind in CONVERSATIONAL_SIGNALS:
            self._last_conversational = signal

    # -- reading ----------------------------------------------------------

    @property
    def status_signals(self):
        """Compacted signals: pending tool calls, task notifications, then the last conversational record."""
        out = list(self._pending.values()) + list(self._notifications)
        if self._last_conversational is not None:
            out.append(self._last_conversational)
        return out

    @property
    def event_count(self):
        return len(self._events)

    @property
    def models(self):
        return list(self._models)

    @property
    def first_timestamp(self):
        return self._first_ts

    @property
    def last_activity_at(self):
        return self._last_ts

    @property
    def tokens_total(self):
        return self._tokens["total"]

    @property
    def session_meta(self):
        """Merged session metadata with a resolved `title`, `startedAt` and `lastActivityAt`."""
        meta = dict(self._meta)
        meta["title"] = resolve_title(self.source, self._meta)
        meta["startedAt"] = self._first_ts
        meta["lastActivityAt"] = self._last_ts
        return meta

    def _decorated(self, event):
        if event.kind not in ("tool_call", "skill") or not event.tool_call_id:
            return event
        call = self.tool_calls.get(event.tool_call_id)
        if call is None:
            return event
        return replace(event, result_seq=call.result_seq, duration_ms=call.duration_ms,
                       spawned_agent_key=call.spawned_agent_key)

    def all_events(self):
        return [self._decorated(e) for e in self._events]

    def events(self, before, after, limit, kinds, include_meta, tool_name=None):
        """One page of events: `before`/`after` are exclusive seqs; neither means the last page.

        `tool_name` keeps only events whose tool name equals it exactly, before paging.
        """
        limit = max(int(limit or 0), 0)
        if limit == 0:
            return []

        def wanted(event):
            if tool_name is not None and event.tool_name != tool_name:
                return False
            if kinds:
                return event.kind in kinds
            return include_meta or event.kind != "meta"

        page = []
        if after is not None:
            for event in self._events[max(after + 1, 0):]:
                if wanted(event):
                    page.append(event)
                    if len(page) == limit:
                        break
        else:
            end = len(self._events) if before is None else max(min(before, len(self._events)), 0)
            for event in reversed(self._events[:end]):
                if wanted(event):
                    page.append(event)
                    if len(page) == limit:
                        break
            page.reverse()
        return [self._decorated(e) for e in page]

    def pending_by_tool(self):
        """Tool calls still waiting for a result, counted per tool name, from the pending index."""
        counts = {}
        for tool_call_id in self._pending:
            call = self.tool_calls.get(tool_call_id)
            name = call.name if call is not None else "unknown"
            counts[name] = counts.get(name, 0) + 1
        return counts

    def event_ref(self, seq):
        """Location of the source line of event `seq`, with the file filled in; None when unknown."""
        if not isinstance(seq, int) or not 0 <= seq < len(self._events):
            return None
        ref = self._events[seq].ref
        return replace(ref, file=self.file) if ref is not None else None

    def full_content(self, seq, record):
        """SPEC 9.5 body for event `seq`, given its re-read source record."""
        if not isinstance(seq, int) or not 0 <= seq < len(self._events):
            return None
        event = self._events[seq]
        body = self.parser.full_content(record, event.ref.block if event.ref else 0)
        return dict({"seq": seq, "kind": event.kind, "toolCallId": event.tool_call_id, "isError": event.is_error}, **body)

    def stats(self):
        """Stats of this agent alone (scope `agent`); `agents` is completed by `stats.agent_stats`."""
        return Stats(
            scope="agent", tokens=dict(self._tokens), cost_usd=self._cost,
            by_model={k: dict(v) for k, v in self._by_model.items() if v["messages"] > 0},
            tools={k: dict(v) for k, v in self._tools.items()},
            skills={k: {"count": v["count"], "via": dict(v["via"])} for k, v in self._skill_stats.items()},
            slash_commands=dict(self._slash), subagent_types=dict(self._subagent_types),
            agents={"total": 1, "maxDepth": 0, "running": 0}, active_duration_ms=self._active_ms,
            errors=dict(self._errors), turns=len(self._final_messages),
            parse={"lines": self.parse_stats.lines, "skipped": self.parse_stats.skipped,
                   "unknownTypes": dict(self.parse_stats.unknown_types)},
            started_at=self._first_ts, last_activity_at=self._last_ts)

    def node_input(self, project="", status="finished", meta=None, forked_skill=None, missing=False, session_id=None):
        """NodeInput for `tree.build_tree` from this state plus what the caller knows (sidecars, status)."""
        return NodeInput(
            source=self.source, session_id=session_id or self.session_id, agent_id=self.agent_id,
            file=self.file or "", project=project, meta=meta, forked_skill=forked_skill,
            parent_session=self._meta.get("parentSession"), started_at=self._first_ts,
            last_activity_at=self._last_ts, status=status, models=tuple(self._models),
            event_count=len(self._events), tokens_total=self._tokens["total"], missing=missing)
