"""Claude Code transcript parser (SPEC 4.1, 5.4-5.7, 6.1 steps 4-5).

One `ClaudeParser` per agent file. It is stateful only for what a single file
needs: the event counter, the tool_use ids seen (to pair results and emit
links) and the usage already emitted per `message.id` (dedup, SPEC 5.7).
"""
import re
from dataclasses import replace

from .model import (Event, EventRef, Link, SkillInvocation, StatusSignal, ToolCallUpdate, Usage, agent_key,
                    parse_ts, split_key)
from .parse_common import (FINAL_STOP_REASONS, ParseResult, as_dict, as_int, as_list, as_str, content_blocks,
                           content_text, dumps_preview, preview_text)

BUILTIN_SLASH_COMMANDS = frozenset((
    "clear", "compact", "model", "login", "logout", "rename", "resume", "help", "config", "cost", "status", "exit",
    "init", "memory", "permissions", "agents", "mcp", "hooks", "ide", "doctor", "fast", "effort", "plan", "add-dir",
    "context", "export", "list-agents",
))

# Session metadata records: record type -> (record field, session_meta key).
META_RECORDS = {
    "custom-title": ("customTitle", "customTitle"),
    "ai-title": ("aiTitle", "aiTitle"),
    "agent-name": ("agentName", "agentName"),
    "last-prompt": ("lastPrompt", "lastPrompt"),
    "pr-link": ("prUrl", "prUrl"),
    "permission-mode": ("permissionMode", "permissionMode"),
    "summary": ("summary", "summary"),
}

KNOWN_TYPES = frozenset((
    "user", "assistant", "attachment", "system", "queue-operation", "last-prompt", "ai-title", "custom-title",
    "agent-name", "mode", "permission-mode", "atis-latch", "bridge-session", "pr-link", "cost-state",
    "file-history-snapshot", "file-history-delta", "history-suppression", "frame-link", "continued-in", "summary",
))

SPAWN_TOOLS = ("Agent", "Task")
DEFAULT_SUBAGENT_TYPE = "general-purpose"
COORDINATOR_PREFIX = "The coordinator sent a message while you were working:"
WRAPPER_SCAN_CHARS = 300

AGENT_ID_RE = re.compile(r"agentId: ([0-9a-f]{8,})")
RESUME_RE = re.compile(r'"resumedAgentId"\s*:\s*"([0-9a-f]{8,})"')
TASK_ID_RE = re.compile(r"<task-id>\s*(.*?)\s*</task-id>", re.S)
TASK_STATUS_RE = re.compile(r"<status>\s*(.*?)\s*</status>", re.S)
COMMAND_NAME_RE = re.compile(r"<command-name>\s*/?(.*?)\s*</command-name>", re.S)
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
SKILL_FORMAT_MARK = "<skill-format>true</skill-format>"

EMPTY_CONTENT = {"content": "", "input": None, "blocks": []}


class _Ctx:
    __slots__ = ("record", "res", "ts", "uuid", "parent_uuid", "offset", "length", "line_no")

    def __init__(self, record, res, offset, length, line_no):
        self.record = record
        self.res = res
        self.ts = parse_ts(record.get("timestamp"))
        self.uuid = as_str(record.get("uuid"))
        self.parent_uuid = as_str(record.get("parentUuid"))
        self.offset = offset
        self.length = length
        self.line_no = line_no


class ClaudeParser:
    source = "claude"
    known_types = KNOWN_TYPES

    def __init__(self, agent_key):
        self.agent_key = agent_key
        parts = split_key(agent_key) or ("claude", "", "main")
        self.session_id = parts[1]
        self.agent_id = parts[2]
        self.reset()

    def reset(self):
        self._seq = 0
        self._calls = {}   # tool_use id -> (name, timestamp, subagent_type, description)
        self._usage = {}   # message.id -> last emitted Usage

    # -- public -----------------------------------------------------------

    def parse_record(self, record, offset, length, line_no):
        """Map one decoded record to a ParseResult; never raises (errors land in `result.error`)."""
        res = ParseResult()
        seq_before = self._seq
        try:
            self._dispatch(_Ctx(record, res, offset, length, line_no))
        except Exception as exc:  # defensive: undocumented schema, never fatal (SPEC G6)
            self._seq = seq_before
            return ParseResult(error="parser_error:%s" % type(exc).__name__)
        return res

    def full_content(self, record, block):
        """Untruncated content of one event (SPEC 9.5 body without `seq`/`kind`)."""
        try:
            return self._full_content(record, block)
        except Exception:
            return dict(EMPTY_CONTENT)

    # -- dispatch ---------------------------------------------------------

    def _dispatch(self, ctx):
        record = ctx.record
        rtype = record["type"]
        self._common_meta(record, ctx.res.session_meta)
        if rtype == "assistant":
            self._assistant(ctx)
        elif rtype == "user":
            self._user(ctx)
        elif rtype == "system":
            self._system(ctx)
        elif rtype in META_RECORDS:
            self._meta_record(ctx, rtype)
        elif rtype == "continued-in":
            self._continued_in(ctx)
        elif rtype in KNOWN_TYPES:
            detail = as_str(as_dict(record.get("attachment")).get("type"))
            self._event(ctx, "meta", 0, text="%s: %s" % (rtype, detail) if detail else rtype)
        else:
            ctx.res.unknown_type = rtype
            self._event(ctx, "meta", 0, text=rtype)

    @staticmethod
    def _common_meta(record, meta):
        for field_name in ("cwd", "gitBranch", "version", "sessionId"):
            value = as_str(record.get(field_name))
            if value:
                meta[field_name] = value

    def _event(self, ctx, kind, block, text=None, **fields):
        preview, truncated = preview_text(text)
        event = Event(seq=self._seq, agent_key=self.agent_key, kind=kind, timestamp=ctx.ts, uuid=ctx.uuid,
                      parent_uuid=ctx.parent_uuid, preview=preview, truncated=truncated,
                      ref=EventRef(None, ctx.offset, ctx.length, block, ctx.line_no), **fields)
        self._seq += 1
        ctx.res.events.append(event)
        return event

    # -- assistant --------------------------------------------------------

    def _assistant(self, ctx):
        record, res = ctx.record, ctx.res
        msg = as_dict(record.get("message"))
        mid = as_str(msg.get("id"))
        model = as_str(msg.get("model"))
        stop = msg.get("stop_reason")
        api_error = record.get("isApiErrorMessage") is True
        common = {"role": "assistant", "message_id": mid, "model": model}
        for i, block in enumerate(_blocks(msg.get("content"))):
            btype = block.get("type")
            if btype == "text":
                self._event(ctx, "error" if api_error else "text", i, text=_s(block.get("text")),
                            is_error=api_error, **common)
            elif btype in ("thinking", "redacted_thinking"):
                text = _s(block.get("thinking"))
                self._event(ctx, "thinking", i, text=text, redacted=not text, **common)
            elif btype == "tool_use":
                self._tool_use(ctx, block, i, common)
            else:
                self._event(ctx, "meta", i, text=str(btype), **common)
        if api_error:
            res.api_errors += 1
        usage = msg.get("usage")
        if mid and model != "<synthetic>" and isinstance(usage, dict):
            self._usage_for(ctx, mid, model, usage)
        final = isinstance(stop, str) and stop in FINAL_STOP_REASONS
        res.status_signals.append(StatusSignal("assistant", ctx.ts, final=final, message_id=mid))

    def _tool_use(self, ctx, block, index, common):
        res = ctx.res
        tid = as_str(block.get("id"))
        name = as_str(block.get("name")) or "unknown"
        tool_input = as_dict(block.get("input"))
        kind = "skill" if name == "Skill" else "tool_call"
        event = self._event(ctx, kind, index, text=dumps_preview(tool_input), tool_call_id=tid, tool_name=name, **common)
        subagent_type = as_str(tool_input.get("subagent_type")) or DEFAULT_SUBAGENT_TYPE
        if tid:
            self._calls[tid] = (name, ctx.ts, subagent_type, as_str(tool_input.get("description")))
            res.tool_call_updates.append(ToolCallUpdate("start", tid, ctx.ts, event.seq, name=name, input=tool_input,
                                                        intent=as_str(tool_input.get("description"))))
            res.status_signals.append(StatusSignal("tool_call", ctx.ts, tool_call_id=tid))
        if name == "Skill" and as_str(tool_input.get("skill")):
            res.skills.append(SkillInvocation(tool_input["skill"], "tool", self.agent_key, ctx.ts, tid,
                                              as_str(tool_input.get("args"))))
        if name in SPAWN_TOOLS:
            res.subagent_types.append(subagent_type)

    def _usage_for(self, ctx, mid, model, usage):
        details = as_dict(usage.get("output_tokens_details"))
        new = Usage(message_id=mid, model=model, timestamp=ctx.ts,
                    input_tokens=as_int(usage.get("input_tokens")), output_tokens=as_int(usage.get("output_tokens")),
                    cache_read_tokens=as_int(usage.get("cache_read_input_tokens")),
                    cache_creation_tokens=as_int(usage.get("cache_creation_input_tokens")),
                    reasoning_tokens=as_int(details.get("thinking_tokens")))
        previous = self._usage.get(mid)
        if previous is not None:
            if previous.counts() == new.counts():
                return
            new = replace(new, timestamp=previous.timestamp)
        self._usage[mid] = new
        ctx.res.usages.append(new)

    # -- user -------------------------------------------------------------

    def _user(self, ctx):
        record = ctx.record
        content = as_dict(record.get("message")).get("content")
        if record.get("isCompactSummary") is True:
            self._event(ctx, "compaction", 0, text=content_text(content), role="user")
            return
        is_coordinator = as_dict(record.get("origin")).get("kind") == "coordinator"
        if record.get("isMeta") is True and not (is_coordinator and isinstance(content, str)):
            text = content_text(content)
            preloaded = _preloaded_skill(record, text)
            if preloaded:
                # Injected for an agent definition's `skills:`; not user activity, so no status signal.
                self._event(ctx, "skill", 0, text=text, role="user")
                ctx.res.skills.append(SkillInvocation(preloaded, "preload", self.agent_key, ctx.ts, None, None))
                return
            self._event(ctx, "meta", 0, text=text, role="user",
                        tool_call_id=as_str(record.get("sourceToolUseID")))
            return
        if isinstance(content, str):
            self._user_text(ctx, content, 0)
            return
        blocks = _blocks(content)
        results = [b for b in blocks if b.get("type") == "tool_result"]
        tool_use_result = as_dict(record.get("toolUseResult")) if len(results) == 1 else {}
        for i, block in enumerate(blocks):
            btype = block.get("type")
            if btype == "tool_result":
                self._tool_result(ctx, block, i, tool_use_result)
            elif btype == "text":
                self._user_text(ctx, _s(block.get("text")), i)
            elif btype == "image":
                self._event(ctx, "prompt", i, text="[image]", role="user")
                ctx.res.status_signals.append(StatusSignal("user", ctx.ts))
            else:
                self._event(ctx, "meta", i, text=str(btype), role="user")

    def _user_text(self, ctx, text, index):
        res = ctx.res
        head = text.lstrip()[:WRAPPER_SCAN_CHARS]
        if head.startswith("<task-notification>") or head.startswith("<task-id>"):
            self._event(ctx, "notification", index, text=text, role="user")
            res.status_signals.append(StatusSignal("user", ctx.ts))
            task_id = TASK_ID_RE.search(text)
            if task_id and task_id.group(1):
                status = TASK_STATUS_RE.search(text)
                res.status_signals.append(StatusSignal("task_notification", ctx.ts, agent_id=task_id.group(1),
                                                       status=status.group(1) if status else None))
            return
        command = COMMAND_NAME_RE.search(head) if "<command-" in head else None
        if command and command.group(1):
            self._slash_command(ctx, text, index, command.group(1))
            return
        if head.startswith("<local-command-") or head.startswith("[Request interrupted by user"):
            self._event(ctx, "system", index, text=text, role="user")
            return
        if head.startswith(COORDINATOR_PREFIX):
            self._event(ctx, "notification", index, text=text, role="user")
            res.status_signals.append(StatusSignal("user", ctx.ts))
            return
        self._event(ctx, "prompt", index, text=text, role="user")
        res.status_signals.append(StatusSignal("user", ctx.ts))
        if text.strip() and "firstPrompt" not in res.session_meta:
            res.session_meta["firstPrompt"] = preview_text(text)[0]

    def _slash_command(self, ctx, text, index, name):
        res = ctx.res
        if name in BUILTIN_SLASH_COMMANDS:
            self._event(ctx, "system", index, text=text, role="user")
            res.slash_commands.append(name)
            return
        args_match = COMMAND_ARGS_RE.search(text)
        args = args_match.group(1).strip() if args_match else ""
        self._event(ctx, "skill", index, text=text, role="user")
        res.skills.append(SkillInvocation(name, "slash", self.agent_key, ctx.ts, None, args or None))
        res.status_signals.append(StatusSignal("user", ctx.ts))

    def _tool_result(self, ctx, block, index, tool_use_result):
        res = ctx.res
        tid = as_str(block.get("tool_use_id"))
        is_error = block.get("is_error") is True
        text = content_text(block.get("content"))
        call = self._calls.get(tid) if tid else None
        name = call[0] if call else None
        spawned = None
        if name in SPAWN_TOOLS:
            child = as_str(tool_use_result.get("agentId")) or _regex_group(AGENT_ID_RE, text)
            if child:
                spawned = agent_key("claude", self.session_id, child)
                res.links.append(Link("spawn", self.agent_key, child, tid, call[1] or ctx.ts,
                                      {"agentType": call[2], "description": call[3]}))
        if name == "SendMessage" or "resumedAgentId" in tool_use_result:
            resumed = as_str(tool_use_result.get("resumedAgentId")) or _regex_group(RESUME_RE, text)
            if resumed:
                res.links.append(Link("resume", self.agent_key, resumed, tid, ctx.ts, {}))
        event = self._event(ctx, "tool_result", index, text=text, role="tool", tool_call_id=tid, tool_name=name,
                            is_error=is_error)
        if tid:
            res.tool_call_updates.append(ToolCallUpdate("finish", tid, ctx.ts, event.seq, name=name, is_error=is_error,
                                                        spawned_agent_key=spawned))
            res.status_signals.append(StatusSignal("tool_result", ctx.ts, tool_call_id=tid))

    # -- system and metadata ----------------------------------------------

    def _system(self, ctx):
        record, res = ctx.record, ctx.res
        subtype = as_str(record.get("subtype"))
        text = _s(record.get("content")) or subtype or "system"
        if subtype == "compact_boundary":
            self._event(ctx, "compaction", 0, text=text, role="system")
        elif record.get("level") == "error":
            self._event(ctx, "error", 0, text=text, role="system", is_error=True)
            res.api_errors += 1
        else:
            self._event(ctx, "system", 0, text=text, role="system")
        if subtype == "turn_duration":
            res.active_duration_ms = as_int(record.get("durationMs"))

    def _meta_record(self, ctx, rtype):
        field_name, meta_key = META_RECORDS[rtype]
        value = as_str(ctx.record.get(field_name))
        if value:
            ctx.res.session_meta[meta_key] = value
        self._event(ctx, "meta", 0, text=value or rtype)

    def _continued_in(self, ctx):
        record, res = ctx.record, ctx.res
        target = as_str(record.get("continuedInSessionId"))
        origin = as_str(record.get("sessionId"))
        if target and target != self.session_id:
            to_key = "claude:" + target
            res.session_meta["continuedIn"] = to_key
            res.links.append(Link("continued_in", self.agent_key, "main", None, ctx.ts, {"toSessionKey": to_key}))
        elif target and origin and origin != self.session_id:
            res.session_meta["continuedFrom"] = "claude:" + origin
        self._event(ctx, "meta", 0, text="continued-in: %s" % (target or ""))

    # -- full content -----------------------------------------------------

    def _full_content(self, record, block):
        rtype = record.get("type")
        msg = as_dict(record.get("message"))
        content = msg.get("content")
        if rtype == "assistant":
            return _assistant_block_content(_block_at(content, block))
        if rtype == "user":
            if record.get("isMeta") is True or record.get("isCompactSummary") is True or isinstance(content, str):
                return {"content": content_text(content), "input": None, "blocks": content_blocks(content)}
            item = _block_at(content, block)
            if item.get("type") == "tool_result":
                inner = item.get("content")
                return {"content": content_text(inner), "input": None, "blocks": content_blocks(inner)}
            return {"content": content_text([item]), "input": None, "blocks": content_blocks([item])}
        if rtype == "system":
            text = _s(record.get("content")) or as_str(record.get("subtype")) or ""
            return {"content": text, "input": None, "blocks": [{"type": "text", "text": text}]}
        text = dumps_preview(record)
        return {"content": text, "input": None, "blocks": [{"type": "text", "text": text}]}


def _assistant_block_content(item):
    btype = item.get("type")
    if btype == "tool_use":
        tool_input = as_dict(item.get("input"))
        return {"content": dumps_preview(tool_input), "input": tool_input,
                "blocks": [{"type": "tool_use", "id": as_str(item.get("id")), "name": as_str(item.get("name"))}]}
    if btype in ("thinking", "redacted_thinking"):
        text = _s(item.get("thinking"))
        return {"content": text, "input": None, "blocks": [{"type": "thinking", "text": text}]}
    text = _s(item.get("text"))
    return {"content": text, "input": None, "blocks": [{"type": "text", "text": text}]}


def _blocks(content):
    """Content as a list of dict blocks; a plain string becomes one text block."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b if isinstance(b, dict) else {"type": None} for b in as_list(content)]


def _block_at(content, index):
    blocks = _blocks(content)
    return blocks[index] if isinstance(index, int) and 0 <= index < len(blocks) else {}


def _s(value):
    return value if isinstance(value, str) else ""


def _preloaded_skill(record, text):
    """Skill name of a preloaded-skill record, else None.

    Real shape: an isMeta user record with no source tool call whose text
    starts with <command-name>NAME</command-name> and the skill-format marker.
    """
    if record.get("sourceToolUseID") or SKILL_FORMAT_MARK not in text[:WRAPPER_SCAN_CHARS]:
        return None
    return _regex_group(COMMAND_NAME_RE, text[:WRAPPER_SCAN_CHARS]) or None


def _regex_group(pattern, text):
    match = pattern.search(text) if text else None
    return match.group(1) if match else None
