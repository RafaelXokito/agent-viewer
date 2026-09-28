"""WS1: Claude Code record -> Event mapping, usage, skills, links, errors."""
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
import loader  # noqa: E402

from agent_viewer.accumulate import AgentState  # noqa: E402
from agent_viewer.claude_parser import BUILTIN_SLASH_COMMANDS, ClaudeParser  # noqa: E402

MAIN_KEY = "claude:s-main:main"


def state_for(root, rel, key):
    state = AgentState(key, ClaudeParser(key), file=os.path.join(root, rel))
    return loader.feed_file(state, os.path.join(root, rel))


def record_line(record):
    return json.dumps(record).encode()


class ClaudeMainFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = loader.copy_fixtures()
        cls.state = state_for(cls.root, "claude/-tmp-demo/s-main.jsonl", MAIN_KEY)

    @classmethod
    def tearDownClass(cls):
        loader.remove_fixtures(cls.root)

    def test_usage_is_counted_once_per_message_id(self):
        usages = self.state.usages
        self.assertEqual(sorted(usages), ["msg_1", "msg_2", "msg_3", "msg_4"])
        u = usages["msg_1"]
        self.assertEqual((u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_creation_tokens),
                         (10, 50, 1000, 200))
        self.assertEqual(self.state.parse_stats.usage_conflicts, 0)

    def test_token_totals(self):
        tokens = self.state.stats().tokens
        self.assertEqual(tokens, {"input": 25, "output": 110, "cacheRead": 4900, "cacheCreation": 200,
                                  "reasoning": 0, "total": 5235})

    def test_tool_counts(self):
        tools = self.state.stats().tools
        self.assertEqual({k: v["calls"] for k, v in tools.items()}, {"Agent": 2, "Skill": 1, "SendMessage": 1})

    def test_skill_via_tool_and_builtin_slash_command(self):
        stats = self.state.stats()
        self.assertEqual(stats.skills, {"code-review": {"count": 1, "via": {"tool": 1, "slash": 0, "fork": 0, "read": 0, "preload": 0}}})
        self.assertEqual(stats.slash_commands, {"model": 1})
        skill = self.state.skills[0]
        self.assertEqual((skill.skill, skill.via, skill.tool_call_id, skill.args), ("code-review", "tool", "toolu_S", "low"))

    def test_unknown_type_counted_and_kept_as_meta(self):
        self.assertEqual(self.state.parse_stats.unknown_types, {"brand-new-type": 1})
        self.assertEqual(self.state.parse_stats.skipped, 0)
        self.assertEqual(self.state.all_events()[-1].kind, "meta")

    def test_links(self):
        spawn = [(l.to_agent_id, l.tool_call_id) for l in self.state.links if l.kind == "spawn"]
        resume = [(l.to_agent_id, l.tool_call_id) for l in self.state.links if l.kind == "resume"]
        self.assertEqual(spawn, [("a1111111111111111", "toolu_A"), ("a3333333333333333", "toolu_B")])
        self.assertEqual(resume, [("a1111111111111111", "toolu_M")])
        legacy = [l for l in self.state.links if l.to_agent_id == "a3333333333333333"][0]
        self.assertEqual(legacy.evidence["agentType"], "ecc:code-explorer")
        self.assertEqual(legacy.evidence["description"], "Helper legacy")
        self.assertEqual(legacy.from_key, MAIN_KEY)
        self.assertEqual(legacy.timestamp.isoformat(), "2026-09-25T10:00:01.200000+00:00")

    def test_thinking_is_redacted(self):
        thinking = [e for e in self.state.all_events() if e.kind == "thinking"]
        self.assertEqual(len(thinking), 1)
        self.assertTrue(thinking[0].redacted)
        self.assertEqual(thinking[0].preview, "")

    def test_active_duration_and_turns(self):
        stats = self.state.stats()
        self.assertEqual(stats.active_duration_ms, 32000)
        self.assertEqual(stats.turns, 1)

    def test_event_kinds_in_order(self):
        kinds = [e.kind for e in self.state.all_events()]
        self.assertEqual(kinds, [
            "meta", "prompt", "thinking", "tool_call", "tool_call", "tool_result", "tool_result",
            "skill", "tool_result", "tool_call", "tool_result", "notification", "system", "text",
            "system", "meta"])
        self.assertEqual([e.seq for e in self.state.all_events()], list(range(16)))

    def test_tool_call_event_is_linked_to_result_and_child(self):
        call = self.state.all_events()[3]
        self.assertEqual((call.tool_name, call.tool_call_id), ("Agent", "toolu_A"))
        self.assertEqual(call.result_seq, 5)
        self.assertEqual(call.duration_ms, 900)
        self.assertEqual(call.spawned_agent_key, "claude:s-main:a1111111111111111")
        self.assertEqual(json.loads(call.preview)["description"], "Helper one")
        result = self.state.all_events()[5]
        self.assertEqual(result.tool_name, "Agent")
        tool_call = self.state.tool_calls["toolu_A"]
        self.assertEqual((tool_call.status, tool_call.duration_ms, tool_call.intent), ("ok", 900, "Helper one"))

    def test_session_meta(self):
        meta = self.state.session_meta
        self.assertEqual(meta["title"], "Demo session")
        self.assertEqual(meta["cwd"], "/tmp/demo")
        self.assertEqual(meta["gitBranch"], "feature/DEMO-1")
        self.assertTrue(meta["firstPrompt"].startswith("Spawn two helpers"))

    def test_status_signals_capture_notification_and_final_turn(self):
        kinds = [s.kind for s in self.state.status_signals]
        self.assertIn("task_notification", kinds)
        note = [s for s in self.state.status_signals if s.kind == "task_notification"][0]
        self.assertEqual((note.agent_id, note.status), ("a1111111111111111", "completed"))
        last = [s for s in self.state.status_signals if s.kind in ("user", "assistant", "tool_result")][-1]
        self.assertEqual((last.kind, last.final), ("assistant", True))

    def test_event_to_dict_matches_contract_shape(self):
        d = self.state.all_events()[3].to_dict()
        self.assertEqual(set(d), {"seq", "kind", "timestamp", "role", "uuid", "parentUuid", "messageId", "model",
                                  "preview", "truncated", "redacted", "toolCallId", "toolName", "isError",
                                  "spawnedAgentKey", "resultSeq", "durationMs"})
        self.assertEqual(d["timestamp"], "2026-09-25T10:00:01.100Z")
        self.assertNotIn("resultSeq", self.state.all_events()[1].to_dict())

    def test_event_ref_points_at_the_source_line(self):
        ref = self.state.event_ref(3)
        path = os.path.join(self.root, "claude/-tmp-demo/s-main.jsonl")
        self.assertEqual(ref.file, path)
        with open(path, "rb") as handle:
            handle.seek(ref.offset)
            record = json.loads(handle.read(ref.length))
        self.assertEqual(record["uuid"], "a2")
        self.assertIsNone(self.state.event_ref(999))

    def test_events_paging_and_filters(self):
        seqs = lambda events: [e.seq for e in events]  # noqa: E731
        self.assertEqual(seqs(self.state.events(None, None, 3, None, False)), [12, 13, 14])
        self.assertEqual(seqs(self.state.events(None, None, 3, None, True)), [13, 14, 15])
        self.assertEqual(seqs(self.state.events(5, None, 2, None, False)), [3, 4])
        self.assertEqual(seqs(self.state.events(None, 0, 2, None, False)), [1, 2])
        self.assertEqual(seqs(self.state.events(None, -1, 1, None, True)), [0])
        self.assertEqual(seqs(self.state.events(None, None, 10, {"tool_result"}, False)), [5, 6, 8, 10])
        self.assertEqual(self.state.events(None, None, 0, None, True), [])
        self.assertEqual(self.state.events(None, None, 1, {"tool_call"}, False)[0].result_seq, 10)

    def test_forked_skill_is_counted_via_fork(self):
        state = AgentState("claude:s:aF", ClaudeParser("claude:s:aF"))
        state.add_forked_skill("code-review")
        self.assertEqual(state.stats().skills["code-review"]["via"]["fork"], 1)

    def test_reset_forgets_everything(self):
        state = state_for(self.root, "claude/-tmp-demo/s-main.jsonl", MAIN_KEY)
        state.reset()
        self.assertEqual((state.event_count, state.parse_stats.lines, state.links, state.stats().tokens["total"]), (0, 0, [], 0))
        loader.feed_file(state, os.path.join(self.root, "claude/-tmp-demo/s-main.jsonl"))
        self.assertEqual([e.to_dict() for e in state.all_events()], [e.to_dict() for e in self.state.all_events()])

    def test_full_content_of_tool_call_and_result(self):
        path = os.path.join(self.root, "claude/-tmp-demo/s-main.jsonl")
        with open(path, "rb") as handle:
            lines = handle.read().split(b"\n")
        call = self.state.full_content(3, json.loads(lines[3]))
        self.assertEqual(call["seq"], 3)
        self.assertEqual(call["kind"], "tool_call")
        self.assertEqual(call["input"]["subagent_type"], "general-purpose")
        result = self.state.full_content(5, json.loads(lines[5]))
        self.assertIn("agentId: a1111111111111111", result["content"])
        self.assertEqual(result["blocks"], [{"type": "text", "text": result["content"]}])
        self.assertIsNone(result["input"])


class ClaudeRecordTest(unittest.TestCase):
    def setUp(self):
        self.state = AgentState(MAIN_KEY, ClaudeParser(MAIN_KEY))
        self.line = 0

    def feed(self, record):
        self.line += 1
        return self.state.feed_line(record_line(record), self.line * 1000, self.line)

    def assistant(self, mid, blocks, model="claude-opus-5-5", usage=None, stop="end_turn", **extra):
        rec = {"type": "assistant", "uuid": "x%d" % self.line, "timestamp": "2026-09-25T10:00:0%d.000Z" % (self.line % 10),
               "message": {"id": mid, "model": model, "stop_reason": stop, "content": blocks,
                           "usage": usage if usage is not None else {"input_tokens": 1, "output_tokens": 2}}}
        rec.update(extra)
        return self.feed(rec)

    def user(self, content, **extra):
        rec = {"type": "user", "uuid": "u%d" % self.line, "timestamp": "2026-09-25T10:00:00.000Z",
               "message": {"role": "user", "content": content}}
        rec.update(extra)
        return self.feed(rec)

    def test_usage_conflict_last_wins_and_is_counted(self):
        self.assistant("m", [{"type": "text", "text": "a"}], usage={"input_tokens": 1, "output_tokens": 2})
        result = self.assistant("m", [{"type": "text", "text": "b"}], usage={"input_tokens": 1, "output_tokens": 9})
        self.assertEqual(len(result.usages), 1)
        self.assertEqual(self.state.usages["m"].output_tokens, 9)
        self.assertEqual(self.state.parse_stats.usage_conflicts, 1)
        self.assertEqual(self.state.stats().tokens["output"], 9)
        self.assertEqual(self.state.stats().by_model["claude-opus-5-5"]["messages"], 1)

    def test_identical_repeated_usage_is_not_reemitted(self):
        self.assistant("m", [{"type": "text", "text": "a"}])
        self.assertEqual(self.assistant("m", [{"type": "text", "text": "b"}]).usages, [])

    def test_synthetic_model_produces_no_usage_and_no_model(self):
        result = self.assistant("m", [{"type": "text", "text": "No response requested."}], model="<synthetic>")
        self.assertEqual(result.usages, [])
        self.assertEqual(self.state.models, [])

    def test_reasoning_tokens_are_read(self):
        self.assistant("m", [], usage={"input_tokens": 1, "output_tokens_details": {"thinking_tokens": 7}})
        self.assertEqual(self.state.stats().tokens["reasoning"], 7)

    def test_api_error_message(self):
        result = self.assistant("m", [{"type": "text", "text": "API Error: x"}], isApiErrorMessage=True)
        self.assertEqual(result.events[0].kind, "error")
        self.assertTrue(result.events[0].is_error)
        self.assertEqual(self.state.stats().errors["apiErrors"], 1)

    def test_system_error_level_and_compaction(self):
        self.feed({"type": "system", "subtype": "informational", "level": "error", "content": "boom"})
        self.feed({"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"})
        self.user("summary text", isCompactSummary=True)
        kinds = [e.kind for e in self.state.all_events()]
        self.assertEqual(kinds, ["error", "compaction", "compaction"])
        self.assertEqual(self.state.stats().errors["apiErrors"], 1)

    def test_tool_error_is_counted_per_tool(self):
        self.assistant("m", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "false"}}], stop="tool_use")
        self.user([{"type": "tool_result", "tool_use_id": "t1", "content": "Exit code 1", "is_error": True}])
        stats = self.state.stats()
        self.assertEqual(stats.tools["Bash"], {"calls": 1, "errors": 1, "totalDurationMs": 0})
        self.assertEqual(stats.errors["toolErrors"], 1)
        self.assertEqual(self.state.tool_calls["t1"].status, "error")

    def test_pending_tool_call_signal(self):
        self.assistant("m", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], stop="tool_use")
        kinds = [s.kind for s in self.state.status_signals]
        self.assertEqual(kinds, ["tool_call", "assistant"])
        self.user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])
        self.assertEqual([s.kind for s in self.state.status_signals], ["tool_result"])

    def test_skill_slash_command_and_args(self):
        result = self.user("<command-name>/code-review</command-name><command-message>code-review</command-message>"
                           "<command-args>low</command-args>")
        self.assertEqual(result.events[0].kind, "skill")
        self.assertEqual([(s.skill, s.via, s.args) for s in self.state.skills], [("code-review", "slash", "low")])
        self.assertEqual(self.state.stats().slash_commands, {})

    def test_builtin_slash_command_list(self):
        for name in ("clear", "compact", "model", "login", "list-agents"):
            self.assertIn(name, BUILTIN_SLASH_COMMANDS)
        result = self.user("<command-name>/clear</command-name><command-args></command-args>")
        self.assertEqual(result.events[0].kind, "system")
        self.assertEqual(result.status_signals, [])

    def test_is_meta_skill_body_attaches_to_tool_call(self):
        result = self.user([{"type": "text", "text": "Base directory for this skill: /x"}], isMeta=True, sourceToolUseID="toolu_S")
        event = result.events[0]
        self.assertEqual((event.kind, event.tool_call_id), ("meta", "toolu_S"))
        self.assertEqual(result.status_signals, [])

    def test_coordinator_message_is_a_notification(self):
        result = self.user("The coordinator sent a message while you were working:\nTry again")
        self.assertEqual(result.events[0].kind, "notification")
        self.assertEqual([s.kind for s in result.status_signals], ["user"])

    def test_preloaded_skill_is_a_skill_invocation(self):
        # Real shape: an agent definition's `skills:` are injected as isMeta user records with no source tool call.
        text = ("<command-message>llm-wiki</command-message>\n<command-name>llm-wiki</command-name>\n"
                "<skill-format>true</skill-format>\nBase directory for this skill: /x/.agent/skills/llm-wiki")
        result = self.user([{"type": "text", "text": text}], isMeta=True)
        self.assertEqual(result.events[0].kind, "skill")
        self.assertEqual([(s.skill, s.via, s.tool_call_id) for s in result.skills], [("llm-wiki", "preload", None)])
        self.assertEqual(result.status_signals, [])
        self.assertEqual(self.state.stats().skills["llm-wiki"]["via"]["preload"], 1)

    def test_meta_skill_body_without_skill_format_stays_meta(self):
        # A slash command's expansion carries the body but no <skill-format>; the slash record already counted it.
        result = self.user([{"type": "text", "text": "<command-name>handoff</command-name>\nBase directory for this skill: /x"}], isMeta=True)
        self.assertEqual(result.events[0].kind, "meta")
        self.assertEqual(result.skills, [])

    def test_meta_coordinator_message_is_a_notification(self):
        # Real Claude Code records a SendMessage resume as isMeta with a coordinator origin.
        result = self.user("The coordinator sent a message while you were working:\nTry again",
                           isMeta=True, origin={"kind": "coordinator"})
        self.assertEqual(result.events[0].kind, "notification")
        self.assertEqual([s.kind for s in result.status_signals], ["user"])

    def test_spawn_child_id_from_regex_when_no_tool_use_result(self):
        self.assistant("m", [{"type": "tool_use", "id": "t1", "name": "Task", "input": {"description": "d"}}], stop="tool_use")
        result = self.user([{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "agentId: abcdef0123 done"}]}])
        self.assertEqual([(l.kind, l.to_agent_id) for l in result.links], [("spawn", "abcdef0123")])
        self.assertEqual(result.links[0].evidence["agentType"], "general-purpose")

    def test_spawn_needs_an_agent_tool_use_in_the_same_file(self):
        result = self.user([{"type": "tool_result", "tool_use_id": "zz", "content": "agentId: abcdef0123"}],
                           toolUseResult={"agentId": "abcdef0123"})
        self.assertEqual(result.links, [])

    def test_resume_from_regex(self):
        self.assistant("m", [{"type": "tool_use", "id": "t9", "name": "SendMessage", "input": {"to": "x"}}], stop="tool_use")
        result = self.user([{"type": "tool_result", "tool_use_id": "t9", "content": '{"resumedAgentId": "abcdef0123"}'}])
        self.assertEqual([(l.kind, l.to_agent_id) for l in result.links], [("resume", "abcdef0123")])

    def test_image_blocks_are_summarised_in_full_content(self):
        block = {"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "text", "text": "see"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD" * 10}}]}
        record = {"type": "user", "message": {"role": "user", "content": [block]}}
        self.feed(record)
        full = self.state.full_content(0, record)
        self.assertEqual(full["blocks"][1], {"type": "image", "mediaType": "image/png", "bytes": 30})
        self.assertNotIn("QUJD", json.dumps(full))

    def test_metadata_records(self):
        self.feed({"type": "ai-title", "aiTitle": "AI title"})
        self.assertEqual(self.state.session_meta["title"], "AI title")
        self.feed({"type": "custom-title", "customTitle": "Mine"})
        self.feed({"type": "ai-title", "aiTitle": "Later AI"})
        self.feed({"type": "pr-link", "prUrl": "https://example/pr/1"})
        self.feed({"type": "continued-in", "sessionId": "s-main", "continuedInSessionId": "s-next"})
        meta = self.state.session_meta
        self.assertEqual((meta["title"], meta["prUrl"], meta["continuedIn"]), ("Mine", "https://example/pr/1", "claude:s-next"))
        self.assertEqual([(l.kind, l.evidence["toSessionKey"]) for l in self.state.links], [("continued_in", "claude:s-next")])

    def test_title_falls_back_to_first_prompt_120_chars(self):
        self.user("p" * 300)
        self.assertEqual(self.state.session_meta["title"], "p" * 120)

    def test_parser_exception_is_contained(self):
        parser = ClaudeParser(MAIN_KEY)
        parser._assistant = None  # force a TypeError inside the parser
        state = AgentState(MAIN_KEY, parser)
        result = state.feed_line(record_line({"type": "assistant", "message": {}}), 0, 1)
        self.assertIsNotNone(result.error)
        self.assertEqual(state.parse_stats.skipped, 1)
        self.assertEqual(state.parse_stats.parsed, 0)
        self.assertEqual(state.event_count, 0)

    def test_random_bytes_and_random_records_never_raise(self):
        rng = random.Random(1234)
        values = [None, True, 0, -1, 3.5, "", "x", "<task-notification>", "<command-name>/m</command-name>", [], {}, [1], {"a": 1}]
        keys = ["type", "message", "content", "id", "model", "usage", "stop_reason", "tool_use_id", "toolUseResult",
                "timestamp", "isMeta", "subtype", "level", "input", "name", "text", "thinking"]
        types = ["user", "assistant", "system", "attachment", "custom-title", "continued-in", "zzz", "tool_use", "tool_result", "text"]

        def rand_value(depth=0):
            if depth < 3 and rng.random() < 0.3:
                return {rng.choice(keys): rand_value(depth + 1) for _ in range(rng.randint(0, 4))}
            if depth < 3 and rng.random() < 0.2:
                return [rand_value(depth + 1) for _ in range(rng.randint(0, 3))]
            return rng.choice(values + types)

        for i in range(10000):
            if i % 3 == 0:
                raw = bytes(rng.randrange(256) for _ in range(rng.randint(0, 40)))
            else:
                rec = {k: rand_value() for k in rng.sample(keys, rng.randint(0, 6))}
                rec["type"] = rng.choice(types)
                raw = json.dumps(rec).encode()
            self.state.feed_line(raw, i, i + 1)
        self.state.stats()
        self.state.events(None, None, 50, None, True)


if __name__ == "__main__":
    unittest.main()
