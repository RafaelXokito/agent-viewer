// Stats panel (section 8.4): tables only, agent or subtree scope.

import { api } from '../api.js';
import { h, replace } from '../dom.js';
import { formatCompact, formatDuration, formatInt, formatUsd } from '../lib/format.js';
import { emptyState, errorBox, loading } from './widgets.js';

const NOT_READY_RETRY_MS = 3000;
const SCOPES = ['subtree', 'agent'];

function entries(obj) {
  return obj && typeof obj === 'object' ? Object.entries(obj) : [];
}

function kvTable(rows) {
  return h('table', { class: 'kv' }, h('tbody', null, rows.filter(Boolean).map(([label, value, cls]) =>
    h('tr', { class: cls }, h('th', { scope: 'row', text: label }), h('td', { class: 'num', text: value })))));
}

function dataTable(columns, rows) {
  if (rows.length === 0) return emptyState('None');
  return h('table', { class: 'data' },
    h('thead', null, h('tr', null, columns.map((c, i) => h('th', { scope: 'col', class: i > 0 && 'num', text: c })))),
    h('tbody', null, rows.map((r) => h('tr', null, r.map((cell, i) =>
      h('td', { class: [i === 0 ? 'mono name' : 'num'], title: i === 0 ? String(cell) : null, text: String(cell) }))))));
}

function block(title, ...body) {
  return h('section', { class: 'stats-block' }, h('h3', { text: title }), body);
}

function byCountDesc(getCount) {
  return (a, b) => getCount(b[1]) - getCount(a[1]) || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
}

function viaText(via) {
  const parts = entries(via).filter(([, n]) => n > 0).map(([k, n]) => `${k} ${n}`);
  return parts.length ? parts.join(', ') : '-';
}

export function renderStats(stats) {
  const t = stats.tokens || {};
  const errors = stats.errors || {};
  const agents = stats.agents || {};
  const parse = stats.parse || {};
  const unknown = entries(parse.unknownTypes).sort(byCountDesc((n) => n));

  return [
    block('Tokens', kvTable([
      ['Input', formatInt(t.input)],
      ['Output', formatInt(t.output)],
      ['  of which reasoning', formatInt(t.reasoning), 'sub'],
      ['Cache read', formatInt(t.cacheRead)],
      ['Cache creation', formatInt(t.cacheCreation)],
      ['Total', formatInt(t.total), 'total'],
      stats.costUsd != null && ['Recorded cost', formatUsd(stats.costUsd)],
    ])),
    block('By model', dataTable(['Model', 'Msgs', 'In', 'Out', 'C.rd', 'C.cr'],
      entries(stats.byModel).sort(byCountDesc((m) => m.messages || 0)).map(([name, m]) =>
        [name, formatInt(m.messages), formatCompact(m.input), formatCompact(m.output), formatCompact(m.cacheRead), formatCompact(m.cacheCreation)])),
    h('p', { class: 'muted small', text: 'C.rd = cache read, C.cr = cache creation' })),
    block('Tool calls', dataTable(['Tool', 'Calls', 'Errors', 'Time'],
      entries(stats.tools).sort(byCountDesc((x) => x.calls || 0)).map(([name, x]) =>
        [name, formatInt(x.calls), formatInt(x.errors), formatDuration(x.totalDurationMs)]))),
    block('Skills', dataTable(['Skill', 'Count', 'Via'],
      entries(stats.skills).sort(byCountDesc((x) => x.count || 0)).map(([name, x]) => [name, formatInt(x.count), viaText(x.via)]))),
    block('Subagent types spawned', dataTable(['Type', 'Count'],
      entries(stats.subagentTypes).sort(byCountDesc((n) => n)).map(([name, n]) => [name, formatInt(n)]))),
    block('Slash commands', dataTable(['Command', 'Count'],
      entries(stats.slashCommands).sort(byCountDesc((n) => n)).map(([name, n]) => [`/${name}`, formatInt(n)]))),
    block('Duration', kvTable([
      ['Wall clock', formatDuration(stats.durationMs)],
      ['Active', formatDuration(stats.activeDurationMs)],
      ['Turns', formatInt(stats.turns)],
      ['Agents', formatInt(agents.total)],
      ['Max depth', formatInt(agents.maxDepth)],
      ['Running', formatInt(agents.running)],
    ])),
    block('Errors', kvTable([
      ['Tool errors', formatInt(errors.toolErrors), errors.toolErrors > 0 && 'has-errors'],
      ['API errors', formatInt(errors.apiErrors), errors.apiErrors > 0 && 'has-errors'],
      ['Aborted', formatInt(errors.aborted), errors.aborted > 0 && 'has-errors'],
    ])),
    block('Parse',
      kvTable([
        ['Lines', formatInt(parse.lines)],
        ['Skipped', formatInt(parse.skipped), parse.skipped > 0 && 'has-errors'],
      ]),
      unknown.length > 0 && dataTable(['Unknown type', 'Count'], unknown.map(([name, n]) => [name, formatInt(n)]))),
  ];
}

/**
 * `load(ref, agentKey, sessionKey, isRoot)` shows stats for the selected
 * node. SSE stats.update messages for that key and scope replace the view.
 */
export function createStatsView(container) {
  let ref = null;
  let agentKey = null;
  let sessionKey = null;
  let isRoot = false;
  let scope = 'subtree';
  let token = 0;
  let retryTimer = null;

  const scopeBar = h('div', { class: 'scope-toggle', role: 'group', 'aria-label': 'Stats scope' });
  const body = h('div', { class: 'panel-body stats-body' });
  replace(container, h('div', { class: 'panel-header' }, h('h2', { text: 'Stats' }), scopeBar), body);

  function renderScopeBar() {
    replace(scopeBar, SCOPES.map((s) => h('button', {
      type: 'button',
      class: ['btn', 'btn-small', s === scope && 'active'],
      'aria-pressed': s === scope ? 'true' : 'false',
      text: s,
      onClick: () => {
        if (scope === s) return;
        scope = s;
        renderScopeBar();
        fetchStats();
      },
    })));
  }

  async function fetchStats() {
    if (!ref) return;
    clearTimeout(retryTimer);
    const myToken = ++token;
    replace(body, loading('Loading stats...'));
    try {
      const stats = await api.stats(ref, scope);
      if (myToken !== token) return;
      replace(body, renderStats(stats));
    } catch (err) {
      if (myToken !== token) return;
      if (err.code === 'not_ready') {
        replace(body, emptyState('Stats appear once this session is fully indexed.'));
        retryTimer = setTimeout(fetchStats, NOT_READY_RETRY_MS);
        return;
      }
      replace(body, errorBox(err));
    }
  }

  renderScopeBar();

  return {
    load(nextRef, nextAgentKey, nextSessionKey, nextIsRoot) {
      ref = nextRef;
      agentKey = nextAgentKey;
      sessionKey = nextSessionKey;
      isRoot = nextIsRoot;
      fetchStats();
    },
    onUpdate(data) {
      if (!data || !data.stats || data.scope !== scope) return;
      const isMine = data.key === agentKey || (isRoot && scope === 'subtree' && data.key === sessionKey);
      if (!isMine) return;
      token += 1;
      clearTimeout(retryTimer);
      replace(body, renderStats(data.stats));
    },
    resync() {
      fetchStats();
    },
    destroy() {
      token += 1;
      clearTimeout(retryTimer);
    },
  };
}
