// Pure timeline helpers: pair tool calls with their results, apply the
// timeline toggles, and derive tool block headers. No DOM access.

import { durationBetween, firstLine, truncate } from './format.js';

const CALL_KINDS = new Set(['tool_call', 'skill']);
const SPAWN_TOOLS = new Set(['Agent', 'Task', 'task']);
const SUMMARY_MAX = 120;
const SUMMARY_FIELDS = ['description', 'command', 'file_path', 'path', 'pattern', 'url', 'query', 'skill', 'prompt'];

/** JSON.parse that returns undefined instead of throwing. */
export function parseJsonSafe(text) {
  if (typeof text !== 'string' || text === '') return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/**
 * Turn a sorted event list into display items:
 * `{type: 'tool', key, call, result}` for a call plus its result (either may
 * be null when the other is on a page not loaded yet), and
 * `{type: 'event', key, event}` for everything else.
 */
export function groupEvents(events, { hideMeta = true, hideThinking = false, onlyErrors = false } = {}) {
  const items = [];
  const toolItems = new Map();

  for (const ev of events) {
    if (hideMeta && ev.kind === 'meta') continue;
    if (hideThinking && ev.kind === 'thinking') continue;
    const id = ev.toolCallId;

    if (CALL_KINDS.has(ev.kind) && id) {
      const existing = toolItems.get(id);
      if (!existing) {
        const item = { type: 'tool', key: `tc:${id}`, call: ev, result: null, skill: null };
        toolItems.set(id, item);
        items.push(item);
        continue;
      }
      // A second call-like event for the same id: the skill form of a call.
      if (!existing.call) {
        existing.call = ev;
        continue;
      }
      if (ev.kind === 'skill' && !existing.skill) {
        existing.skill = ev;
        continue;
      }
    }
    if (ev.kind === 'tool_result' && id) {
      const existing = toolItems.get(id);
      if (existing && !existing.result) {
        existing.result = ev;
        continue;
      }
      if (!existing) {
        const item = { type: 'tool', key: `tc:${id}`, call: null, result: ev, skill: null };
        toolItems.set(id, item);
        items.push(item);
        continue;
      }
    }
    items.push({ type: 'event', key: `e:${ev.seq}`, event: ev });
  }

  return onlyErrors ? items.filter(isErrorItem) : items;
}

export function isErrorItem(item) {
  if (item.type === 'tool') {
    return Boolean((item.call && item.call.isError) || (item.result && item.result.isError));
  }
  return item.event.kind === 'error' || Boolean(item.event.isError);
}

/** "pending", "ok" or "error" for a tool item. */
export function toolStatus(item) {
  if (isErrorItem(item)) return 'error';
  return item.result ? 'ok' : 'pending';
}

/** Server duration when known, else the gap between call and result. */
export function toolDurationMs(item) {
  if (item.call && Number.isFinite(item.call.durationMs)) return item.call.durationMs;
  if (item.call && item.result) return durationBetween(item.call.timestamp, item.result.timestamp);
  return null;
}

/** Skill name from a skill event: input.skill, or the text after skill://. */
export function skillName(event, input) {
  if (input && typeof input === 'object') {
    if (typeof input.skill === 'string') return input.skill;
    if (typeof input.path === 'string' && input.path.startsWith('skill://')) {
      return input.path.slice('skill://'.length);
    }
  }
  const match = /skill:\/\/([^"\s]+)/.exec((event && event.preview) || '');
  return match ? match[1] : null;
}

function summaryFromInput(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return '';
  for (const field of SUMMARY_FIELDS) {
    if (typeof input[field] === 'string' && input[field].trim()) return input[field];
  }
  const firstString = Object.values(input).find((v) => typeof v === 'string' && v.trim());
  return firstString || '';
}

/**
 * Header data for a tool block: `{name, summary, isSkill, isSpawn}`.
 * The summary is the call intent or its most telling input field.
 */
export function toolHeader(item) {
  const call = item.call;
  const source = call || item.result;
  const input = call ? parseJsonSafe(call.preview) : undefined;
  const isSkill = Boolean(item.skill || (call && call.kind === 'skill') || (source && source.toolName === 'Skill'));
  const name = (source && source.toolName) || (isSkill ? 'Skill' : 'tool');
  const isSpawn = SPAWN_TOOLS.has(name);
  let summary = '';
  if (isSkill) {
    summary = skillName(call, input) || (item.skill ? skillName(item.skill, parseJsonSafe(item.skill.preview)) : '') || '';
  }
  if (!summary && call && typeof call.intent === 'string') summary = call.intent;
  if (!summary) summary = summaryFromInput(input);
  if (!summary && call && input === undefined) summary = call.preview || '';
  return { name, summary: truncate(firstLine(summary), SUMMARY_MAX), isSkill, isSpawn };
}

/** Pretty-printed tool input, or the raw preview when it is not valid JSON. */
export function prettyInput(preview) {
  const parsed = parseJsonSafe(preview);
  if (parsed === undefined) return preview || '';
  return JSON.stringify(parsed, null, 2);
}

/** Highest seq in a sorted event list, or -1. */
export function lastSeq(events) {
  return events.length ? events[events.length - 1].seq : -1;
}

/**
 * Timeline tool filter (graph feature G-FR-29a): with a filter set, keep only
 * events whose toolName equals it exactly, the same rule the server applies.
 */
export function matchesToolFilter(event, toolName) {
  if (!toolName) return true;
  return Boolean(event) && event.toolName === toolName;
}
