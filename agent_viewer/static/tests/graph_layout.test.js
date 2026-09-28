import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deriveVisible } from '../lib/graph_model.js';
import { CARD_H, CARD_W, H_GAP, LEAF_WRAP, TOOL_H, V_GAP, layoutGraph, subtreeBounds } from '../lib/graph_layout.js';

const EXAMPLES = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'contract', 'examples');
const load = (name) => JSON.parse(readFileSync(join(EXAMPLES, name), 'utf8'));
const GRAPH = load('graph.json');
const CHAIN = load('graph_chain.json');
const MAIN = 'claude:s-main:main';

/** Seeded linear congruential generator, so random trees are reproducible. */
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

/** A random tree graph of n agents, some with tool nodes. */
function randomGraph(n, seed, { tools = true } = {}) {
  const rand = rng(seed);
  const nodes = [{ id: 'n0', kind: 'agent', agentId: 'main', isRoot: true, parentId: null }];
  const toolNodes = [];
  for (let i = 1; i < n; i += 1) {
    nodes.push({ id: `n${i}`, kind: 'agent', agentId: `n${i}`, parentId: `n${Math.floor(rand() * i)}` });
  }
  if (tools) {
    for (let i = 0; i < n; i += 1) {
      const count = Math.floor(rand() * 4);
      for (let t = 0; t < count; t += 1) toolNodes.push({ id: `tool|n${i}|T${t}`, kind: 'tool', ownerId: `n${i}`, name: `T${t}`, calls: 1 });
    }
  }
  // Agent nodes in depth-first order as the server emits them (rule 1).
  const children = new Map(nodes.map((x) => [x.id, []]));
  for (const x of nodes.slice(1)) children.get(x.parentId).push(x);
  const ordered = [];
  const visit = (x) => { ordered.push(x); children.get(x.id).forEach(visit); };
  visit(nodes[0]);
  return { rootKey: 'n0', nodes: [...ordered, ...toolNodes], edges: [] };
}

function boxes(layout) {
  return [...layout.positions.entries()].map(([id, p]) => ({ id, ...p }));
}

function overlapping(list) {
  const hits = [];
  for (let i = 0; i < list.length; i += 1) {
    for (let j = i + 1; j < list.length; j += 1) {
      const a = list[i];
      const b = list[j];
      if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) hits.push(`${a.id} x ${b.id}`);
    }
  }
  return hits;
}

const serialize = (layout) => JSON.stringify({ p: [...layout.positions], e: [...layout.edges], b: layout.bounds });

test('the layout is deterministic for the same input', () => {
  const v = deriveVisible(GRAPH, { toolsMode: 'all' });
  assert.equal(serialize(layoutGraph(v)), serialize(layoutGraph(deriveVisible(GRAPH, { toolsMode: 'all' }))));
});

test('no two node boxes overlap for random trees of 1 to 500 nodes', () => {
  for (const [n, seed] of [[1, 1], [2, 2], [7, 3], [30, 4], [120, 5], [500, 6]]) {
    const layout = layoutGraph(deriveVisible(randomGraph(n, seed), { toolsMode: 'all' }));
    assert.equal(layout.positions.size >= n, true);
    assert.deepEqual(overlapping(boxes(layout)), [], `n=${n}`);
  }
});

test('all coordinates are integers', () => {
  const layout = layoutGraph(deriveVisible(randomGraph(200, 9), { toolsMode: 'all' }));
  for (const p of layout.positions.values()) {
    for (const v of [p.x, p.y, p.w, p.h]) assert.ok(Number.isInteger(v), `${JSON.stringify(p)}`);
  }
  for (const e of layout.edges.values()) assert.doesNotMatch(e.d, /\d\.\d/);
});

test('children sit below their parent in children order, parent centered over them', () => {
  const layout = layoutGraph(deriveVisible(GRAPH, { toolsMode: 'none' }));
  const p = (id) => layout.positions.get(`claude:s-main:${id}`);
  const root = p('main');
  const kids = ['a1111111111111111', 'a3333333333333333', 'a4444444444444444'].map(p);
  for (const k of kids) assert.equal(k.y, root.y + CARD_H + V_GAP);
  assert.ok(kids[0].x < kids[1].x && kids[1].x < kids[2].x);
  const spanCenter = (kids[0].x + kids[2].x + CARD_W) / 2;
  assert.ok(Math.abs(root.x + CARD_W / 2 - spanCenter) <= 1);
  assert.equal(p('a2222222222222222').y, kids[0].y + CARD_H + V_GAP);
});

test('more than LEAF_WRAP leaves wrap into a grid of LEAF_WRAP columns', () => {
  const nodes = [{ id: 'r', kind: 'agent', agentId: 'main', isRoot: true, parentId: null }];
  for (let i = 0; i < 14; i += 1) nodes.push({ id: `c${i}`, kind: 'agent', agentId: `c${i}`, parentId: 'r' });
  const layout = layoutGraph(deriveVisible({ rootKey: 'r', nodes, edges: [] }, { toolsMode: 'none' }));
  const ys = [...new Set(nodes.slice(1).map((n) => layout.positions.get(n.id).y))];
  assert.equal(ys.length, 3);
  const firstRow = nodes.slice(1).filter((n) => layout.positions.get(n.id).y === ys[0]);
  assert.equal(firstRow.length, LEAF_WRAP);
  assert.equal(layout.positions.get('c6').x, layout.positions.get('c0').x);
  assert.equal(layout.positions.get('c1').x - layout.positions.get('c0').x, CARD_W + H_GAP);
  // Six or fewer leaves stay on one row.
  const six = layoutGraph(deriveVisible({ rootKey: 'r', nodes: nodes.slice(0, 7), edges: [] }, { toolsMode: 'none' }));
  assert.equal(new Set(nodes.slice(1, 7).map((n) => six.positions.get(n.id).y)).size, 1);
});

test('session cards sit on the root row, predecessors left and successors right', () => {
  const layout = layoutGraph(deriveVisible(CHAIN, { toolsMode: 'all' }));
  const root = layout.positions.get('claude:s-chain-2:main');
  const pred = layout.positions.get('session|claude:s-chain-1');
  const succ = layout.positions.get('session|claude:s-chain-3');
  assert.ok(pred.x + pred.w < root.x);
  assert.ok(succ.x > root.x + CARD_W);
  assert.ok(pred.y >= root.y && pred.y + pred.h <= root.y + CARD_H);
  assert.equal(succ.y, pred.y);
  assert.deepEqual(overlapping(boxes(layout)), []);
  assert.equal(layout.edges.size, 3);
});

test('adding a last child moves no node before it in depth-first order', () => {
  const base = randomGraph(80, 11, { tools: false });
  const agents = base.nodes;
  const parent = agents[17];
  const grown = JSON.parse(JSON.stringify(base));
  // Insert the new last child of `parent` right after its subtree, keeping DFS order.
  const isDesc = (id) => { let cur = grown.nodes.find((n) => n.id === id); while (cur) { if (cur.id === parent.id) return true; cur = grown.nodes.find((n) => n.id === cur.parentId); } return false; };
  let at = grown.nodes.findIndex((n) => n.id === parent.id) + 1;
  while (at < grown.nodes.length && isDesc(grown.nodes[at].id)) at += 1;
  grown.nodes.splice(at, 0, { id: 'new', kind: 'agent', agentId: 'new', parentId: parent.id });
  const before = layoutGraph(deriveVisible(base, { toolsMode: 'none' }));
  const after = layoutGraph(deriveVisible(grown, { toolsMode: 'none' }));
  const ancestors = new Set();
  for (let cur = parent; cur; cur = agents.find((n) => n.id === cur.parentId)) ancestors.add(cur.id);
  const moved = agents.slice(0, at).filter((n) => !ancestors.has(n.id) && !isDesc(n.id))
    .filter((n) => JSON.stringify(before.positions.get(n.id)) !== JSON.stringify(after.positions.get(n.id)));
  assert.deepEqual(moved.map((n) => n.id), []);
  for (const n of agents) assert.equal(after.positions.get(n.id).y, before.positions.get(n.id).y);
});

test('a tool column widens the box and sits right of its card', () => {
  const layout = layoutGraph(deriveVisible(GRAPH, { toolsMode: 'all' }));
  const card = layout.positions.get('claude:s-main:a2222222222222222');
  const tool = layout.positions.get('tool|claude:s-main:a2222222222222222|Bash');
  assert.ok(tool.x >= card.x + CARD_W);
  assert.equal(tool.h, TOOL_H);
  assert.ok(tool.y >= card.y && tool.y + tool.h <= card.y + CARD_H);
});

test('every visible edge gets a path, with a label point for resume edges', () => {
  const layout = layoutGraph(deriveVisible(GRAPH, { toolsMode: 'all' }));
  assert.equal(layout.edges.size, GRAPH.edges.length);
  for (const e of layout.edges.values()) assert.match(e.d, /^M-?\d+ -?\d+/);
  const resume = [...layout.edges.values()].find((e) => e.kind === 'resume');
  assert.ok(Number.isInteger(resume.label.x));
});

test('grid children beyond the first row are routed through the column gutter', () => {
  const nodes = [{ id: 'r', kind: 'agent', agentId: 'main', isRoot: true, parentId: null }];
  const edges = [];
  for (let i = 0; i < 8; i += 1) {
    nodes.push({ id: `c${i}`, kind: 'agent', agentId: `c${i}`, parentId: 'r' });
    edges.push({ id: `spawn|r|c${i}`, kind: 'spawn', from: 'r', to: `c${i}` });
  }
  const layout = layoutGraph(deriveVisible({ rootKey: 'r', nodes, edges }, { toolsMode: 'none' }));
  const d = layout.edges.get('spawn|r|c7').d;
  const c7 = layout.positions.get('c7');
  assert.ok(d.endsWith(`H${c7.x}`));
});

test('subtreeBounds covers a card and its visible descendants', () => {
  const v = deriveVisible(GRAPH, { toolsMode: 'all' });
  const layout = layoutGraph(v);
  const b = subtreeBounds(layout, v, 'claude:s-main:a1111111111111111');
  const a2 = layout.positions.get('claude:s-main:a2222222222222222');
  assert.ok(b.y + b.h >= a2.y + a2.h);
  assert.deepEqual(subtreeBounds(layout, v, MAIN), layout.bounds);
  assert.equal(subtreeBounds(layout, v, 'claude:s-main:a4444444444444444').w, CARD_W);
});

test('an empty graph lays out to empty bounds', () => {
  const layout = layoutGraph(deriveVisible({ nodes: [], edges: [] }));
  assert.equal(layout.positions.size, 0);
  assert.deepEqual(layout.bounds, { x: 0, y: 0, w: 0, h: 0 });
});

test('2,000 visible nodes lay out within the G-PERF-6 bound', () => {
  const v = deriveVisible(randomGraph(2000, 21, { tools: false }), { toolsMode: 'none' });
  const t0 = performance.now();
  const layout = layoutGraph(v);
  const ms = performance.now() - t0;
  assert.equal(layout.positions.size, 2000);
  assert.ok(ms < 200, `layout took ${ms.toFixed(1)} ms`);
});

/** Points along an SVG path made of M, H, V and C commands. */
function samplePath(d) {
  const tokens = d.match(/[MHVC]|-?\d+(?:\.\d+)?/g);
  const points = [];
  let x = 0;
  let y = 0;
  let i = 0;
  const num = () => Number(tokens[i++]);
  const line = (x2, y2) => {
    for (let k = 1; k <= 20; k += 1) points.push({ x: x + ((x2 - x) * k) / 20, y: y + ((y2 - y) * k) / 20 });
    x = x2;
    y = y2;
  };
  while (i < tokens.length) {
    const cmd = tokens[i++];
    if (cmd === 'M') { x = num(); y = num(); points.push({ x, y }); }
    else if (cmd === 'H') line(num(), y);
    else if (cmd === 'V') line(x, num());
    else if (cmd === 'C') {
      const [c1x, c1y, c2x, c2y, x2, y2] = [num(), num(), num(), num(), num(), num()];
      for (let k = 1; k <= 40; k += 1) {
        const t = k / 40;
        const u = 1 - t;
        points.push({
          x: u ** 3 * x + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t ** 3 * x2,
          y: u ** 3 * y + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t ** 3 * y2,
        });
      }
      x = x2;
      y = y2;
    }
  }
  return points;
}

/** A copy of the graph where main also used `count` tools, like a real busy session. */
function withMainTools(graph, count) {
  const g = structuredClone(graph);
  for (let n = 0; n < count; n += 1) {
    const id = `tool|${MAIN}|T${n}`;
    g.nodes.push({ id, kind: 'tool', ownerId: MAIN, name: `T${n}`, calls: 10 - n, errors: 0, pending: 0, totalDurationMs: 1 });
    g.edges.push({ id: `uses_tool|${MAIN}|${id}`, kind: 'uses_tool', from: MAIN, to: id, count: 10 - n, errors: 0 });
  }
  return g;
}

function relationEdgesCrossingTools(graph) {
  const visible = deriveVisible(graph, { toolsMode: 'all' });
  const layout = layoutGraph(visible);
  const toolRects = visible.nodes.filter((n) => n.kind !== 'agent' && n.kind !== 'session').map((n) => layout.positions.get(n.id));
  const inside = (p, r) => p.x > r.x + 1 && p.x < r.x + r.w - 1 && p.y > r.y + 1 && p.y < r.y + r.h - 1;
  const crossing = [];
  for (const edge of layout.edges.values()) {
    if (edge.kind !== 'resume' && edge.kind !== 'continued_in' && edge.kind !== 'spawn') continue;
    if (samplePath(edge.d).some((p) => toolRects.some((r) => inside(p, r)))) crossing.push(edge.kind);
  }
  return crossing;
}

test('spawn, resume and continued-in edges never pass through a tool node', () => {
  const resumeRight = withMainTools(GRAPH, 5);
  const last = resumeRight.nodes.filter((n) => n.kind === 'agent' && n.parentId === MAIN).pop();
  for (const edge of resumeRight.edges) if (edge.kind === 'resume') edge.to = last.id;
  for (const graph of [GRAPH, CHAIN, withMainTools(GRAPH, 5), withMainTools(CHAIN, 5), resumeRight]) {
    assert.deepEqual(relationEdgesCrossingTools(graph), []);
  }
});

test('a continued-in edge starts on the agent card, not at its tools column', () => {
  const visible = deriveVisible(CHAIN, { toolsMode: 'all' });
  const layout = layoutGraph(visible);
  const card = layout.positions.get(visible.rootId);
  const edge = [...layout.edges.values()].find((e) => e.kind === 'continued_in' && samplePath(e.d)[0].x >= card.x);
  const start = samplePath(edge.d)[0];
  assert.ok(start.x >= card.x && start.x <= card.x + CARD_W, `start x ${start.x} is outside the card`);
  assert.ok(start.y >= card.y && start.y <= card.y + CARD_H, `start y ${start.y} is outside the card`);
});
