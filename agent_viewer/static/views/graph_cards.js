// Element builders for the graph canvas (FEATURE-graph-canvas 10.3, 10.4,
// 10.8): agent and session cards, tool, skill and "+N more" pills, edge
// paths, arrow markers and the legend. Transcript text only enters through
// dom.js (`textContent` and non-URL attributes).

import { h, setVar, svg } from '../dom.js';
import { formatCompact, formatInt, shortId, truncate } from '../lib/format.js';
import { agentLabel, agentTypeText, skillBadge } from '../lib/graph_model.js';
import { nodeWarnings, warningText } from '../lib/tree.js';
import { statusDot } from './widgets.js';

const DESCRIPTION_MAX = 48;
const TOOL_NAME_MAX = 20;
const STATUS_WORDS = Object.freeze({ running: 'running', idle: 'idle', stale: 'stale', finished: 'finished' });

let descSeq = 0;

function statusWord(status) {
  return STATUS_WORDS[status] || 'unknown status';
}

/** Place an element at world coordinates through CSS custom properties. */
export function placeAt(el, pos) {
  setVar(el, '--x', `${pos.x}px`);
  setVar(el, '--y', `${pos.y}px`);
  return el;
}

function errorTotal(node) {
  const e = node.errors || {};
  return (e.toolErrors || 0) + (e.apiErrors || 0);
}

function badges(node) {
  const warnings = nodeWarnings(node);
  return [
    warnings.length > 0 && h('span', {
      class: 'gcard-warn',
      title: warnings.map((w) => `${w}: ${warningText(w)}`).join('\n'),
      'aria-hidden': 'true',
      text: '⚠',
    }),
    node.isFork && h('span', { class: 'badge', text: 'fork' }),
    node.forkedSkill && h('span', { class: 'badge', title: node.forkedSkill, text: `skill: ${truncate(node.forkedSkill, 14)}` }),
    node.stoppedByUser && h('span', { class: 'badge', text: 'stopped' }),
  ];
}

function modelText(models) {
  const list = Array.isArray(models) ? models : [];
  if (list.length === 0) return null;
  return list.length > 1 ? `${list[0]} +${list.length - 1}` : list[0];
}

function metaRow(node) {
  const models = modelText(node.models);
  const errors = errorTotal(node);
  const resumes = node.resumeCount || 0;
  return h('div', { class: 'gcard-meta' },
    models && h('span', { class: 'gcard-model mono', title: (node.models || []).join(', '), text: models }),
    h('span', { title: `${formatInt(node.tokensTotal)} tokens`, text: `${formatCompact(node.tokensTotal)} tok` }),
    h('span', { title: `${formatInt(node.toolCalls)} tool calls`, text: `${formatCompact(node.toolCalls || 0)} ${node.toolCalls === 1 ? 'tool' : 'tools'}` }),
    errors > 0 && h('span', { class: 'has-errors', title: `${formatInt(errors)} errors`, text: `${formatCompact(errors)} err` }),
    resumes > 0 && h('span', { class: 'badge badge-resume', title: `${resumes} resume(s)`, text: `↻${resumes}` }));
}

function collapseControl(entry, onToggle) {
  if (!entry.hasChildren) return null;
  const noun = entry.hidden === 1 ? 'agent' : 'agents';
  const label = entry.collapsed ? `+${entry.hidden} ${noun}` : '-';
  return h('button', {
    type: 'button',
    class: 'gcard-collapse',
    tabindex: -1,
    'aria-label': entry.collapsed ? `Expand, ${entry.hidden} hidden ${noun}` : 'Collapse',
    text: label,
    onClick: (e) => {
      e.stopPropagation();
      onToggle(entry.node.id);
    },
    onDblclick: (e) => e.stopPropagation(),
  });
}

/**
 * Agent card (240 x 88). `entry` is a deriveVisible agent row; `opts` carries
 * `{selected, tabbable, description, sessionTitle, onToggle}`.
 */
export function agentCard(entry, opts) {
  const { node } = entry;
  const label = agentLabel(node);
  const type = agentTypeText(node);
  const text = node.isRoot ? (opts.sessionTitle || node.description || node.name || 'session root') : (node.description || node.name || '');
  const descId = `gdesc-${(descSeq += 1)}`;
  const card = h('div', {
    class: ['gnode', 'gcard', `status-${STATUS_WORDS[node.status] ? node.status : 'unknown'}`,
      opts.selected && 'is-selected', node.missing && 'is-missing', node.linkedBy === 'orphan' && 'is-orphan'],
    role: 'treeitem',
    tabindex: opts.tabbable ? 0 : -1,
    'aria-level': entry.level + 1,
    'aria-selected': opts.selected ? 'true' : 'false',
    'aria-expanded': entry.hasChildren ? (entry.collapsed ? 'false' : 'true') : null,
    'aria-label': `${label}, ${type}, ${statusWord(node.status)}`,
    'aria-describedby': descId,
    title: text ? `${label}: ${text}` : label,
    dataset: { id: node.id, kind: 'agent' },
  },
  h('div', { class: 'gcard-head' },
    statusDot(node.status),
    h('span', { class: 'gcard-type', text: type }),
    !node.isRoot && h('code', { class: 'gcard-id mono', title: String(node.agentId || ''), text: shortId(node.agentId) }),
    badges(node)),
  h('div', { class: 'gcard-desc', text: truncate(text, DESCRIPTION_MAX) }),
  metaRow(node),
  collapseControl(entry, opts.onToggle),
  h('span', { id: descId, class: 'gsr-only', text: opts.description || '' }));
  return card;
}

/** Session card (240 x 64) for a continuation-chain neighbour. */
export function sessionCard(node) {
  const relation = node.relation === 'predecessor' ? 'continued from' : 'continued in';
  const hops = node.hops > 1 ? ` (${node.hops} hops)` : '';
  const sessionId = String(node.sessionKey || '').split(':').slice(1).join(':');
  const title = node.missing ? 'not found' : (node.title || shortId(sessionId, 12));
  const card = h('button', {
    type: 'button',
    class: ['gnode', 'gsession', node.missing && 'is-missing'],
    disabled: Boolean(node.missing),
    'aria-label': `${relation}${hops}: ${title}`,
    title: node.sessionKey || '',
    dataset: { id: node.id, kind: 'session' },
  },
  h('div', { class: 'gsession-rel', text: `${relation}${hops}` }),
  h('div', { class: 'gsession-title', text: truncate(title, 30) }),
  !node.missing && h('div', { class: 'gsession-meta' },
    statusDot(node.status),
    h('span', { text: node.status || 'unknown' }),
    Number.isFinite(node.agentCount) && h('span', { text: `${formatInt(node.agentCount)} agents` })));
  return card;
}

/** Tool pill (160 x 26); `active` when it has pending calls and its owner is running. */
export function toolPill(node, active) {
  const errors = node.errors || 0;
  return h('div', {
    class: ['gnode', 'gpill', 'gtool', active && 'is-active', errors > 0 && 'has-err'],
    'aria-hidden': 'true',
    title: `${node.name}: ${formatInt(node.calls)} calls, ${formatInt(errors)} errors${node.pending ? `, ${node.pending} pending` : ''}`,
    dataset: { id: node.id, kind: 'tool' },
  },
  active && h('span', { class: 'gpill-active' }),
  h('span', { class: 'gpill-name', text: truncate(node.name, TOOL_NAME_MAX) }),
  h('span', { class: 'gpill-count', text: `x${formatCompact(node.calls || 0)}` }),
  errors > 0 && h('span', { class: 'gpill-err', text: `${formatCompact(errors)} err` }));
}

export function skillPill(node) {
  const badge = skillBadge(node);
  return h('div', {
    class: ['gnode', 'gpill', 'gskill', badge.isPreloaded && 'is-preloaded'],
    'aria-hidden': 'true',
    title: badge.title,
    dataset: { id: node.id, kind: 'skill' },
  },
  h('span', { class: 'gpill-name', text: truncate(node.name, TOOL_NAME_MAX) }),
  h('span', { class: 'gpill-count', text: badge.text }));
}

export function morePill(node) {
  return h('div', {
    class: ['gnode', 'gpill', 'gmore'],
    'aria-hidden': 'true',
    title: `${node.count} more tools and skills, ${formatInt(node.calls)} calls`,
    dataset: { id: node.id, kind: 'more' },
  }, h('span', { class: 'gpill-name', text: `+${node.count} more (${formatCompact(node.calls)} calls)` }));
}

const EDGE_CLASSES = Object.freeze({
  spawn: 'gedge-spawn',
  resume: 'gedge-resume',
  continued_in: 'gedge-continued',
  uses_tool: 'gedge-tool',
  uses_more: 'gedge-tool',
  uses_skill: 'gedge-skill',
});

const EDGE_MARKERS = Object.freeze({ resume: 'resume', continued_in: 'continued' });

/** SVG arrow markers; `suffix` keeps ids unique per canvas instance. */
export function edgeDefs(suffix) {
  const marker = (name) => svg('marker', {
    id: `garrow-${name}-${suffix}`,
    class: `gmarker gmarker-${name}`,
    viewBox: '0 0 10 10',
    refX: 9,
    refY: 5,
    markerUnits: 'userSpaceOnUse',
    markerWidth: 9,
    markerHeight: 9,
    orient: 'auto-start-reverse',
  }, svg('path', { d: 'M0 0L10 5L0 10z' }));
  return svg('defs', null, marker('resume'), marker('continued'));
}

/**
 * One edge: a `g` with the path and, for resume edges above 1, a count label.
 * `active` animates the line while its tool call or child agent is running.
 */
export function edgeElement(edge, geometry, suffix, active = false) {
  const marker = EDGE_MARKERS[edge.kind];
  const path = svg('path', {
    class: ['gedge', EDGE_CLASSES[edge.kind] || 'gedge-other', edge.kind === 'spawn' && edge.linkedBy === 'orphan' && 'is-orphan',
      active && 'is-active'],
    d: geometry.d,
    'marker-end': marker ? `url(#garrow-${marker}-${suffix})` : null,
  });
  const count = edge.kind === 'resume' ? edge.count || 0 : 0;
  const label = count > 1 && geometry.label
    ? svg('text', { class: 'gedge-label', x: geometry.label.x, y: geometry.label.y, text: `x${count}` })
    : null;
  return svg('g', { class: ['gedge-group', edge.kind.startsWith('uses_') && 'gedge-usage'], dataset: { id: edge.id } }, path, label);
}

function legendEdge(kind, text, suffix, extra = {}) {
  const geometry = { d: 'M2 8H46' };
  return h('li', null,
    svg('svg', { class: 'glegend-edge', width: 50, height: 16, viewBox: '0 0 50 16', 'aria-hidden': 'true' },
      edgeDefs(`${suffix}-${kind}${extra.linkedBy || ''}`),
      edgeElement({ id: kind, kind, count: 0, ...extra }, geometry, `${suffix}-${kind}${extra.linkedBy || ''}`)),
    h('span', { text }));
}

function legendSwatch(cls, text) {
  return h('li', null, h('span', { class: `glegend-swatch ${cls}`, 'aria-hidden': 'true' }), h('span', { text }));
}

/** The legend panel content (10.8). */
export function legendContent(suffix) {
  return [
    h('h3', { text: 'Nodes' }),
    h('ul', null,
      legendSwatch('sw-agent', 'agent card'),
      legendSwatch('sw-session', 'session card (continuation)'),
      legendSwatch('sw-tool', 'tool node (calls, errors)'),
      legendSwatch('sw-skill', 'skill node'),
      legendSwatch('sw-more', '"+N more" tools and skills')),
    h('h3', { text: 'Status' }),
    h('ul', null, ['running', 'idle', 'stale', 'finished'].map((s) => h('li', null, statusDot(s), h('span', { text: s })))),
    h('h3', { text: 'Edges' }),
    h('ul', null,
      legendEdge('spawn', 'spawned', suffix),
      legendEdge('spawn', 'spawned, orphan (no spawn evidence)', suffix, { linkedBy: 'orphan' }),
      legendEdge('resume', 'resumed (SendMessage), count when above 1', suffix),
      legendEdge('continued_in', 'continued in a later session', suffix),
      legendEdge('uses_tool', 'uses tool', suffix),
      legendEdge('uses_skill', 'uses skill', suffix)),
    h('p', { class: 'muted small', text: 'Oh My Pi skill reads appear both as a read tool call and as a skill node.' }),
  ];
}
