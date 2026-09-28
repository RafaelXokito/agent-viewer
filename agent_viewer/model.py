"""Normalized data model (SPEC 5).

Frozen dataclasses with snake_case attributes; `to_dict()` produces the
camelCase JSON shapes of SPEC 9. Timestamps are aware UTC `datetime` values in
Python and ISO 8601 strings with millisecond precision and `Z` in JSON.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def parse_ts(value):
    """Parse an ISO 8601 string into an aware UTC datetime; None if invalid."""
    if not isinstance(value, str):
        return None
    match = _TS_RE.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, frac, zone = match.groups()
    micros = int((frac or "0")[:6].ljust(6, "0"))
    try:
        dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micros, tzinfo=timezone.utc)
    except ValueError:
        return None
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        offset_minutes = sign * (int(digits[:2]) * 60 + int(digits[2:]))
        dt = dt - timedelta(minutes=offset_minutes)
    return dt


def format_ts(dt):
    """Format a datetime as `YYYY-MM-DDTHH:MM:SS.mmmZ`; None stays None."""
    if dt is None:
        return None
    dt = dt.astimezone(timezone.utc)
    return "%s.%03dZ" % (dt.strftime("%Y-%m-%dT%H:%M:%S"), dt.microsecond // 1000)


def duration_ms(start, end):
    if start is None or end is None:
        return None
    return int(round((end - start).total_seconds() * 1000))


def sort_ts(dt):
    """Sort key placing None last."""
    return (dt is None, dt.timestamp() if dt is not None else 0.0)


def split_key(key):
    """Split `source:sessionId:agentId`; the agentId may not contain ':'. None if malformed."""
    if not isinstance(key, str):
        return None
    parts = key.split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def agent_key(source, session_id, agent_id):
    return "%s:%s:%s" % (source, session_id, agent_id)


@dataclass(frozen=True)
class EventRef:
    file: Optional[str]
    offset: int
    length: int
    block: int
    line_no: int = 0

    def to_dict(self):
        return {"file": self.file, "offset": self.offset, "length": self.length, "block": self.block}


@dataclass(frozen=True)
class Event:
    seq: int
    agent_key: str
    kind: str
    timestamp: Optional[datetime] = None
    role: Optional[str] = None
    uuid: Optional[str] = None
    parent_uuid: Optional[str] = None
    message_id: Optional[str] = None
    model: Optional[str] = None
    preview: str = ""
    truncated: bool = False
    redacted: bool = False
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    is_error: bool = False
    spawned_agent_key: Optional[str] = None
    result_seq: Optional[int] = None
    duration_ms: Optional[int] = None
    ref: Optional[EventRef] = None

    def to_dict(self):
        """SPEC 9.3 shape; `ref` and `agentKey` are never sent to the browser."""
        out = {
            "seq": self.seq, "kind": self.kind, "timestamp": format_ts(self.timestamp), "role": self.role,
            "uuid": self.uuid, "parentUuid": self.parent_uuid, "messageId": self.message_id, "model": self.model,
            "preview": self.preview, "truncated": self.truncated, "redacted": self.redacted,
            "toolCallId": self.tool_call_id, "toolName": self.tool_name, "isError": self.is_error,
            "spawnedAgentKey": self.spawned_agent_key,
        }
        if self.kind == "tool_call":
            out["resultSeq"] = self.result_seq
            out["durationMs"] = self.duration_ms
        return out


@dataclass(frozen=True)
class ToolCall:
    id: str
    agent_key: str
    name: str
    input: dict
    intent: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    status: str = "pending"
    call_seq: Optional[int] = None
    result_seq: Optional[int] = None
    spawned_agent_key: Optional[str] = None

    def to_dict(self):
        return {
            "id": self.id, "agentKey": self.agent_key, "name": self.name, "input": self.input, "intent": self.intent,
            "startedAt": format_ts(self.started_at), "endedAt": format_ts(self.ended_at), "durationMs": self.duration_ms,
            "status": self.status, "callSeq": self.call_seq, "resultSeq": self.result_seq,
            "spawnedAgentKey": self.spawned_agent_key,
        }


@dataclass(frozen=True)
class ToolCallUpdate:
    """Start or finish of a ToolCall; kind is `start`, `finish` or `started_at`."""
    kind: str
    tool_call_id: str
    timestamp: Optional[datetime] = None
    seq: Optional[int] = None
    name: Optional[str] = None
    input: Optional[dict] = None
    intent: Optional[str] = None
    is_error: bool = False
    spawned_agent_key: Optional[str] = None


@dataclass(frozen=True)
class SkillInvocation:
    skill: str
    via: str
    agent_key: str
    timestamp: Optional[datetime] = None
    tool_call_id: Optional[str] = None
    args: Optional[str] = None

    def to_dict(self):
        return {"skill": self.skill, "via": self.via, "agentKey": self.agent_key, "timestamp": format_ts(self.timestamp),
                "toolCallId": self.tool_call_id, "args": self.args}


@dataclass(frozen=True)
class Usage:
    message_id: str
    model: Optional[str] = None
    timestamp: Optional[datetime] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None

    def counts(self):
        return (self.model, self.input_tokens, self.output_tokens, self.cache_read_tokens,
                self.cache_creation_tokens, self.reasoning_tokens, self.cost_usd)

    def to_dict(self):
        return {"messageId": self.message_id, "model": self.model, "timestamp": format_ts(self.timestamp),
                "inputTokens": self.input_tokens, "outputTokens": self.output_tokens,
                "cacheReadTokens": self.cache_read_tokens, "cacheCreationTokens": self.cache_creation_tokens,
                "reasoningTokens": self.reasoning_tokens, "costUsd": self.cost_usd, "durationMs": self.duration_ms}


@dataclass(frozen=True)
class Link:
    """Tree evidence (SPEC 6): kind is `spawn`, `resume`, `continued_in` or `parent_session`."""
    kind: str
    from_key: str
    to_agent_id: Optional[str]
    tool_call_id: Optional[str] = None
    timestamp: Optional[datetime] = None
    evidence: dict = field(default_factory=dict)

    def sort_key(self):
        return (self.kind, str(self.to_agent_id), sort_ts(self.timestamp), str(self.from_key),
                str(self.tool_call_id), repr(sorted(self.evidence.items(), key=repr)))


@dataclass(frozen=True)
class StatusSignal:
    """Input to `status.decide_status`.

    kind: `user`, `assistant` (with `final`), `tool_call`, `tool_result`,
    `task_notification` (with `agent_id`, `status`), `session_exit`,
    `stopped_by_user`.
    """
    kind: str
    timestamp: Optional[datetime] = None
    tool_call_id: Optional[str] = None
    agent_id: Optional[str] = None
    status: Optional[str] = None
    final: bool = False
    message_id: Optional[str] = None


@dataclass(frozen=True)
class Resume:
    by_agent_key: str
    tool_call_id: Optional[str]
    timestamp: Optional[datetime]

    def to_dict(self):
        return {"byAgentKey": self.by_agent_key, "toolCallId": self.tool_call_id, "timestamp": format_ts(self.timestamp)}


@dataclass(frozen=True)
class NodeInput:
    """One agent file (or known agent) handed to `tree.build_tree`.

    session_id is the owning session: for Claude the folder session, for Oh My
    Pi the root `session.id` of the top-level folder. `meta` and
    `forked_skill` are the parsed Claude sidecars (None when absent or bad).
    `parent_session` is the Oh My Pi `session.parentSession` path.
    """
    source: str
    session_id: str
    agent_id: str
    file: str = ""
    project: str = ""
    meta: Optional[dict] = None
    forked_skill: Optional[dict] = None
    parent_session: Optional[str] = None
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    status: str = "finished"
    models: Tuple[str, ...] = ()
    event_count: int = 0
    tokens_total: int = 0
    missing: bool = False

    @property
    def key(self):
        return agent_key(self.source, self.session_id, self.agent_id)


@dataclass(frozen=True)
class Agent:
    """Tree node (SPEC 5.3 plus `warnings`, `tokensTotal` and `spawnSeenIn`)."""
    key: str
    agent_id: str
    session_id: str
    source: str
    parent_key: Optional[str]
    depth: int
    agent_type: Optional[str]
    description: Optional[str]
    name: Optional[str]
    linked_by: str
    spawn_tool_call_id: Optional[str]
    spawned_at: Optional[datetime]
    last_activity_at: Optional[datetime]
    status: str
    stopped_by_user: bool
    is_fork: bool
    forked_skill: Optional[str]
    resumes: Tuple[Resume, ...]
    models: Tuple[str, ...]
    file: str
    event_count: int
    missing: bool
    children: Tuple[str, ...]
    warnings: Tuple[str, ...] = ()
    tokens_total: int = 0
    spawn_seen_in: Optional[str] = None
    project: str = ""

    def to_dict(self, include_file=True):
        out = {
            "key": self.key, "agentId": self.agent_id, "sessionId": self.session_id, "parentKey": self.parent_key,
            "depth": self.depth, "agentType": self.agent_type, "description": self.description, "name": self.name,
            "linkedBy": self.linked_by, "spawnToolCallId": self.spawn_tool_call_id,
            "spawnedAt": format_ts(self.spawned_at), "lastActivityAt": format_ts(self.last_activity_at),
            "status": self.status, "stoppedByUser": self.stopped_by_user, "isFork": self.is_fork,
            "forkedSkill": self.forked_skill, "resumes": [r.to_dict() for r in self.resumes],
            "models": list(self.models), "eventCount": self.event_count, "missing": self.missing,
            "children": list(self.children), "warnings": list(self.warnings), "tokensTotal": self.tokens_total,
        }
        if self.spawn_seen_in is not None:
            out["spawnSeenIn"] = self.spawn_seen_in
        if include_file:
            out["file"] = self.file
        return out


@dataclass(frozen=True)
class Tree:
    """All nodes of a `build_tree` call; `session_tree()` gives the SPEC 9.4 JSON of one session."""
    nodes: Dict[str, Agent]

    def session_keys(self):
        return sorted({"%s:%s" % (n.source, n.session_id) for n in self.nodes.values() if n.agent_id == "main"})

    def root_key(self, session_key):
        return session_key + ":main"

    def subtree_keys(self, root_key):
        """Keys reachable from `root_key` in depth-first, children order."""
        if root_key not in self.nodes:
            return []
        out, stack, seen = [], [root_key], set()
        while stack:
            key = stack.pop()
            if key in seen or key not in self.nodes:
                continue
            seen.add(key)
            out.append(key)
            stack.extend(reversed(self.nodes[key].children))
        return out

    def session_tree(self, session_key):
        root = self.root_key(session_key)
        return {"sessionKey": session_key, "rootKey": root,
                "nodes": {k: self.nodes[k].to_dict(include_file=False) for k in self.subtree_keys(root)}}


def empty_tokens():
    return {"input": 0, "output": 0, "cacheRead": 0, "cacheCreation": 0, "reasoning": 0, "total": 0}


def empty_errors():
    return {"toolErrors": 0, "apiErrors": 0, "aborted": 0}


@dataclass(frozen=True)
class Stats:
    """SPEC 5.8. `started_at`/`last_activity_at` are internal, used for `durationMs` when merging."""
    scope: str = "agent"
    tokens: dict = field(default_factory=empty_tokens)
    cost_usd: Optional[float] = None
    by_model: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    skills: dict = field(default_factory=dict)
    slash_commands: dict = field(default_factory=dict)
    subagent_types: dict = field(default_factory=dict)
    agents: dict = field(default_factory=lambda: {"total": 1, "maxDepth": 0, "running": 0})
    active_duration_ms: Optional[int] = None
    errors: dict = field(default_factory=empty_errors)
    turns: int = 0
    parse: dict = field(default_factory=lambda: {"lines": 0, "skipped": 0, "unknownTypes": {}})
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None

    @classmethod
    def empty(cls, scope="agent"):
        return cls(scope=scope)

    @property
    def duration_ms(self):
        return duration_ms(self.started_at, self.last_activity_at)

    def to_dict(self):
        return {
            "scope": self.scope, "tokens": dict(self.tokens), "costUsd": self.cost_usd,
            "byModel": {k: dict(v) for k, v in self.by_model.items()},
            "tools": {k: dict(v) for k, v in self.tools.items()},
            "skills": {k: {"count": v["count"], "via": dict(v["via"])} for k, v in self.skills.items()},
            "slashCommands": dict(self.slash_commands), "subagentTypes": dict(self.subagent_types),
            "agents": dict(self.agents), "durationMs": self.duration_ms, "activeDurationMs": self.active_duration_ms,
            "errors": dict(self.errors), "turns": self.turns,
            "parse": {"lines": self.parse["lines"], "skipped": self.parse["skipped"],
                      "unknownTypes": dict(self.parse["unknownTypes"])},
        }


@dataclass(frozen=True)
class Session:
    """Session list item (SPEC 5.2, JSON of 9.2). Built by the store (WS2)."""
    source: str
    session_id: str
    project: str
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    title: Optional[str] = None
    first_prompt: Optional[str] = None
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    mtime: Optional[float] = None
    status: str = "finished"
    indexed: bool = False
    agent_count: int = 1
    running_agents: int = 0
    tokens: Optional[dict] = None
    errors: Optional[int] = None
    continued_from: Optional[str] = None
    continued_in: Optional[str] = None
    version: Optional[object] = None
    pr_url: Optional[str] = None
    parse: Optional[dict] = None

    @property
    def key(self):
        return "%s:%s" % (self.source, self.session_id)

    @property
    def root_agent_key(self):
        return self.key + ":main"

    def to_dict(self):
        return {
            "key": self.key, "source": self.source, "sessionId": self.session_id, "project": self.project,
            "cwd": self.cwd, "gitBranch": self.git_branch, "title": self.title, "firstPrompt": self.first_prompt,
            "startedAt": format_ts(self.started_at), "lastActivityAt": format_ts(self.last_activity_at),
            "status": self.status, "indexed": self.indexed, "agentCount": self.agent_count,
            "runningAgents": self.running_agents, "tokens": dict(self.tokens) if self.indexed and self.tokens else None,
            "errors": self.errors if self.indexed else None, "continuedFrom": self.continued_from,
            "continuedIn": self.continued_in, "prUrl": self.pr_url,
        }

