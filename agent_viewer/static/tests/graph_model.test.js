import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  MAX_VISIBLE_NODES, MORE, TOOLS_VISIBLE, agentLabel, agentTypeText, describeAgent, deriveVisible, diffGraphs,
  indexGraph, normalizeToolsMode, toolColumn, toolsHintText, activeEdgeIds, skillBadge,
} from '../lib/graph_model.js';

const EXAMPLES = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'contract', 'examples');
const loadExample = (name) => JSON.parse(readFileSync(join(EXAMPLES, name), 'utf8'));

const GRAPH = loadExample('graph.json');
const CHAIN = loadExample('graph_chain.json');
const K = (id) => `claude:s-main:${id}`;
const A1 = K('a1111111111111111');
const A2 = K('a2222222222222222');
const A3 = K('a3333333333333333');
const MAIN = K('main');

const kinds = (v) => v.nodes.map((n) => n.kind);
const ids = (v) => v.nodes.map((n) => n.id);

test('indexGraph builds children in node order and a tree for flattenTree', () => {
  const index = indexGraph(GRAPH);
  assert.equal(index.tree.rootKey, MAIN);
  assert.deepEqual(index.tree.nodes[MAIN].children, [A1, A3, K('a4444444444444444')]);
  assert.deepEqual(index.tree.nodes[A1].children, [A2]);
  assert.equal(index.agents.size, 5);
  assert.equal(index.toolsByOwner.get(A2)[0].name, 'Bash');
  assert.equal(index.skillsByOwner.get(MAIN)[0].name, 'code-review');
});

test('tools mode none shows agents only, with every spawn and resume edge', () => {
  const v = deriveVisible(GRAPH, { toolsMode: 'none' });
  assert.deepEqual(kinds(v), ['agent', 'agent', 'agent', 'agent', 'agent']);
  assert.deepEqual(v.edges.map((e) => e.kind), ['spawn', 'spawn', 'spawn', 'spawn', 'resume']);
  assert.equal(v.guarded, false);
});

test('tools mode selected shows the tool column of the selected agent only', () => {
  const v = deriveVisible(GRAPH, { toolsMode: 'selected', selectedId: A2 });
  assert.deepEqual(ids(v).slice(5), [`tool|${A2}|Bash`]);
  assert.ok(v.edges.some((e) => e.kind === 'uses_tool' && e.to === `tool|${A2}|Bash`));
  const none = deriveVisible(GRAPH, { toolsMode: 'selected', selectedId: null });
  assert.equal(none.nodes.length, 5);
});

test('tools mode all shows every tool and skill node with their edges', () => {
  const v = deriveVisible(GRAPH, { toolsMode: 'all' });
  assert.deepEqual(ids(v).slice(5).sort(), [`skill|${MAIN}|code-review`, `tool|${A2}|Bash`, `tool|${A3}|Read`].sort());
  assert.equal(v.edges.length, GRAPH.edges.length);
});

test('an invalid or missing tools mode falls back to the default, all', () => {
  assert.equal(normalizeToolsMode('bogus'), 'all');
  assert.equal(deriveVisible(GRAPH, { toolsMode: 'bogus', selectedId: A3 }).toolsMode, 'all');
  assert.equal(deriveVisible(GRAPH).toolsMode, 'all');
  assert.equal(deriveVisible(GRAPH).columns.size, 3, 'every agent with tools or skills gets its column');
});

function manyTools(n, omitted = 0, omittedCalls = 0) {
  const owner = { id: 'o', kind: 'agent', agentId: 'main', isRoot: true, parentId: null, toolsOmitted: omitted, toolsOmittedCalls: omittedCalls };
  const tools = Array.from({ length: n }, (_, i) => ({ id: `tool|o|T${i}`, kind: 'tool', ownerId: 'o', name: `T${i}`, calls: 10 - i, errors: 0 }));
  return { rootKey: 'o', nodes: [owner, ...tools], edges: [] };
}

test('the column keeps the top 5 tools and synthesizes one +N more node', () => {
  const graph = manyTools(8, 2, 7);
  const column = toolColumn(indexGraph(graph), graph.nodes[0]);
  assert.equal(column.length, TOOLS_VISIBLE + 1);
  const more = column[TOOLS_VISIBLE];
  assert.equal(more.kind, MORE);
  assert.equal(more.count, 3 + 2);
  assert.equal(more.calls, (10 - 5) + (10 - 6) + (10 - 7) + 7);
  const v = deriveVisible(graph, { toolsMode: 'all' });
  assert.ok(v.edges.some((e) => e.kind === 'uses_more' && e.to === more.id));
});

test('no +N more node when everything fits', () => {
  const graph = manyTools(5);
  assert.ok(toolColumn(indexGraph(graph), graph.nodes[0]).every((n) => n.kind === 'tool'));
});

test('collapse hides every descendant and counts them', () => {
  const v = deriveVisible(GRAPH, { toolsMode: 'all', collapsed: new Set([A1]) });
  assert.ok(!ids(v).includes(A2));
  assert.ok(!ids(v).includes(`tool|${A2}|Bash`));
  const a1 = v.agents.find((a) => a.node.id === A1);
  assert.equal(a1.collapsed, true);
  assert.equal(a1.hidden, 1);
  const root = deriveVisible(GRAPH, { collapsed: new Set([MAIN]) });
  assert.equal(root.agents.length, 1);
  assert.equal(root.agents[0].hidden, 4);
});

test('tools mode all falls back to selected over MAX_VISIBLE_NODES', () => {
  const nodes = [{ id: 'r', kind: 'agent', agentId: 'main', isRoot: true, parentId: null }];
  for (let i = 0; i < 400; i += 1) {
    const id = `a${i}`;
    nodes.push({ id, kind: 'agent', agentId: id, parentId: 'r' });
    for (let t = 0; t < 4; t += 1) nodes.push({ id: `tool|${id}|${t}`, kind: 'tool', ownerId: id, name: String(t), calls: 1 });
  }
  const v = deriveVisible({ rootKey: 'r', nodes, edges: [] }, { toolsMode: 'all', selectedId: 'a1' });
  assert.equal(v.guarded, true);
  assert.equal(v.toolsMode, 'selected');
  assert.ok(v.nodes.length <= MAX_VISIBLE_NODES);
  assert.equal(v.nodes.filter((n) => n.kind === 'tool').length, 4);
});

test('session nodes of the continuation chain are always visible', () => {
  const v = deriveVisible(CHAIN, { toolsMode: 'none' });
  assert.deepEqual(v.sessions.map((s) => s.relation), ['predecessor', 'successor']);
  assert.deepEqual(v.edges.map((e) => e.kind), ['continued_in', 'continued_in']);
});

test('diffGraphs reports added, removed and changed ids', () => {
  const next = JSON.parse(JSON.stringify(GRAPH));
  next.nodes.find((n) => n.id === A2).status = 'running';
  next.nodes = next.nodes.filter((n) => n.id !== `tool|${A3}|Read`);
  next.edges = next.edges.filter((e) => e.to !== `tool|${A3}|Read`);
  next.nodes.push({ id: K('a5'), kind: 'agent', agentId: 'a5', parentId: MAIN });
  const d = diffGraphs(GRAPH, next);
  assert.deepEqual(d.added, [K('a5')]);
  assert.deepEqual(d.removed, [`tool|${A3}|Read`]);
  assert.deepEqual(d.changed, [A2]);
  assert.equal(d.edgesRemoved.length, 1);
  assert.deepEqual(diffGraphs(null, GRAPH).added.length, GRAPH.nodes.length);
});

test('describeAgent lists relations and usage for each fixture agent', () => {
  assert.equal(describeAgent(GRAPH, MAIN), 'session root; spawned 3 agents; resumes a1111111 1 time; tools: none; skills: code-review 1 use');
  assert.equal(describeAgent(GRAPH, A1), 'spawned by main; spawned 1 agent; resumed 1 time by main; tools: none; skills: none');
  assert.equal(describeAgent(GRAPH, A2), 'spawned by a1111111; tools: Bash 1 call, 1 error; skills: none');
  assert.equal(describeAgent(GRAPH, A3), 'spawned by main; tools: Read 1 call; skills: none');
  assert.equal(describeAgent(GRAPH, K('a4444444444444444')), 'spawned by main (no spawn evidence); tools: none; skills: none');
  assert.equal(describeAgent(GRAPH, 'nope'), '');
  const indexing = { ...GRAPH, indexed: false };
  assert.match(describeAgent(indexing, A2), /^counts partial while indexing; /);
});

test('labels: main for the root, short id otherwise, and the card type text', () => {
  const index = indexGraph(GRAPH);
  assert.equal(agentLabel(index.agents.get(MAIN)), 'main');
  assert.equal(agentLabel(index.agents.get(A2)), 'a2222222');
  assert.equal(agentTypeText(index.agents.get(K('a4444444444444444'))), 'unknown type');
  assert.equal(agentTypeText(index.agents.get(A3)), 'ecc:code-explorer');
});

test('unknown fields and kinds and malformed input are ignored', () => {
  const g = { ...GRAPH, extra: 1, nodes: [...GRAPH.nodes, { id: 'x', kind: 'future' }, null], edges: [...GRAPH.edges, { id: 'e', kind: 'future', from: MAIN, to: 'x' }] };
  const v = deriveVisible(g, { toolsMode: 'none' });
  assert.equal(v.nodes.length, 5);
  assert.deepEqual(deriveVisible(null).nodes, []);
});

test('toolsHintText explains an empty tools column in selected mode', () => {
  const hint = (selectedId, toolsMode = 'selected') => toolsHintText(deriveVisible(GRAPH, { toolsMode, selectedId }), selectedId);
  assert.equal(hint(null), 'Select an agent to see its tools');
  assert.equal(hint(A1), 'Only agent spawns and messages, drawn as edges');
  assert.equal(hint(MAIN), null);
  assert.equal(hint(K('a4444444444444444')), 'No tool calls in this agent yet');
  assert.equal(hint(A2), null);
  assert.equal(hint(MAIN, 'all'), null);
  assert.equal(hint(MAIN, 'none'), null);
});

test('activeEdgeIds marks lines to pending tools of running agents and spawns of running children', () => {
  const g = structuredClone(GRAPH);
  const byId = new Map(g.nodes.map((n) => [n.id, n]));
  byId.get(A2).status = 'running';
  byId.get(`tool|${A2}|Bash`).pending = 1;
  const active = activeEdgeIds(deriveVisible(g, { toolsMode: 'all' }));
  assert.deepEqual([...active].sort(), [`spawn|${A1}|${A2}`, `uses_tool|${A2}|tool|${A2}|Bash`].sort());
});

test('activeEdgeIds ignores pending tools of agents that are no longer running', () => {
  const g = structuredClone(GRAPH);
  g.nodes.find((n) => n.id === `tool|${A2}|Bash`).pending = 1;
  assert.deepEqual([...activeEdgeIds(deriveVisible(g, { toolsMode: 'all' }))], []);
});

test('skillBadge labels preloaded skills and keeps the use count otherwise', () => {
  const skill = (count, via) => ({ kind: 'skill', name: 'llm-wiki', count, via });
  assert.deepEqual(skillBadge(skill(1, { tool: 0, slash: 0, fork: 0, read: 0, preload: 1 })),
    { text: 'preloaded', isPreloaded: true, title: 'skill llm-wiki: preloaded by the agent definition' });
  assert.deepEqual(skillBadge(skill(3, { tool: 2, slash: 0, fork: 0, read: 0, preload: 1 })),
    { text: 'x3', isPreloaded: true, title: 'skill llm-wiki: 3 uses, 1 preloaded' });
  assert.deepEqual(skillBadge(skill(2, { tool: 2, slash: 0, fork: 0, read: 0 })),
    { text: 'x2', isPreloaded: false, title: 'skill llm-wiki: 2 uses' });
});
