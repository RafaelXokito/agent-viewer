// Pure graph model helpers (FEATURE-graph-canvas sections 2.4, 6.4, 8.2):
// derive the visible graph from a Graph JSON snapshot, synthesize "+N more"
// nodes, apply collapse, diff two snapshots and write accessible descriptions.
// No DOM access, so everything here runs under `node --test`.

import { formatCompact, formatInt } from './format.js';
import { flattenTree } from './tree.js';

export const TOOLS_VISIBLE = 5;
export const MAX_VISIBLE_NODES = 1500;
export const TOOLS_MODES = Object.freeze(['none', 'selected', 'all']);

const AGENT = 'agent';
const SESSION = 'session';
const TOOL = 'tool';
const SKILL = 'skill';
export const MORE = 'more';

export function normalizeToolsMode(mode) {
  return TOOLS_MODES.includes(mode) ? mode : 'all';
}

function nodesOf(graph) {
  return graph && Array.isArray(graph.nodes) ? graph.nodes : [];
}

function edgesOf(graph) {
  return graph && Array.isArray(graph.edges) ? graph.edges : [];
}

/** Display label of an agent node: "main" for the root, else the short agentId. */
export function agentLabel(node) {
  if (!node) return 'unknown';
  if (node.isRoot || node.agentId === 'main') return 'main';
  return String(node.agentId || node.id || '').slice(0, 8) || 'unknown';
}

/** Card type text: "main", the agentType, or "unknown type". */
export function agentTypeText(node) {
  if (node.isRoot || node.agentId === 'main') return 'main';
  return node.agentType || 'unknown type';
}

/**
 * Index a snapshot: agents by id, children in node order, tool and skill
 * nodes per owner, session nodes, and a `tree` shaped like SPEC 9.4 so the
 * tree helpers (flattenTree, moveSelection) work on it unchanged.
 */
export function indexGraph(graph) {
  const agents = new Map();
  const toolsByOwner = new Map();
  const skillsByOwner = new Map();
  const sessions = [];
  const byId = new Map();
  for (const node of nodesOf(graph)) {
    if (!node || typeof node.id !== 'string') continue;
    byId.set(node.id, node);
    if (node.kind === AGENT) agents.set(node.id, node);
    else if (node.kind === SESSION) sessions.push(node);
    else if (node.kind === TOOL) pushTo(toolsByOwner, node.ownerId, node);
    else if (node.kind === SKILL) pushTo(skillsByOwner, node.ownerId, node);
  }
  const treeNodes = {};
  for (const node of agents.values()) {
    treeNodes[node.id] = { key: node.id, agentId: node.agentId, parentKey: node.parentId || null, children: [] };
  }
  for (const node of agents.values()) {
    const parent = node.parentId ? treeNodes[node.parentId] : null;
    if (parent && node.parentId !== node.id) parent.children.push(node.id);
  }
  const rootKey = graph && agents.has(graph.rootKey) ? graph.rootKey : firstRoot(agents);
  return { byId, agents, toolsByOwner, skillsByOwner, sessions, tree: { rootKey, nodes: treeNodes } };
}

function pushTo(map, key, value) {
  if (!map.has(key)) map.set(key, []);
  map.get(key).push(value);
}

function firstRoot(agents) {
  for (const node of agents.values()) if (node.isRoot) return node.id;
  const first = agents.keys().next();
  return first.done ? null : first.value;
}

/** Number of descendants below `key` in the index tree. */
export function descendantCount(tree, key) {
  let count = 0;
  const seen = new Set([key]);
  const stack = [...((tree.nodes[key] && tree.nodes[key].children) || [])];
  while (stack.length > 0) {
    const next = stack.pop();
    if (seen.has(next) || !tree.nodes[next]) continue;
    seen.add(next);
    count += 1;
    stack.push(...tree.nodes[next].children);
  }
  return count;
}

/** The tool, skill and "+N more" column of one agent, at most TOOLS_VISIBLE of each kind. */
export function toolColumn(index, agent) {
  const tools = index.toolsByOwner.get(agent.id) || [];
  const skills = index.skillsByOwner.get(agent.id) || [];
  const shownTools = tools.slice(0, TOOLS_VISIBLE);
  const shownSkills = skills.slice(0, TOOLS_VISIBLE);
  const hiddenTools = tools.slice(TOOLS_VISIBLE);
  const hiddenSkills = skills.slice(TOOLS_VISIBLE);
  const omitted = toInt(agent.toolsOmitted);
  const count = hiddenTools.length + hiddenSkills.length + omitted;
  const column = [...shownTools];
  if (count > 0) {
    const calls = sum(hiddenTools, 'calls') + sum(hiddenSkills, 'count') + toInt(agent.toolsOmittedCalls);
    column.push({ id: `more|${agent.id}`, kind: MORE, ownerId: agent.id, count, calls });
  }
  column.push(...shownSkills);
  return column;
}

function toInt(n) {
  return Number.isFinite(n) ? n : 0;
}

function sum(list, field) {
  return list.reduce((acc, item) => acc + toInt(item[field]), 0);
}

function columnOwners(rows, toolsMode, selectedId) {
  if (toolsMode === 'none') return [];
  if (toolsMode === 'all') return rows.map((r) => r.key);
  return rows.some((r) => r.key === selectedId) ? [selectedId] : [];
}

function buildColumns(index, owners) {
  const columns = new Map();
  for (const owner of owners) {
    const column = toolColumn(index, index.agents.get(owner));
    if (column.length > 0) columns.set(owner, column);
  }
  return columns;
}

function columnSize(columns) {
  let n = 0;
  for (const column of columns.values()) n += column.length;
  return n;
}

/**
 * The visible graph for a tools mode, selection and collapse set:
 * `{rootId, agents: [{node, level, hasChildren, collapsed, hidden}], sessions,
 * columns: Map<ownerId, node[]>, nodes, edges, toolsMode, guarded}`.
 * `guarded` is true when `all` would exceed MAX_VISIBLE_NODES and the view
 * fell back to the selected agent (section 2.4).
 */
export function deriveVisible(graph, { toolsMode = 'all', selectedId = null, collapsed = new Set() } = {}) {
  const index = indexGraph(graph);
  const rows = flattenTree(index.tree, collapsed);
  let mode = normalizeToolsMode(toolsMode);
  let columns = buildColumns(index, columnOwners(rows, mode, selectedId));
  let guarded = false;
  const fixed = rows.length + index.sessions.length;
  if (mode === 'all' && fixed + columnSize(columns) > MAX_VISIBLE_NODES) {
    guarded = true;
    mode = 'selected';
    columns = buildColumns(index, columnOwners(rows, mode, selectedId));
  }
  const agents = rows.map((r) => ({
    node: index.agents.get(r.key),
    level: r.level,
    hasChildren: r.hasChildren,
    collapsed: r.collapsed,
    hidden: r.collapsed ? descendantCount(index.tree, r.key) : 0,
  }));
  const nodes = [...agents.map((a) => a.node), ...index.sessions];
  for (const column of columns.values()) nodes.push(...column);
  const ids = new Set(nodes.map((n) => n.id));
  const edges = edgesOf(graph).filter((e) => e && ids.has(e.from) && ids.has(e.to));
  for (const [owner, column] of columns) {
    for (const item of column) {
      if (item.kind === MORE) edges.push({ id: `uses_more|${owner}|${item.id}`, kind: 'uses_more', from: owner, to: item.id });
    }
  }
  return { rootId: index.tree.rootKey, index, agents, sessions: index.sessions, columns, nodes, edges, toolsMode: mode, guarded };
}

/**
 * Toolbar hint for `selected` mode when no tools column is shown, or null.
 * Spawn and message calls are drawn as edges, so an agent that made only those
 * has calls but no column.
 */
export function toolsHintText(visible, selectedId) {
  if (!visible || visible.toolsMode !== 'selected' || visible.columns.size > 0) return null;
  const agent = selectedId ? visible.index.agents.get(selectedId) : null;
  if (!agent) return 'Select an agent to see its tools';
  if (!agent.toolCalls) return 'No tool calls in this agent yet';
  return 'Only agent spawns and messages, drawn as edges';
}

/**
 * Ids of visible edges that show work in progress: the line to a tool with
 * pending calls whose agent is running, and the spawn line to a running child.
 */
export function activeEdgeIds(visible) {
  const nodes = new Map(visible.nodes.map((n) => [n.id, n]));
  const isRunning = (id) => (nodes.get(id) || {}).status === 'running';
  const active = new Set();
  for (const edge of visible.edges) {
    const target = nodes.get(edge.to);
    const isPendingTool = edge.kind === 'uses_tool' && target && target.pending > 0 && isRunning(edge.from);
    if (isPendingTool || (edge.kind === 'spawn' && isRunning(edge.to))) active.add(edge.id);
  }
  return active;
}

/**
 * Label, tooltip and marker for a skill pill: "preloaded" when every use came
 * from the agent definition's `skills:`, else the use count.
 */
export function skillBadge(node) {
  const count = node.count || 0;
  const preloaded = (node.via && node.via.preload) || 0;
  if (preloaded > 0 && preloaded === count) {
    return { text: 'preloaded', isPreloaded: true, title: `skill ${node.name}: preloaded by the agent definition` };
  }
  const uses = `skill ${node.name}: ${formatInt(count)} uses`;
  return {
    text: `x${formatCompact(count)}`,
    isPreloaded: preloaded > 0,
    title: preloaded > 0 ? `${uses}, ${formatInt(preloaded)} preloaded` : uses,
  };
}

/** Stable per-node signature used to detect changed nodes. */
export function nodeSignature(node) {
  return JSON.stringify(node);
}

/** Added, removed and changed node ids, and added and removed edge ids, between two snapshots. */
export function diffGraphs(prev, next) {
  const before = new Map(nodesOf(prev).map((n) => [n.id, nodeSignature(n)]));
  const after = new Map(nodesOf(next).map((n) => [n.id, nodeSignature(n)]));
  const added = [];
  const changed = [];
  for (const [id, sig] of after) {
    if (!before.has(id)) added.push(id);
    else if (before.get(id) !== sig) changed.push(id);
  }
  const removed = [...before.keys()].filter((id) => !after.has(id));
  const edgesBefore = new Set(edgesOf(prev).map((e) => e.id));
  const edgesAfter = new Set(edgesOf(next).map((e) => e.id));
  return {
    added,
    removed,
    changed,
    edgesAdded: [...edgesAfter].filter((id) => !edgesBefore.has(id)),
    edgesRemoved: [...edgesBefore].filter((id) => !edgesAfter.has(id)),
  };
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

function toolText(tool) {
  const errors = toInt(tool.errors);
  return `${tool.name} ${plural(toInt(tool.calls), 'call')}${errors > 0 ? `, ${plural(errors, 'error')}` : ''}`;
}

function resumeTexts(graph, index, id) {
  const out = [];
  for (const edge of edgesOf(graph)) {
    if (edge.kind !== 'resume') continue;
    const count = toInt(edge.count) || 1;
    if (edge.to === id) out.push(`resumed ${plural(count, 'time')} by ${agentLabel(index.agents.get(edge.from))}`);
    if (edge.from === id) out.push(`resumes ${agentLabel(index.agents.get(edge.to))} ${plural(count, 'time')}`);
  }
  return out;
}

/**
 * Accessible description of an agent card (G-FR-40), for example
 * "spawned by main; resumed 1 time by main; tools: Bash 1 call, 1 error; skills: none".
 */
export function describeAgent(graph, id, index = indexGraph(graph)) {
  const node = index.agents.get(id);
  if (!node) return '';
  const parts = [];
  if (graph && graph.indexed === false) parts.push('counts partial while indexing');
  const parent = node.parentId ? index.agents.get(node.parentId) : null;
  if (node.isRoot) parts.push('session root');
  else parts.push(`spawned by ${parent ? agentLabel(parent) : 'unknown'}${node.linkedBy === 'orphan' ? ' (no spawn evidence)' : ''}`);
  const children = index.tree.nodes[id] ? index.tree.nodes[id].children.length : 0;
  if (children > 0) parts.push(`spawned ${plural(children, 'agent')}`);
  parts.push(...resumeTexts(graph, index, id));
  const tools = (index.toolsByOwner.get(id) || []).map(toolText);
  const omitted = toInt(node.toolsOmitted);
  if (omitted > 0) tools.push(`${plural(omitted, 'more tool')}`);
  parts.push(`tools: ${tools.length > 0 ? tools.join(', ') : 'none'}`);
  const skills = (index.skillsByOwner.get(id) || []).map((s) => `${s.name} ${plural(toInt(s.count), 'use')}`);
  parts.push(`skills: ${skills.length > 0 ? skills.join(', ') : 'none'}`);
  return parts.join('; ');
}
