import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  buildListHash, buildSessionHash, DEFAULT_FILTERS, filtersFromQuery, filtersToApiParams, filtersToQuery,
  mergeEvents, parseAgentKey, parseHash, removeSession, sessionMatches, upsertSession,
} from '../lib/state.js';

const TREE_DEFAULTS = Object.freeze({ layout: 'tree', tools: 'all', drawer: null, tool: null });

const session = (over = {}) => ({
  key: 'claude:s-main',
  source: 'claude',
  sessionId: 's-main',
  project: '-tmp-demo',
  gitBranch: 'feature/DEMO-1',
  title: 'Demo session',
  firstPrompt: 'Spawn two helpers',
  lastActivityAt: '2026-09-25T10:01:00.000Z',
  status: 'finished',
  ...over,
});

test('parseHash routes the list, a session, and a selected agent', () => {
  assert.deepEqual(parseHash(''), { view: 'list', filters: DEFAULT_FILTERS });
  assert.deepEqual(parseHash('#/s/claude/s-main'), {
    view: 'session', source: 'claude', sessionId: 's-main', agentId: 'main', ...TREE_DEFAULTS,
  });
  assert.deepEqual(parseHash('#/s/claude/s-main/a/a2222222222222222'),
    { view: 'session', source: 'claude', sessionId: 's-main', agentId: 'a2222222222222222', ...TREE_DEFAULTS });
});

test('parseHash reads layout, tools, drawer and tool for graph routes', () => {
  assert.deepEqual(parseHash('#/s/claude/s-main/a/a2222222222222222?layout=graph&drawer=timeline&tool=Bash'), {
    view: 'session', source: 'claude', sessionId: 's-main', agentId: 'a2222222222222222',
    layout: 'graph', tools: 'all', drawer: 'timeline', tool: 'Bash',
  });
  const r = parseHash('#/s/claude/s-main/a/a1111111111111111?layout=graph&tools=selected&drawer=stats');
  assert.equal(r.tools, 'selected');
  assert.equal(r.drawer, 'stats');
  assert.equal(r.tool, null);
});

test('parseHash falls back on invalid layout, tools and drawer values', () => {
  const r = parseHash('#/s/claude/s-main?layout=canvas&tools=many');
  assert.equal(r.layout, 'tree');
  assert.equal(r.tools, 'all', 'an invalid tools value falls back to the default, all');
  const g = parseHash('#/s/claude/s-main?layout=graph&drawer=weird&tool=Read');
  assert.equal(g.drawer, 'timeline', 'an invalid drawer value opens the timeline');
  assert.equal(g.tool, 'Read');
  assert.equal(parseHash('#/s/claude/s-main?layout=graph&drawer=').drawer, 'timeline');
});

test('parseHash drops tool without layout=graph and drawer=timeline', () => {
  assert.equal(parseHash('#/s/claude/s-main?layout=graph&tool=Bash').tool, null);
  assert.equal(parseHash('#/s/claude/s-main?layout=graph&drawer=stats&tool=Bash').tool, null);
  assert.equal(parseHash('#/s/claude/s-main?drawer=timeline&tool=Bash').tool, null);
  assert.equal(parseHash('#/s/claude/s-main?layout=graph&drawer=timeline&tool=').tool, null);
  assert.equal(parseHash(`#/s/claude/s-main?layout=graph&drawer=timeline&tool=${'x'.repeat(201)}`).tool, null);
  assert.equal(parseHash(`#/s/claude/s-main?layout=graph&drawer=timeline&tool=${'x'.repeat(200)}`).tool, 'x'.repeat(200));
});

test('parseHash drops the drawer in the tree layout', () => {
  assert.equal(parseHash('#/s/claude/s-main?drawer=stats').drawer, null);
  assert.equal(parseHash('#/s/claude/s-main?layout=tree&tools=none').tools, 'none');
});

test('buildSessionHash writes layout, tools, drawer, tool in a fixed order and omits defaults', () => {
  assert.equal(buildSessionHash('claude', 's-main', 'main', { layout: 'tree', tools: 'all', drawer: null, tool: null }),
    '#/s/claude/s-main');
  assert.equal(buildSessionHash('claude', 's-main', 'main', { layout: 'graph' }), '#/s/claude/s-main?layout=graph');
  assert.equal(
    buildSessionHash('claude', 's-main', 'a2222222222222222', { tool: 'Bash', drawer: 'timeline', tools: 'selected', layout: 'graph' }),
    '#/s/claude/s-main/a/a2222222222222222?layout=graph&tools=selected&drawer=timeline&tool=Bash');
  assert.equal(buildSessionHash('claude', 's-main', 'main', { layout: 'graph', drawer: 'stats', tool: 'Bash' }),
    '#/s/claude/s-main?layout=graph&drawer=stats', 'tool is dropped without drawer=timeline');
  assert.equal(buildSessionHash('claude', 's-main', 'main', { layout: 'tree', tools: 'none', drawer: 'timeline', tool: 'Bash' }),
    '#/s/claude/s-main?tools=none', 'the tree layout drops drawer and tool');
});

test('buildSessionHash round-trips every graph field through parseHash', () => {
  for (const tool of ['Bash', 'my tool', 'a&b=c', 'x#y', 'mcp/one', '100%', 'plus+sign']) {
    const opts = { layout: 'graph', tools: 'none', drawer: 'timeline', tool };
    const route = parseHash(buildSessionHash('omp', 'o-root', 'Parent/Child', opts));
    assert.deepEqual(route, { view: 'session', source: 'omp', sessionId: 'o-root', agentId: 'Parent/Child', ...opts });
  }
});

test('parseHash splits on "/" before decoding so an encoded agentId keeps its slash', () => {
  const route = parseHash('#/s/omp/o-root/a/Parent%2FChild');
  assert.equal(route.agentId, 'Parent/Child');
});

test('parseHash falls back to the list for malformed session routes', () => {
  assert.equal(parseHash('#/s/claude').view, 'list');
  assert.equal(parseHash('#/s/claude/s-main/x/y').view, 'list');
  assert.equal(parseHash('#/s/%E0%A4%A/s-main').view, 'list');
});

test('buildSessionHash round-trips through parseHash, including slashes', () => {
  const hash = buildSessionHash('omp', 'o-root', 'Parent/Child');
  assert.equal(hash, '#/s/omp/o-root/a/Parent%2FChild');
  assert.equal(parseHash(hash).agentId, 'Parent/Child');
  assert.equal(buildSessionHash('claude', 's-main', 'main'), '#/s/claude/s-main');
});

test('filters round-trip through the hash query, dropping defaults and invalid values', () => {
  const filters = filtersFromQuery('source=omp&status=running&q=demo&bogus=1');
  assert.equal(filters.source, 'omp');
  assert.equal(filters.status, 'running');
  assert.equal(filters.q, 'demo');
  assert.equal(filtersToQuery(filters), 'source=omp&status=running&q=demo');
  assert.equal(buildListHash(DEFAULT_FILTERS), '#/');
  assert.deepEqual(parseHash(buildListHash(filters)).filters, filters);
  assert.equal(filtersFromQuery('source=evil&status=nope').source, '');
  assert.equal(filtersFromQuery('status=nope').status, 'recent');
});

test('filtersToApiParams sends recent by default and no status for all', () => {
  assert.deepEqual(filtersToApiParams(DEFAULT_FILTERS), { status: 'recent' });
  assert.deepEqual(filtersToApiParams({ status: 'all', branch: 'DEMO' }), { branch: 'DEMO' });
});

test('sessionMatches mirrors the server filters for live upserts', () => {
  const now = Date.parse('2026-09-25T12:00:00.000Z');
  const s = session();
  assert.equal(sessionMatches(s, DEFAULT_FILTERS, now), true, 'finished within 24h is recent');
  assert.equal(sessionMatches(s, DEFAULT_FILTERS, now + 2 * 86400000), false, 'finished long ago is not recent');
  assert.equal(sessionMatches(session({ status: 'stale' }), DEFAULT_FILTERS, now + 9e9), true);
  assert.equal(sessionMatches(s, { status: 'all', source: 'omp' }, now), false);
  assert.equal(sessionMatches(s, { status: 'all', branch: 'demo-1' }, now), true, 'branch is case-insensitive');
  assert.equal(sessionMatches(session({ gitBranch: null }), { status: 'all', branch: 'x' }, now), false);
  assert.equal(sessionMatches(s, { status: 'all', q: 'HELPERS' }, now), true);
  assert.equal(sessionMatches(s, { status: 'all', q: 'nothing' }, now), false);
  assert.equal(sessionMatches(s, { status: 'running' }, now), false);
  assert.equal(sessionMatches(s, { status: 'all', project: '-tmp-other' }, now), false);
});

test('sessionMatches treats a date-only until as inclusive of that day', () => {
  const now = Date.parse('2026-09-26T00:00:00.000Z');
  const s = session();
  assert.equal(sessionMatches(s, { status: 'all', until: '2026-09-25' }, now), true);
  assert.equal(sessionMatches(s, { status: 'all', until: '2026-09-24' }, now), false);
  assert.equal(sessionMatches(s, { status: 'all', since: '2026-09-26' }, now), false);
  assert.equal(sessionMatches(s, { status: 'all', since: '2026-09-25' }, now), true);
});

test('upsertSession keeps lastActivityAt descending order and replaces in place', () => {
  const a = session({ key: 'a', lastActivityAt: '2026-09-25T10:00:00.000Z' });
  const b = session({ key: 'b', lastActivityAt: '2026-09-25T09:00:00.000Z' });
  const list = [a, b];
  const newer = session({ key: 'c', lastActivityAt: '2026-09-25T11:00:00.000Z' });
  const r1 = upsertSession(list, newer);
  assert.deepEqual(r1.list.map((s) => s.key), ['c', 'a', 'b']);
  assert.equal(r1.isNew, true);
  assert.deepEqual(list.map((s) => s.key), ['a', 'b'], 'input list is not mutated');

  const bumped = session({ key: 'b', lastActivityAt: '2026-09-25T12:00:00.000Z' });
  const r2 = upsertSession(r1.list, bumped);
  assert.deepEqual(r2.list.map((s) => s.key), ['b', 'c', 'a']);
  assert.equal(r2.isNew, false);
});

test('upsertSession leaves out rows that belong to a later page', () => {
  const a = session({ key: 'a', lastActivityAt: '2026-09-25T10:00:00.000Z' });
  const old = session({ key: 'z', lastActivityAt: '2026-09-20T10:00:00.000Z' });
  const r = upsertSession([a], old, { hasMore: true });
  assert.equal(r.inserted, false);
  assert.deepEqual(r.list.map((s) => s.key), ['a']);
  assert.deepEqual(removeSession([a], 'a'), []);
});

test('mergeEvents dedupes by seq, sorts, and lets the newer copy win', () => {
  const existing = [{ seq: 3, v: 'old' }, { seq: 4 }];
  const merged = mergeEvents(existing, [{ seq: 1 }, { seq: 3, v: 'new' }, { seq: 5 }, { nope: true }]);
  assert.deepEqual(merged.map((e) => e.seq), [1, 3, 4, 5]);
  assert.equal(merged[1].v, 'new');
  assert.equal(mergeEvents(existing, []), existing);
  assert.equal(existing[0].v, 'old', 'existing events are not mutated');
});

test('parseAgentKey splits source, sessionId and an agentId that may hold colons', () => {
  assert.deepEqual(parseAgentKey('claude:s-main:a1111111111111111'),
    { source: 'claude', sessionId: 's-main', agentId: 'a1111111111111111' });
  assert.deepEqual(parseAgentKey('omp:o-root:Parent/Child'), { source: 'omp', sessionId: 'o-root', agentId: 'Parent/Child' });
  assert.equal(parseAgentKey('claude:s-main'), null);
  assert.equal(parseAgentKey('claude:s-main:'), null);
  assert.equal(parseAgentKey(null), null);
});
