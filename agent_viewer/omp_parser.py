"""Oh My Pi transcript parser (SPEC 4.2, 5.4-5.7, 6.2 step 5).

Same interface as `ClaudeParser`. Oh My Pi writes one record per API
response, so usage needs no de-duplication.
"""
from .model import (Event, EventRef, Link, SkillInvocation, StatusSignal, ToolCallUpdate, Usage, agent_key,
                    parse_ts, split_key)
from .parse_common import (FINAL_STOP_REASONS, ParseResult, as_dict, as_float, as_int, as_list, as_str,
                           content_blocks, content_text, dumps_preview, preview_text)

KNOWN_TYPES = frozenset((
    "session", "session_init", "title", "title_change", "model_change", "thinking_level_change",
    "service_tier_change", "message", "custom", "custom_message",
))
SYSTEM_TYPES = frozenset(("session_init", "model_change", "thinking_level_change", "service_tier_change"))
SKILL_PREFIX = "skill://"
SPAWN_TOOL = "task"
EMPTY_CONTENT = {"content": "", "input": None, "blocks": []}


class _Ctx:
    __slots__ = ("record", "res", "ts", "uuid", "parent_uuid", "offset", "length", "line_no")

    def __init__(self, record, res, offset, length, line_no):
        self.record = record
        self.res = res
        self.ts = parse_ts(record.get("timestamp"))
        self.uuid = as_str(record.get("id"))
        self.parent_uuid = as_str(record.get("parentId"))
        self.offset = offset
        self.length = length
        self.line_no = line_no


class OmpParser:
    source = "omp"
    known_types = KNOWN_TYPES

    def __init__(self, agent_key):
        self.agent_key = agent_key
        parts = split_key(agent_key) or ("omp", "", "main")
        self.session_id = parts[1]
        self.agent_id = parts[2]
        self.reset()

    def reset(self):
        self._seq = 0
        self._calls = {}      # toolCall id -> (name, timestamp, intent)
        self._spawned = set()  # (toolCallId, child id) already linked

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
        try:
            return self._full_content(record, block)
        except Exception:
            return dict(EMPTY_CONTENT)

    # -- dispatch ---------------------------------------------------------

    def _dispatch(self, ctx):
        record, res = ctx.record, ctx.res
        rtype = record["type"]
        if rtype == "message":
            self._message(ctx)
        elif rtype == "session":
            self._session(ctx)
        elif rtype == "title":
            title = as_str(record.get("title"))
            if title:
                res.session_meta["titleRecord"] = title
            self._event(ctx, "meta", 0, text=title or rtype)
        elif rtype == "title_change":
            title = as_str(record.get("title"))
            if title:
                res.session_meta["titleChange"] = title
            self._event(ctx, "meta", 0, text=title or rtype)
        elif rtype in SYSTEM_TYPES:
            text = as_str(record.get("systemPrompt")) or as_str(record.get("model")) or rtype
            self._event(ctx, "system", 0, text=text, role="system")
        elif rtype == "custom":
            self._custom(ctx)
        elif rtype == "custom_message":
            self._custom_message(ctx)
        else:
            res.unknown_type = rtype
            self._event(ctx, "meta", 0, text=rtype)

    def _event(self, ctx, kind, block, text=None, **fields):
        preview, truncated = preview_text(text)
        event = Event(seq=self._seq, agent_key=self.agent_key, kind=kind, timestamp=ctx.ts, uuid=ctx.uuid,
                      parent_uuid=ctx.parent_uuid, preview=preview, truncated=truncated,
                      ref=EventRef(None, ctx.offset, ctx.length, block, ctx.line_no), **fields)
        self._seq += 1
        ctx.res.events.append(event)
        return event

    def _session(self, ctx):
        record, meta = ctx.record, ctx.res.session_meta
        for field_name, key in (("id", "sessionId"), ("cwd", "cwd"), ("title", "sessionTitle"),
                                ("parentSession", "parentSession")):
            value = as_str(record.get(field_name))
            if value:
                meta[key] = value
        version = record.get("version")
        if isinstance(version, (int, str)) and not isinstance(version, bool):
            meta["version"] = version
        self._event(ctx, "meta", 0, text="session %s" % (as_str(record.get("id")) or ""))

    def _custom(self, ctx):
        record, res = ctx.record, ctx.res
        custom_type = as_str(record.get("customType"))
        data = as_dict(record.get("data"))
        if custom_type == "session_exit":
            self._event(ctx, "system", 0, text="session_exit: %s" % (as_str(data.get("reason")) or ""), role="system")
            res.status_signals.append(StatusSignal("session_exit", ctx.ts))
            return
        if custom_type == "tool_execution_start":
            tid = as_str(data.get("toolCallId"))
            self._event(ctx, "meta", 0, text="tool_execution_start: %s" % (as_str(data.get("toolName")) or ""),
                        tool_call_id=tid, tool_name=as_str(data.get("toolName")))
            if tid:
                started = parse_ts(data.get("startedAt")) or ctx.ts
                res.tool_call_updates.append(ToolCallUpdate("started_at", tid, started))
            return
        self._event(ctx, "meta", 0, text=custom_type or "custom")

    def _custom_message(self, ctx):
        record = ctx.record
        text = content_text(record.get("content"))
        if as_str(record.get("customType")) == "irc:incoming":
            self._event(ctx, "notification", 0, text=text, role="user")
            ctx.res.status_signals.append(StatusSignal("user", ctx.ts))
        else:
            self._event(ctx, "meta", 0, text=text or as_str(record.get("customType")) or "custom_message")

    # -- message ----------------------------------------------------------

    def _message(self, ctx):
        msg = as_dict(ctx.record.get("message"))
        role = msg.get("role")
        if role == "assistant":
            self._assistant(ctx, msg)
        elif role == "toolResult":
            self._tool_result(ctx, msg)
        elif role == "user":
            self._user(ctx, msg)
        else:
            self._event(ctx, "meta", 0, text="message: %s" % role)

    def _user(self, ctx, msg):
        res = ctx.res
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else as_list(content)
        for i, block in enumerate(blocks):
            text = content_text([block]) if isinstance(block, dict) else ""
            self._event(ctx, "prompt", i, text=text, role="user")
            if text.strip() and "firstPrompt" not in res.session_meta:
                res.session_meta["firstPrompt"] = preview_text(text)[0]
        res.status_signals.append(StatusSignal("user", ctx.ts))

    def _assistant(self, ctx, msg):
        record, res = ctx.record, ctx.res
        rid = as_str(record.get("id"))
        model = as_str(msg.get("model"))
        common = {"role": "assistant", "message_id": as_str(msg.get("responseId")) or rid, "model": model}
        for i, block in enumerate(as_list(msg.get("content"))):
            block = block if isinstance(block, dict) else {}
            btype = block.get("type")
            if btype == "text":
                self._event(ctx, "text", i, text=_s(block.get("text")), **common)
            elif btype == "thinking":
                text = _s(block.get("thinking"))
                self._event(ctx, "thinking", i, text=text, redacted=not text, **common)
            elif btype == "toolCall":
                self._tool_call(ctx, block, i, common)
            else:
                self._event(ctx, "meta", i, text=str(btype), **common)
        stop = msg.get("stopReason")
        error_message = as_str(msg.get("errorMessage"))
        if stop == "aborted":
            res.aborted += 1
            self._event(ctx, "error", -1, text=error_message or "aborted", is_error=True, **common)
        elif error_message:
            res.api_errors += 1
            self._event(ctx, "error", -1, text=error_message, is_error=True, **common)
        usage = msg.get("usage")
        if rid and isinstance(usage, dict):
            res.usages.append(Usage(
                message_id=rid, model=model, timestamp=ctx.ts, input_tokens=as_int(usage.get("input")),
                output_tokens=as_int(usage.get("output")), cache_read_tokens=as_int(usage.get("cacheRead")),
                cache_creation_tokens=as_int(usage.get("cacheWrite")),
                reasoning_tokens=as_int(usage.get("reasoningTokens")),
                cost_usd=as_float(as_dict(usage.get("cost")).get("total")),
                duration_ms=as_int(msg.get("duration")) if "duration" in msg else None))
        if "duration" in msg:
            res.active_duration_ms = as_int(msg.get("duration"))
        final = isinstance(stop, str) and stop in FINAL_STOP_REASONS
        res.status_signals.append(StatusSignal("assistant", ctx.ts, final=final, message_id=rid))

    def _tool_call(self, ctx, block, index, common):
        res = ctx.res
        tid = as_str(block.get("id"))
        name = as_str(block.get("name")) or "unknown"
        arguments = as_dict(block.get("arguments"))
        intent = as_str(block.get("intent"))
        path = arguments.get("path")
        is_skill = name == "read" and isinstance(path, str) and path.startswith(SKILL_PREFIX)
        event = self._event(ctx, "skill" if is_skill else "tool_call", index, text=dumps_preview(arguments),
                            tool_call_id=tid, tool_name=name, **common)
        if tid:
            self._calls[tid] = (name, ctx.ts, intent)
            res.tool_call_updates.append(ToolCallUpdate("start", tid, ctx.ts, event.seq, name=name, input=arguments,
                                                        intent=intent))
            res.status_signals.append(StatusSignal("tool_call", ctx.ts, tool_call_id=tid))
        if is_skill and len(path) > len(SKILL_PREFIX):
            res.skills.append(SkillInvocation(path[len(SKILL_PREFIX):], "read", self.agent_key, ctx.ts, tid, None))

    def _tool_result(self, ctx, msg):
        res = ctx.res
        tid = as_str(msg.get("toolCallId"))
        call = self._calls.get(tid) if tid else None
        name = as_str(msg.get("toolName")) or (call[0] if call else None)
        is_error = msg.get("isError") is True
        spawned = None
        if name == SPAWN_TOOL:
            spawned = self._spawn_links(ctx, msg, tid, call)
        event = self._event(ctx, "tool_result", 0, text=content_text(msg.get("content")), role="tool",
                            tool_call_id=tid, tool_name=name, is_error=is_error)
        if tid:
            res.tool_call_updates.append(ToolCallUpdate("finish", tid, ctx.ts, event.seq, name=name, is_error=is_error,
                                                        spawned_agent_key=spawned))
            res.status_signals.append(StatusSignal("tool_result", ctx.ts, tool_call_id=tid))
        elif is_error:
            res.tool_call_updates.append(ToolCallUpdate("finish", "", ctx.ts, event.seq, name=name, is_error=True))

    def _spawn_links(self, ctx, msg, tid, call):
        """Emit one spawn link per `details.progress[]` child; return the first child's agent key."""
        first = None
        for item in as_list(as_dict(msg.get("details")).get("progress")):
            item = as_dict(item)
            child = as_str(item.get("id"))
            if not child or (tid, child) in self._spawned:
                continue
            self._spawned.add((tid, child))
            child_type = as_str(item.get("agent"))
            ctx.res.links.append(Link("spawn", self.agent_key, child, tid, call[1] if call else ctx.ts,
                                      {"agentType": child_type, "description": call[2] if call else None}))
            if child_type:
                ctx.res.subagent_types.append(child_type)
            child_path = child if self.agent_id == "main" else "%s/%s" % (self.agent_id, child)
            first = first or agent_key("omp", self.session_id, child_path)
        return first

    # -- full content -----------------------------------------------------

    def _full_content(self, record, block):
        msg = as_dict(record.get("message"))
        role = msg.get("role")
        if record.get("type") == "message" and role == "assistant":
            blocks = as_list(msg.get("content"))
            item = blocks[block] if isinstance(block, int) and 0 <= block < len(blocks) else {}
            item = item if isinstance(item, dict) else {}
            if item.get("type") == "toolCall":
                arguments = as_dict(item.get("arguments"))
                return {"content": dumps_preview(arguments), "input": arguments,
                        "blocks": [{"type": "toolCall", "id": as_str(item.get("id")), "name": as_str(item.get("name"))}]}
            if item.get("type") == "thinking":
                text = _s(item.get("thinking"))
                return {"content": text, "input": None, "blocks": [{"type": "thinking", "text": text}]}
            if not item:
                text = as_str(msg.get("errorMessage")) or ""
                return {"content": text, "input": None, "blocks": [{"type": "text", "text": text}]}
            text = _s(item.get("text"))
            return {"content": text, "input": None, "blocks": [{"type": "text", "text": text}]}
        if record.get("type") == "message":
            return {"content": content_text(msg.get("content")), "input": None, "blocks": content_blocks(msg.get("content"))}
        if record.get("type") == "custom_message":
            content = record.get("content")
            return {"content": content_text(content), "input": None, "blocks": content_blocks(content)}
        text = as_str(record.get("systemPrompt")) or dumps_preview(record)
        return {"content": text, "input": None, "blocks": [{"type": "text", "text": text}]}


def _s(value):
    return value if isinstance(value, str) else ""
