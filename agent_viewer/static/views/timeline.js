// Timeline of one agent (section 8.3): paged events, tool call/result
// pairing, lazy full content, toggles, live appends with auto-scroll.

import { api } from '../api.js';
import { h, replace } from '../dom.js';
import { formatBytes, formatClock, formatDuration, shortId, truncate } from '../lib/format.js';
import { groupEvents, lastSeq, matchesToolFilter, prettyInput, toolDurationMs, toolHeader, toolStatus } from '../lib/events.js';
import { buildSessionHash, mergeEvents, parseAgentKey } from '../lib/state.js';
import { copyButton, emptyState, errorBox, idChip, loading, statusChip } from './widgets.js';

const PAGE_LIMIT = 200;
const MAX_FETCH_LIMIT = 1000;
const CATCH_UP_ROUNDS = 20;
const NEAR_TOP_PX = 120;
const NEAR_BOTTOM_PX = 48;
const BIG_BODY_CHARS = 1024 * 1024;
const THINKING_SUMMARY_MAX = 100;

const KIND_LABELS = Object.freeze({
  prompt: 'User',
  text: 'Assistant',
  thinking: 'Thinking',
  notification: 'Notification',
  system: 'System',
  compaction: 'Compaction',
  error: 'Error',
  meta: 'Meta',
  tool_result: 'Result',
  skill: 'Skill',
  tool_call: 'Tool',
});

function defaultAgentHash(parsed) {
  return buildSessionHash(parsed.source, parsed.sessionId, parsed.agentId);
}

/**
 * `agentHash(parsedAgentKey)` builds the href of "open agent" links; the
 * graph drawer passes one that keeps the graph layout.
 */
export function createTimelineView(container, { agentHash = defaultAgentHash } = {}) {
  let ref = null;
  let toolName = null;
  let agentKey = null;
  let agentInfo = null;
  let events = [];
  let hasBefore = false;
  let token = 0;
  let isLoadingOlder = false;
  let newCount = 0;
  let freshSeqs = new Set();
  const opts = { hideMeta: true, hideThinking: false, onlyErrors: false };
  const expanded = new Set();
  const fullContent = new Map();

  const header = h('div', { class: 'panel-header timeline-header' });
  const toggles = h('div', { class: 'toggles' });
  const olderBar = h('div', { class: 'older-bar' });
  const list = h('ol', { class: 'timeline', id: 'timeline', 'aria-live': 'off' });
  const scroller = h('div', { class: 'panel-body timeline-scroll', onScroll: onScroll }, olderBar, list);
  const pill = h('button', { type: 'button', class: 'new-pill', hidden: true, onClick: scrollToBottom });
  replace(container, header, toggles, h('div', { class: 'timeline-frame' }, scroller, pill));
  renderToggles();

  function renderToggles() {
    const box = (label, key, onChange) => h('label', { class: 'toggle' },
      h('input', { type: 'checkbox', checked: opts[key], onChange: (e) => onChange(e.target.checked) }),
      h('span', { text: label }));
    replace(toggles,
      box('Hide meta', 'hideMeta', (v) => {
        opts.hideMeta = v;
        reload();
      }),
      box('Hide thinking', 'hideThinking', (v) => {
        opts.hideThinking = v;
        render();
      }),
      box('Only errors', 'onlyErrors', (v) => {
        opts.onlyErrors = v;
        render();
      }));
  }

  function renderHeader() {
    if (!ref) {
      replace(header);
      return;
    }
    const info = agentInfo || {};
    const title = ref.agentId === 'main' ? 'main' : info.agentType || 'agent';
    replace(header,
      h('div', { class: 'timeline-title' },
        h('h2', { text: title }),
        info.status && statusChip(info.status),
        ref.agentId !== 'main' && idChip(ref.agentId, { n: 17, label: 'Copy agent id' })),
      info.description && h('p', { class: 'timeline-desc', text: info.description }),
      info.file && h('p', { class: 'timeline-file mono', title: info.file, text: info.file }));
  }

  function isAtBottom() {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < NEAR_BOTTOM_PX;
  }

  function scrollToBottom() {
    scroller.scrollTop = scroller.scrollHeight;
    newCount = 0;
    renderPill();
  }

  function renderPill() {
    pill.hidden = newCount === 0;
    pill.textContent = `${newCount} new event${newCount === 1 ? '' : 's'}`;
  }

  function renderOlderBar() {
    if (isLoadingOlder) {
      replace(olderBar, loading('Loading older events...'));
    } else if (hasBefore) {
      replace(olderBar, h('button', { type: 'button', class: 'btn btn-small', text: 'Load older events', onClick: loadOlder }));
    } else if (events.length) {
      replace(olderBar, h('p', { class: 'muted small', text: 'Start of transcript' }));
    } else {
      replace(olderBar);
    }
  }

  // ---- full content --------------------------------------------------------

  function fullBody(seq, asInput) {
    const full = fullContent.get(seq);
    if (!full) return null;
    if (full.error) return errorBox(full.error);
    const text = asInput && full.input != null ? JSON.stringify(full.input, null, 2) : full.content || '';
    return h('div', { class: 'full-body' },
      h('div', { class: 'full-bar' },
        h('span', { class: 'muted small', text: `Full content, ${formatBytes(text.length)}` }),
        copyButton(text, 'Copy full content')),
      h('pre', { class: ['content', 'mono', text.length > BIG_BODY_CHARS && 'big'], text }));
  }

  function showFullButton(ev) {
    if (!ev || !ev.truncated || fullContent.has(ev.seq)) return null;
    return h('button', {
      type: 'button',
      class: 'btn btn-small',
      text: 'Show full',
      onClick: async (e) => {
        e.stopPropagation();
        const myToken = token;
        e.currentTarget.disabled = true;
        try {
          const full = await api.eventFull(ref, ev.seq);
          if (myToken !== token) return;
          fullContent.set(ev.seq, full);
        } catch (err) {
          if (myToken !== token) return;
          fullContent.set(ev.seq, { error: err });
        }
        render();
      },
    });
  }

  // ---- rows ----------------------------------------------------------------

  function spawnLink(event) {
    const parsed = event && event.spawnedAgentKey ? parseAgentKey(event.spawnedAgentKey) : null;
    if (!parsed) return null;
    return h('a', {
      class: 'spawn-link',
      href: agentHash(parsed),
      title: `Open agent ${parsed.agentId}`,
      text: `open agent ${shortId(parsed.agentId)}`,
    });
  }

  function textBlock(ev, extraClass) {
    const hasFull = fullContent.has(ev.seq);
    return [
      !hasFull && h('pre', { class: ['content', extraClass], text: ev.preview || '' }),
      !hasFull && ev.truncated && h('span', { class: 'muted small', text: 'Preview truncated. ' }),
      showFullButton(ev),
      fullBody(ev.seq, false),
    ];
  }

  function rowHead(kind, ev, ...extra) {
    return h('div', { class: 'ev-head' },
      h('span', { class: `ev-kind kind-${kind}`, text: KIND_LABELS[kind] || kind }),
      extra,
      h('span', { class: 'ev-time mono', title: ev.timestamp || '', text: formatClock(ev.timestamp) }));
  }

  function renderToolItem(item) {
    const head = toolHeader(item);
    const status = toolStatus(item);
    const duration = toolDurationMs(item);
    const call = item.call;
    const result = item.result;
    const anchor = call || result;
    const details = h('details', {
      class: ['tool', `tool-${status}`, head.isSkill && 'tool-skill'],
      open: expanded.has(item.key),
      onToggle: (e) => {
        if (e.currentTarget.open) expanded.add(item.key);
        else expanded.delete(item.key);
      },
    },
    h('summary', null,
      h('span', { class: `ev-kind ${head.isSkill ? 'kind-skill' : 'kind-tool'}`, text: head.isSkill ? 'Skill' : 'Tool' }),
      h('span', { class: 'tool-name mono', text: head.name }),
      head.summary && h('span', { class: 'tool-summary', title: head.summary, text: head.summary }),
      h('span', { class: `chip chip-${status}`, text: status }),
      duration != null && h('span', { class: 'tool-dur mono', text: formatDuration(duration) }),
      spawnLink(call),
      h('span', { class: 'ev-time mono', title: anchor.timestamp || '', text: formatClock(anchor.timestamp) })),
    h('div', { class: 'tool-body' },
      h('div', { class: 'tool-section' },
        h('h4', null, 'Input ', call && call.toolCallId && h('code', { class: 'mono muted', text: call.toolCallId })),
        call
          ? [
            !fullContent.has(call.seq) && h('pre', { class: 'content mono', text: prettyInput(call.preview) }),
            showFullButton(call),
            fullBody(call.seq, true),
          ]
          : h('p', { class: 'muted small', text: 'The call is on an earlier page.' })),
      h('div', { class: ['tool-section', result && result.isError && 'is-error'] },
        h('h4', { text: result && result.isError ? 'Result (error)' : 'Result' }),
        result ? textBlock(result, 'mono') : h('p', { class: 'muted small', text: 'No result yet.' }))));
    return h('li', { class: ['ev', 'ev-tool', freshSeqs.has(anchor.seq) && 'ev-new'], dataset: { seq: anchor.seq } }, details);
  }

  function renderThinking(ev) {
    const isRedacted = ev.redacted || !ev.preview;
    if (isRedacted) {
      return h('li', { class: 'ev ev-thinking' },
        rowHead('thinking', ev, h('span', { class: 'chip chip-redacted', title: 'The thinking text was not recorded', text: 'redacted' })));
    }
    const key = `e:${ev.seq}`;
    return h('li', { class: 'ev ev-thinking' },
      h('details', {
        open: expanded.has(key),
        onToggle: (e) => {
          if (e.currentTarget.open) expanded.add(key);
          else expanded.delete(key);
        },
      },
      h('summary', null,
        h('span', { class: 'ev-kind kind-thinking', text: 'Thinking' }),
        h('span', { class: 'tool-summary', text: truncate(ev.preview.replace(/\s+/g, ' '), THINKING_SUMMARY_MAX) }),
        h('span', { class: 'ev-time mono', text: formatClock(ev.timestamp) })),
      textBlock(ev, 'thinking-text')));
  }

  function renderEvent(ev) {
    const kind = ev.kind;
    if (kind === 'thinking') return renderThinking(ev);
    const cls = ['ev', `ev-${kind}`, (ev.isError || kind === 'error') && 'is-error', freshSeqs.has(ev.seq) && 'ev-new'];
    if (kind === 'compaction') {
      return h('li', { class: cls }, h('div', { class: 'divider' }, h('span', { text: 'Context compacted' })), ev.preview && textBlock(ev));
    }
    const extra = [];
    if (kind === 'text' && ev.model) extra.push(h('span', { class: 'ev-model mono', text: ev.model }));
    if (ev.toolName) extra.push(h('span', { class: 'mono muted', text: ev.toolName }));
    return h('li', { class: cls, dataset: { seq: ev.seq } }, rowHead(kind, ev, extra), textBlock(ev));
  }

  function render() {
    const items = groupEvents(events, opts);
    if (items.length === 0) {
      const noEvents = toolName ? `No ${toolName} calls in this agent yet.` : 'No events yet.';
      replace(list, h('li', { class: 'ev' }, emptyState(events.length ? 'No events match the toggles.' : noEvents)));
    } else {
      replace(list, items.map((item) => (item.type === 'tool' ? renderToolItem(item) : renderEvent(item.event))));
    }
    renderOlderBar();
  }

  /** Re-render while keeping the content under the viewport still. */
  function renderKeepingPosition() {
    const before = scroller.scrollHeight - scroller.scrollTop;
    render();
    scroller.scrollTop = scroller.scrollHeight - before;
  }

  // ---- loading -------------------------------------------------------------

  async function reload() {
    if (!ref) return;
    const myToken = ++token;
    events = [];
    hasBefore = false;
    newCount = 0;
    freshSeqs = new Set();
    fullContent.clear();
    renderPill();
    replace(list, h('li', { class: 'ev' }, loading('Loading events...')));
    try {
      const page = await api.events(ref, { limit: PAGE_LIMIT, includeMeta: !opts.hideMeta, toolName });
      if (myToken !== token) return;
      events = mergeEvents([], page.items || []);
      hasBefore = Boolean(page.hasBefore);
      render();
      scrollToBottom();
    } catch (err) {
      if (myToken !== token) return;
      replace(list, h('li', { class: 'ev' }, errorBox(err)));
    }
  }

  async function loadOlder() {
    if (!ref || !hasBefore || isLoadingOlder || events.length === 0) return;
    const myToken = token;
    isLoadingOlder = true;
    renderOlderBar();
    try {
      const page = await api.events(ref, { before: events[0].seq, limit: PAGE_LIMIT, includeMeta: !opts.hideMeta, toolName });
      if (myToken !== token) return;
      events = mergeEvents(events, page.items || []);
      hasBefore = Boolean(page.hasBefore);
      isLoadingOlder = false;
      renderKeepingPosition();
    } catch (err) {
      if (myToken !== token) return;
      isLoadingOlder = false;
      replace(olderBar, errorBox(err));
    }
  }

  function onScroll() {
    if (scroller.scrollTop < NEAR_TOP_PX) loadOlder();
    if (newCount > 0 && isAtBottom()) {
      newCount = 0;
      renderPill();
    }
  }

  function applyNew(incoming) {
    const known = new Set(events.map((e) => e.seq));
    const added = incoming.filter((e) => !known.has(e.seq));
    if (added.length === 0 && incoming.length === 0) return;
    const wasAtBottom = isAtBottom();
    events = mergeEvents(events, incoming);
    freshSeqs = new Set(added.map((e) => e.seq));
    render();
    if (wasAtBottom) {
      scrollToBottom();
    } else {
      newCount += groupEvents(added, opts).length;
      renderPill();
    }
  }

  /** Fetch everything after the last loaded seq. */
  async function catchUp() {
    if (!ref) return;
    if (events.length === 0) {
      await reload();
      return;
    }
    const myToken = token;
    let after = lastSeq(events);
    try {
      for (let round = 0; round < CATCH_UP_ROUNDS; round += 1) {
        const page = await api.events(ref, { after, limit: MAX_FETCH_LIMIT, includeMeta: !opts.hideMeta, toolName });
        if (myToken !== token) return;
        const items = page.items || [];
        applyNew(items);
        if (!page.hasAfter || items.length === 0) return;
        after = items[items.length - 1].seq;
      }
    } catch {
      // A failed catch-up is retried on the next append or resync.
    }
  }

  return {
    /** `toolName` limits every page and live append to that tool (G-FR-29a). */
    load(nextRef, key, info, { toolName: nextToolName = null } = {}) {
      ref = nextRef;
      agentKey = key;
      toolName = nextToolName || null;
      agentInfo = info || null;
      expanded.clear();
      renderHeader();
      reload();
    },
    setInfo(info) {
      if (info) agentInfo = { ...(agentInfo || {}), ...info };
      renderHeader();
    },
    onAppend(data) {
      if (!ref || !data || data.agentKey !== agentKey) return;
      const inline = Array.isArray(data.events) ? data.events : [];
      const isComplete = inline.length > 0 && inline.length >= (data.toSeq - data.fromSeq + 1);
      if (toolName) {
        // Filtered seqs are sparse, so a gap says nothing; resync repairs misses.
        if (!isComplete) catchUp();
        else applyNew(inline.filter((ev) => matchesToolFilter(ev, toolName)));
        return;
      }
      if (isComplete) {
        const gap = data.fromSeq > lastSeq(events) + 1 && events.length > 0;
        applyNew(inline);
        if (gap) catchUp();
      } else {
        catchUp();
      }
    },
    onReset(data) {
      if (data && data.agentKey === agentKey) reload();
    },
    resync() {
      catchUp();
    },
    key() {
      return agentKey;
    },
    toolName() {
      return toolName;
    },
  };
}
