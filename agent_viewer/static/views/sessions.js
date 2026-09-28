// Session list view (section 8.1): filters in the URL, live row upserts,
// cursor pagination.

import { api } from '../api.js';
import { h, replace } from '../dom.js';
import {
  durationBetween, formatCompact, formatDateTime, formatDuration, formatInt, formatRelative,
  projectLabel, truncate,
} from '../lib/format.js';
import {
  buildListHash, buildSessionHash, filtersToApiParams, normalizeFilters, removeSession,
  sessionMatches, STATUS_OPTIONS, upsertSession,
} from '../lib/state.js';
import { emptyState, errorBox, loading, sourceBadge, statusDot } from './widgets.js';

const PAGE_SIZE = 50;
const TEXT_DEBOUNCE_MS = 300;
const RELATIVE_TICK_MS = 5000;
const TITLE_MAX = 90;

const STATUS_LABELS = Object.freeze({
  recent: 'Active + finished 24h',
  all: 'All',
  running: 'Running',
  idle: 'Idle',
  stale: 'Stale',
  finished: 'Finished',
});

const COLUMNS = [
  ['', 'col-status'], ['Source', 'col-source'], ['Project', 'col-project'], ['Branch', 'col-branch'],
  ['Title', 'col-title'], ['Started', 'col-started'], ['Last activity', 'col-last'],
  ['Duration', 'col-num'], ['Agents', 'col-num'], ['Tokens', 'col-num'], ['Errors', 'col-num'],
];

function option(value, label, selected) {
  return h('option', { value, selected: value === selected, text: label });
}

export function createSessionsView(root, initialFilters) {
  let filters = normalizeFilters(initialFilters);
  let sessions = [];
  let nextCursor = null;
  let total = 0;
  let loadToken = 0;
  let projects = [];
  const highlight = new Set();
  const rowCache = new Map();

  const form = h('form', { class: 'filters', role: 'search', 'aria-label': 'Session filters' });
  const tbody = h('tbody');
  const table = h('table', { class: 'sessions' },
    h('thead', null, h('tr', null, COLUMNS.map(([label, cls]) => h('th', { class: cls, scope: 'col', text: label })))),
    tbody);
  const status = h('div', { class: 'list-status' });
  const footer = h('div', { class: 'list-footer' });
  replace(root, h('section', { class: 'list-view' }, form, status, h('div', { class: 'table-wrap' }, table), footer));
  document.title = 'Sessions - agent-viewer';

  const navigate = (next) => {
    const hash = buildListHash(next);
    if (hash !== location.hash) location.hash = hash;
  };

  let debounce = null;
  const change = (key, value, delay = 0) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => navigate({ ...filters, [key]: value }), delay);
  };
  const controls = {
    source: h('select', { onChange: (e) => change('source', e.target.value) },
      option('', 'Both', ''), option('claude', 'Claude Code', ''), option('omp', 'Oh My Pi', '')),
    project: h('select', { onChange: (e) => change('project', e.target.value) }),
    branch: h('input', {
      type: 'search', placeholder: 'substring', class: 'mono',
      onInput: (e) => change('branch', e.target.value, TEXT_DEBOUNCE_MS),
    }),
    since: h('input', { type: 'date', onChange: (e) => change('since', e.target.value) }),
    until: h('input', { type: 'date', onChange: (e) => change('until', e.target.value) }),
    status: h('select', { onChange: (e) => change('status', e.target.value) },
      STATUS_OPTIONS.map((st) => option(st, STATUS_LABELS[st], ''))),
    q: h('input', {
      type: 'search', placeholder: 'title, prompt, session id',
      onInput: (e) => change('q', e.target.value, TEXT_DEBOUNCE_MS),
    }),
  };
  const field = (label, control) => h('label', { class: 'field' }, h('span', { text: label }), control);
  replace(form,
    field('Source', controls.source), field('Project', controls.project), field('Branch', controls.branch),
    field('Since', controls.since), field('Until', controls.until), field('Status', controls.status),
    field('Search', controls.q),
    h('button', { type: 'button', class: 'btn', text: 'Reset', onClick: () => navigate({}) }));
  form.addEventListener('submit', (e) => e.preventDefault());

  function renderProjectOptions() {
    const opts = [option('', 'All projects', '')];
    for (const p of projects) {
      opts.push(option(p.project, `${projectLabel(p.cwd, p.project)} (${p.source}, ${p.sessionCount})`, ''));
    }
    const hasCurrent = !filters.project || projects.some((p) => p.project === filters.project);
    if (!hasCurrent) opts.push(option(filters.project, filters.project, ''));
    replace(controls.project, opts);
  }

  /** Push filter values into the controls, leaving the focused one alone. */
  function syncForm() {
    for (const [key, control] of Object.entries(controls)) {
      if (control !== document.activeElement && control.value !== filters[key]) control.value = filters[key];
    }
  }

  function buildRow(s) {
    const href = buildSessionHash(s.source, s.sessionId);
    const now = Date.now();
    const title = s.title || s.firstPrompt || s.sessionId;
    const tokens = s.tokens ? s.tokens.total : null;
    const row = h('tr', {
      class: ['session-row', highlight.has(s.key) && 'row-new', `row-${s.status}`],
      tabindex: 0,
      dataset: { key: s.key },
      onClick: (e) => {
        if (e.target.closest('a')) return;
        location.hash = href;
      },
      onKeydown: (e) => {
        if (e.key === 'Enter') location.hash = href;
      },
    },
    h('td', { class: 'col-status' }, statusDot(s.status)),
    h('td', { class: 'col-source' }, sourceBadge(s.source)),
    h('td', { class: 'col-project', title: s.cwd || s.project, text: projectLabel(s.cwd, s.project) }),
    h('td', { class: 'col-branch mono', title: s.gitBranch || '', text: s.gitBranch ? truncate(s.gitBranch, 40) : '-' }),
    h('td', { class: 'col-title' }, h('a', { href, title, text: truncate(title, TITLE_MAX) })),
    h('td', { class: 'col-started', title: s.startedAt || '', text: formatDateTime(s.startedAt) }),
    h('td', { class: 'col-last js-rel', title: s.lastActivityAt || '', dataset: { ts: s.lastActivityAt }, text: formatRelative(s.lastActivityAt, now) }),
    h('td', { class: 'col-num', text: formatDuration(durationBetween(s.startedAt, s.lastActivityAt)) }),
    h('td', { class: 'col-num', title: `${s.runningAgents || 0} running`, text: formatInt(s.agentCount) }),
    h('td', { class: 'col-num', title: s.indexed ? formatInt(tokens) : 'indexing', text: s.indexed ? formatCompact(tokens) : '...' }),
    h('td', { class: ['col-num', s.errors > 0 && 'has-errors'], text: s.indexed ? formatInt(s.errors) : '...' }));
    row.addEventListener('animationend', () => {
      highlight.delete(s.key);
      row.classList.remove('row-new');
    });
    return row;
  }

  function renderRows() {
    const rows = sessions.map((s) => {
      const cached = rowCache.get(s.key);
      if (cached && cached.session === s) return cached.row;
      const row = buildRow(s);
      rowCache.set(s.key, { session: s, row });
      return row;
    });
    const keep = new Set(sessions.map((s) => s.key));
    for (const key of rowCache.keys()) if (!keep.has(key)) rowCache.delete(key);
    tbody.replaceChildren(...rows);
    if (sessions.length === 0) {
      replace(status, emptyState('No sessions match these filters.'));
    } else {
      replace(status);
    }
    replace(footer,
      h('span', { class: 'muted', text: `${sessions.length} shown of ${Math.max(total, sessions.length)}` }),
      nextCursor && h('button', { type: 'button', class: 'btn', text: 'Load more', onClick: loadMore }));
  }

  async function load() {
    const token = ++loadToken;
    replace(status, loading('Loading sessions...'));
    try {
      const page = await api.sessions({ ...filtersToApiParams(filters), limit: PAGE_SIZE });
      if (token !== loadToken) return;
      sessions = page.items || [];
      nextCursor = page.nextCursor || null;
      total = page.total || sessions.length;
      rowCache.clear();
      renderRows();
    } catch (err) {
      if (token !== loadToken) return;
      replace(status, errorBox(err));
    }
  }

  async function loadMore() {
    if (!nextCursor) return;
    const token = loadToken;
    try {
      const page = await api.sessions({ ...filtersToApiParams(filters), limit: PAGE_SIZE }, nextCursor);
      if (token !== loadToken) return;
      const known = new Set(sessions.map((s) => s.key));
      sessions = [...sessions, ...(page.items || []).filter((s) => !known.has(s.key))];
      nextCursor = page.nextCursor || null;
      total = page.total || total;
      renderRows();
    } catch (err) {
      replace(status, errorBox(err));
    }
  }

  async function loadProjects() {
    try {
      const res = await api.projects();
      projects = res.items || [];
      renderProjectOptions();
      syncForm();
    } catch {
      // The project filter stays at "All projects"; the list still works.
    }
  }

  function onUpsert(session) {
    if (!session || !session.key) return;
    const isVisible = sessions.some((s) => s.key === session.key);
    if (!sessionMatches(session, filters, Date.now())) {
      if (isVisible) {
        sessions = removeSession(sessions, session.key);
        total = Math.max(0, total - 1);
        renderRows();
      }
      return;
    }
    const result = upsertSession(sessions, session, { hasMore: Boolean(nextCursor) });
    if (!result.inserted && !isVisible) return;
    if (result.isNew && result.inserted) {
      highlight.add(session.key);
      total += 1;
    }
    sessions = result.list;
    renderRows();
  }

  const ticker = setInterval(() => {
    const now = Date.now();
    for (const cell of tbody.querySelectorAll('.js-rel')) {
      cell.textContent = formatRelative(cell.dataset.ts, now);
    }
  }, RELATIVE_TICK_MS);

  renderProjectOptions();
  syncForm();
  loadProjects();
  load();

  return {
    setFilters(next) {
      filters = normalizeFilters(next);
      if (filters.project && !projects.some((p) => p.project === filters.project)) renderProjectOptions();
      syncForm();
      load();
    },
    onEvent(name, data) {
      if (name === 'session.upsert') onUpsert(data);
      if (name === 'session.remove' && data && data.key) {
        sessions = removeSession(sessions, data.key);
        renderRows();
      }
    },
    resync() {
      load();
    },
    destroy() {
      clearInterval(ticker);
      clearTimeout(debounce);
      loadToken += 1;
    },
  };
}
