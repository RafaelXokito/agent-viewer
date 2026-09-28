import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  groupEvents, isErrorItem, lastSeq, parseJsonSafe, prettyInput, skillName, toolDurationMs, toolHeader, toolStatus,
} from '../lib/events.js';

let seq = 0;
const ev = (kind, extra = {}) => ({ seq: seq++, kind, timestamp: '2026-09-25T10:00:00.000Z', preview: '', isError: false, ...extra });

test('groupEvents pairs a tool call with its result into one item', () => {
  seq = 0;
  const events = [
    ev('prompt', { preview: 'Leaf' }),
    ev('tool_call', { toolCallId: 'toolu_X', toolName: 'Bash', preview: '{"command": "false"}', durationMs: 200 }),
    ev('tool_result', { toolCallId: 'toolu_X', toolName: 'Bash', preview: 'Exit code 1', isError: true }),
    ev('text', { preview: 'Leaf done' }),
  ];
  const items = groupEvents(events);
  assert.deepEqual(items.map((i) => i.type), ['event', 'tool', 'event']);
  const tool = items[1];
  assert.equal(tool.call.seq, 1);
  assert.equal(tool.result.seq, 2);
  assert.equal(toolStatus(tool), 'error');
  assert.equal(toolDurationMs(tool), 200);
  assert.deepEqual(toolHeader(tool), { name: 'Bash', summary: 'false', isSkill: false, isSpawn: false });
});

test('groupEvents keeps a result whose call is on an earlier page', () => {
  seq = 10;
  const items = groupEvents([ev('tool_result', { toolCallId: 'toolu_old', toolName: 'Read', preview: 'data' })]);
  assert.equal(items.length, 1);
  assert.equal(items[0].call, null);
  assert.equal(toolStatus(items[0]), 'ok');
  assert.equal(toolHeader(items[0]).name, 'Read');
});

test('a call without a result is pending and its duration is unknown', () => {
  seq = 20;
  const items = groupEvents([ev('tool_call', { toolCallId: 't1', toolName: 'Read', preview: '{"file_path":"/tmp/demo/x"}' })]);
  assert.equal(toolStatus(items[0]), 'pending');
  assert.equal(toolDurationMs(items[0]), null);
  assert.equal(toolHeader(items[0]).summary, '/tmp/demo/x');
});

test('duration falls back to the gap between call and result timestamps', () => {
  seq = 30;
  const items = groupEvents([
    ev('tool_call', { toolCallId: 't1', toolName: 'Read', timestamp: '2026-09-25T10:00:00.000Z' }),
    ev('tool_result', { toolCallId: 't1', timestamp: '2026-09-25T10:00:01.500Z' }),
  ]);
  assert.equal(toolDurationMs(items[0]), 1500);
});

test('skill events group with their result and show the skill name', () => {
  seq = 40;
  const claude = groupEvents([
    ev('skill', { toolCallId: 'toolu_S', toolName: 'Skill', preview: '{"skill": "code-review", "args": "low"}' }),
    ev('tool_result', { toolCallId: 'toolu_S', toolName: 'Skill', preview: 'Launching skill: code-review' }),
  ]);
  assert.equal(claude.length, 1);
  assert.deepEqual(toolHeader(claude[0]), { name: 'Skill', summary: 'code-review', isSkill: true, isSpawn: false });

  const omp = groupEvents([
    ev('tool_call', { toolCallId: 'call_1|x', toolName: 'read', preview: '{"path": "skill://jira-integration"}' }),
    ev('skill', { toolCallId: 'call_1|x', toolName: 'read', preview: '{"path": "skill://jira-integration"}' }),
    ev('tool_result', { toolCallId: 'call_1|x', toolName: 'read', preview: '# jira-integration' }),
  ]);
  assert.equal(omp.length, 1, 'the skill form of the same call is merged, not a second row');
  assert.equal(toolHeader(omp[0]).isSkill, true);
  assert.equal(toolHeader(omp[0]).summary, 'jira-integration');
});

test('spawn calls are flagged so the view can link the child agent', () => {
  seq = 50;
  const items = groupEvents([ev('tool_call', {
    toolCallId: 'toolu_A', toolName: 'Agent', spawnedAgentKey: 'claude:s-main:a1111111111111111',
    preview: '{"description": "Helper one", "subagent_type": "general-purpose", "prompt": "Do one"}',
  })]);
  assert.deepEqual(toolHeader(items[0]), { name: 'Agent', summary: 'Helper one', isSkill: false, isSpawn: true });
});

test('toggles hide meta and thinking, and only-errors keeps error items', () => {
  seq = 60;
  const events = [
    ev('meta', { preview: 'attachment' }),
    ev('thinking', { preview: '', redacted: true }),
    ev('text', { preview: 'ok' }),
    ev('error', { preview: 'API Error' }),
    ev('tool_call', { toolCallId: 'tA', toolName: 'Bash' }),
    ev('tool_result', { toolCallId: 'tA', isError: true }),
    ev('tool_call', { toolCallId: 'tB', toolName: 'Bash' }),
  ];
  assert.deepEqual(groupEvents(events).map((i) => i.key), ['e:61', 'e:62', 'e:63', 'tc:tA', 'tc:tB']);
  assert.equal(groupEvents(events, { hideMeta: false }).length, 6);
  assert.equal(groupEvents(events, { hideThinking: true }).some((i) => i.event && i.event.kind === 'thinking'), false);
  const errorsOnly = groupEvents(events, { onlyErrors: true });
  assert.deepEqual(errorsOnly.map((i) => i.key), ['e:63', 'tc:tA']);
  assert.equal(errorsOnly.every(isErrorItem), true);
});

test('truncated or invalid JSON input is shown raw', () => {
  assert.equal(prettyInput('{"a": 1}'), '{\n  "a": 1\n}');
  assert.equal(prettyInput('{"a": "cut…'), '{"a": "cut…');
  assert.equal(prettyInput(''), '');
  assert.equal(parseJsonSafe('nope'), undefined);
  seq = 70;
  const items = groupEvents([ev('tool_call', { toolCallId: 't', toolName: 'Write', preview: '{"content": "long', truncated: true })]);
  assert.equal(toolHeader(items[0]).summary, '{"content": "long');
});

test('skillName reads input.skill, skill:// paths, or the preview text', () => {
  assert.equal(skillName(null, { skill: 'x' }), 'x');
  assert.equal(skillName(null, { path: 'skill://y' }), 'y');
  assert.equal(skillName({ preview: 'read skill://zeta now' }, undefined), 'zeta');
  assert.equal(skillName({ preview: 'nothing' }, undefined), null);
});

test('lastSeq reports the tail of a sorted list', () => {
  assert.equal(lastSeq([]), -1);
  assert.equal(lastSeq([{ seq: 2 }, { seq: 9 }]), 9);
});
