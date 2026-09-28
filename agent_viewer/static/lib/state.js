// Pure state helpers: hash routes, list filters, session list upserts,
// event page merging and key parsing. No DOM access.

const RECENT_WINDOW_MS = 24 * 60 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

export const SOURCES = Object.freeze(['', 'claude', 'omp']);
export const STATUS_OPTIONS = Object.freeze(['recent', 'all', 'running', 'idle', 'stale', 'finished']);
const ACTIVE_STATUSES = new Set(['running', 'idle', 'stale']);

export const DEFAULT_FILTERS = Object.freeze({
  source: '',
  project: '',
  branch: '',
  since: '',
  until: '',
  status: 'recent',
  q: '',
});
const FILTER_KEYS = Object.keys(DEFAULT_FILTERS);

function safeDecode(segment) {
  try {
    return decodeURIComponent(segment);
  } catch {
    return null;
  }
}

/** Normalize raw filter values: unknown keys dropped, invalid enums reset. */
export function normalizeFilters(raw) {
  const out = { ...DEFAULT_FILTERS };
  for (const key of FILTER_KEYS) {
    const value = raw && raw[key];
    if (typeof value === 'string') out[key] = value.trim();
  }
  if (!SOURCES.includes(out.source)) out.source = DEFAULT_FILTERS.source;
  if (!STATUS_OPTIONS.includes(out.status)) out.status = DEFAULT_FILTERS.status;
  return Object.freeze(out);
}

export function filtersFromQuery(query) {
  const params = new URLSearchParams(query || '');
  const raw = {};
  for (const key of FILTER_KEYS) {
    if (params.has(key)) raw[key] = params.get(key);
  }
  return normalizeFilters(raw);
}

/** Query string with only the non-default filters, in a stable order. */
export function filtersToQuery(filters) {
  const f = normalizeFilters(filters);
  const params = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    if (f[key] !== DEFAULT_FILTERS[key]) params.set(key, f[key]);
  }
  return params.toString();
}

/** Parameters for GET /api/sessions. "all" sends no status at all. */
export function filtersToApiParams(filters) {
  const f = normalizeFilters(filters);
  const params = {};
  for (const key of FILTER_KEYS) {
    if (key === 'status') continue;
    if (f[key] !== '') params[key] = f[key];
  }
  if (f.status !== 'all') params.status = f.status;
  return params;
}

/**
 * Parse a location hash into a route.
 * The path is split on "/" before decoding, so an encoded "/" inside an
 * Oh My Pi agentId survives.
 */
export function parseHash(hash) {
  let h = String(hash || '').replace(/^#/, '');
  if (!h.startsWith('/')) h = `/${h}`;
  const qi = h.indexOf('?');
  const path = qi >= 0 ? h.slice(0, qi) : h;
  const query = qi >= 0 ? h.slice(qi + 1) : '';
  const segs = path.split('/').filter((s) => s !== '');

  if (segs[0] === 's' && (segs.length === 3 || (segs.length === 5 && segs[3] === 'a'))) {
    const source = safeDecode(segs[1]);
    const sessionId = safeDecode(segs[2]);
    const agentId = segs.length === 5 ? safeDecode(segs[4]) : null;
    const isValid = source && sessionId && (segs.length === 3 || agentId);
    if (isValid) {
      return { view: 'session', source, sessionId, agentId: agentId || 'main', ...viewStateFromQuery(query) };
    }
  }
  return { view: 'list', filters: filtersFromQuery(query) };
}

export const LAYOUTS = Object.freeze(['tree', 'graph']);
export const TOOLS_MODES = Object.freeze(['none', 'selected', 'all']);
export const DRAWER_TABS = Object.freeze(['timeline', 'stats']);
export const MAX_TOOL_FILTER_CHARS = 200;
const DEFAULT_TOOLS_MODE = 'all';

/**
 * Normalize the session view state of the hash query (graph feature G-FR-2
 * to G-FR-3c): invalid values fall back, the drawer only exists in the graph
 * layout, and the tool filter only with the drawer on the timeline.
 */
export function normalizeViewState(raw) {
  const r = raw || {};
  const layout = r.layout === 'graph' ? 'graph' : 'tree';
  const tools = TOOLS_MODES.includes(r.tools) ? r.tools : DEFAULT_TOOLS_MODE;
  let drawer = null;
  if (layout === 'graph' && r.drawer != null) drawer = DRAWER_TABS.includes(r.drawer) ? r.drawer : 'timeline';
  const isToolValid = typeof r.tool === 'string' && r.tool !== '' && r.tool.length <= MAX_TOOL_FILTER_CHARS;
  const tool = drawer === 'timeline' && isToolValid ? r.tool : null;
  return { layout, tools, drawer, tool };
}

function viewStateFromQuery(query) {
  const params = new URLSearchParams(query || '');
  const raw = {};
  for (const key of ['layout', 'tools', 'drawer', 'tool']) {
    if (params.has(key)) raw[key] = params.get(key);
  }
  return normalizeViewState(raw);
}

export function buildListHash(filters) {
  const q = filtersToQuery(filters);
  return q ? `#/?${q}` : '#/';
}

/**
 * Hash of a session route. `view` holds the optional {layout, tools, drawer,
 * tool}; they are normalized and written in that fixed order with defaults
 * omitted, so equal states give equal URLs.
 */
export function buildSessionHash(source, sessionId, agentId, view) {
  let path = `#/s/${encodeURIComponent(source)}/${encodeURIComponent(sessionId)}`;
  if (agentId && agentId !== 'main') path = `${path}/a/${encodeURIComponent(agentId)}`;
  const v = normalizeViewState(view);
  const params = new URLSearchParams();
  if (v.layout !== 'tree') params.set('layout', v.layout);
  if (v.tools !== DEFAULT_TOOLS_MODE) params.set('tools', v.tools);
  if (v.drawer) params.set('drawer', v.drawer);
  if (v.tool) params.set('tool', v.tool);
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

/** Agent key "<source>:<sessionId>:<agentId>"; the agentId may contain ":". */
export function parseAgentKey(key) {
  if (typeof key !== 'string') return null;
  const first = key.indexOf(':');
  const second = first >= 0 ? key.indexOf(':', first + 1) : -1;
  if (first <= 0 || second <= first + 1 || second === key.length - 1) return null;
  return {
    source: key.slice(0, first),
    sessionId: key.slice(first + 1, second),
    agentId: key.slice(second + 1),
  };
}

export function makeAgentKey(source, sessionId, agentId) {
  return `${source}:${sessionId}:${agentId}`;
}

export function makeSessionKey(source, sessionId) {
  return `${source}:${sessionId}`;
}

function includesCi(haystack, needle) {
  return haystack != null && String(haystack).toLowerCase().includes(needle.toLowerCase());
}

function boundMs(value, isUpper) {
  if (!value) return NaN;
  const ms = Date.parse(value);
  if (Number.isNaN(ms)) return NaN;
  // A date-only upper bound includes that whole day.
  return isUpper && DATE_ONLY.test(value) ? ms + DAY_MS : ms;
}

function statusMatches(session, status, nowMs) {
  if (status === 'all') return true;
  if (status === 'recent') {
    if (ACTIVE_STATUSES.has(session.status)) return true;
    const last = Date.parse(session.lastActivityAt);
    return session.status === 'finished' && !Number.isNaN(last) && nowMs - last <= RECENT_WINDOW_MS;
  }
  return session.status === status;
}

/**
 * Client-side mirror of the /api/sessions filters, used to decide whether a
 * live session.upsert belongs in the visible list.
 */
export function sessionMatches(session, filters, nowMs) {
  const f = normalizeFilters(filters);
  if (!session) return false;
  if (f.source && session.source !== f.source) return false;
  if (f.project && session.project !== f.project) return false;
  if (f.branch && !includesCi(session.gitBranch, f.branch)) return false;
  const last = Date.parse(session.lastActivityAt);
  const since = boundMs(f.since, false);
  const until = boundMs(f.until, true);
  if (!Number.isNaN(since) && !(last >= since)) return false;
  if (!Number.isNaN(until) && !(last < until)) return false;
  if (!statusMatches(session, f.status, nowMs)) return false;
  if (f.q) {
    const hit = [session.title, session.firstPrompt, session.sessionId].some((v) => includesCi(v, f.q));
    if (!hit) return false;
  }
  return true;
}

/** Sort order of /api/sessions: lastActivityAt descending, ties by key. */
export function compareSessions(a, b) {
  const ta = Date.parse(a.lastActivityAt) || 0;
  const tb = Date.parse(b.lastActivityAt) || 0;
  if (ta !== tb) return tb - ta;
  if (a.key < b.key) return -1;
  if (a.key > b.key) return 1;
  return 0;
}

/**
 * Insert or replace a session in a sorted list, returning a new list.
 * When `hasMore` is true a session that would sort after the last loaded row
 * belongs to a later page and is left out.
 */
export function upsertSession(list, session, { hasMore = false } = {}) {
  const without = list.filter((s) => s.key !== session.key);
  const isNew = without.length === list.length;
  let index = without.findIndex((s) => compareSessions(session, s) < 0);
  if (index === -1) {
    if (hasMore) return { list: without, isNew, inserted: false };
    index = without.length;
  }
  const next = [...without.slice(0, index), session, ...without.slice(index)];
  return { list: next, isNew, inserted: true };
}

export function removeSession(list, key) {
  return list.filter((s) => s.key !== key);
}

/** Merge event pages by seq, sorted ascending; incoming wins on duplicates. */
export function mergeEvents(existing, incoming) {
  if (!incoming || incoming.length === 0) return existing;
  const bySeq = new Map();
  for (const ev of existing) bySeq.set(ev.seq, ev);
  for (const ev of incoming) {
    if (ev && Number.isInteger(ev.seq)) bySeq.set(ev.seq, ev);
  }
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}
