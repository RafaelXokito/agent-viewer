// Deterministic layered tree layout (FEATURE-graph-canvas section 10.5).
// Input: the visible graph of graph_model.deriveVisible. Output: integer
// positions per node id, the bounds, and one SVG path per edge. Pure.

export const CARD_W = 240;
export const CARD_H = 88;
export const SESSION_W = 240;
export const SESSION_H = 64;
export const TOOL_W = 160;
export const TOOL_H = 26;
export const TOOL_GAP = 6;
export const H_GAP = 32;
export const V_GAP = 56;
export const LEAF_WRAP = 6;
const SESSION_GAP = 2 * H_GAP;
const EDGE_STUB = 8;

/**
 * Children per visible agent, derived from the depth-first rows and their
 * levels (graph_model.deriveVisible): a row's parent is the nearest earlier
 * row one level up. This is acyclic by construction and matches flattenTree.
 */
function childMap(agents) {
  const children = new Map();
  const stack = [];
  for (const { node, level } of agents) {
    children.set(node.id, []);
    while (stack.length > 0 && stack[stack.length - 1].level >= level) stack.pop();
    if (stack.length > 0) children.get(stack[stack.length - 1].id).push(node.id);
    stack.push({ id: node.id, level });
  }
  return children;
}

function columnHeight(count) {
  return count > 0 ? count * TOOL_H + (count - 1) * TOOL_GAP : 0;
}

function boxOf(columns, id) {
  const column = columns.get(id) || [];
  if (column.length === 0) return { w: CARD_W, h: CARD_H };
  return { w: CARD_W + H_GAP / 2 + TOOL_W, h: Math.max(CARD_H, columnHeight(column.length)) };
}

/** Layout context: children, boxes and memoized subtree widths. */
function makeContext(visible) {
  const columns = visible.columns || new Map();
  const children = childMap(visible.agents || []);
  const boxes = new Map();
  for (const id of children.keys()) boxes.set(id, boxOf(columns, id));
  return { columns, children, boxes, widths: new Map(), positions: new Map(), grids: new Map() };
}

function isLeaf(ctx, id) {
  return ctx.children.get(id).length === 0;
}

function usesGrid(ctx, id) {
  const kids = ctx.children.get(id);
  return kids.length > LEAF_WRAP && kids.every((k) => isLeaf(ctx, k));
}

function gridMetrics(ctx, kids) {
  const colW = Math.max(...kids.map((k) => ctx.boxes.get(k).w));
  const rowH = Math.max(...kids.map((k) => ctx.boxes.get(k).h));
  const cols = Math.min(LEAF_WRAP, kids.length);
  return { colW, rowH, cols, width: cols * colW + (cols - 1) * H_GAP };
}

function subtreeWidth(ctx, id) {
  if (ctx.widths.has(id)) return ctx.widths.get(id);
  const kids = ctx.children.get(id);
  const own = ctx.boxes.get(id).w;
  let span = 0;
  if (kids.length > 0 && usesGrid(ctx, id)) {
    span = gridMetrics(ctx, kids).width;
  } else if (kids.length > 0) {
    span = kids.reduce((acc, k) => acc + subtreeWidth(ctx, k), 0) + (kids.length - 1) * H_GAP;
  }
  const width = Math.max(own, span);
  ctx.widths.set(id, width);
  return width;
}

function childrenSpan(ctx, id) {
  const kids = ctx.children.get(id);
  if (kids.length === 0) return 0;
  if (usesGrid(ctx, id)) return gridMetrics(ctx, kids).width;
  return kids.reduce((acc, k) => acc + subtreeWidth(ctx, k), 0) + (kids.length - 1) * H_GAP;
}

/** Place the subtree of `id` whose span starts at x0, with its card top at y. */
function place(ctx, id, x0, y) {
  const box = ctx.boxes.get(id);
  const width = subtreeWidth(ctx, id);
  const span = childrenSpan(ctx, id);
  const center = x0 + Math.floor((span > 0 ? span : box.w) / 2);
  const x = Math.min(Math.max(center - CARD_W / 2, x0), x0 + width - box.w);
  ctx.positions.set(id, { x, y, w: CARD_W, h: CARD_H });
  const kids = ctx.children.get(id);
  if (kids.length === 0) return;
  const childY = y + box.h + V_GAP;
  if (usesGrid(ctx, id)) {
    placeGrid(ctx, kids, x0, childY);
    return;
  }
  let cursor = x0;
  for (const kid of kids) {
    place(ctx, kid, cursor, childY);
    cursor += subtreeWidth(ctx, kid) + H_GAP;
  }
}

function placeGrid(ctx, kids, x0, y0) {
  const m = gridMetrics(ctx, kids);
  kids.forEach((kid, i) => {
    const col = i % m.cols;
    const row = Math.floor(i / m.cols);
    const cellX = x0 + col * (m.colW + H_GAP);
    const cellY = y0 + row * (m.rowH + V_GAP);
    ctx.positions.set(kid, { x: cellX, y: cellY, w: CARD_W, h: CARD_H });
    ctx.grids.set(kid, { row, cellX });
  });
}

function placeColumns(ctx) {
  for (const [owner, column] of ctx.columns) {
    const card = ctx.positions.get(owner);
    if (!card) continue;
    const x = card.x + CARD_W + H_GAP / 2;
    const top = card.y + Math.max(0, Math.floor((CARD_H - columnHeight(column.length)) / 2));
    column.forEach((item, i) => {
      ctx.positions.set(item.id, { x, y: top + i * (TOOL_H + TOOL_GAP), w: TOOL_W, h: TOOL_H });
    });
  }
}

function placeSessions(ctx, sessions, rootId) {
  const root = ctx.positions.get(rootId);
  if (!root) return;
  const rootBox = ctx.boxes.get(rootId);
  const y = root.y + Math.floor((CARD_H - SESSION_H) / 2);
  const byHops = (a, b) => (a.hops || 0) - (b.hops || 0);
  const preds = sessions.filter((s) => s.relation === 'predecessor').sort(byHops);
  const succs = sessions.filter((s) => s.relation !== 'predecessor').sort(byHops);
  preds.forEach((s, i) => {
    ctx.positions.set(s.id, { x: root.x - (i + 1) * (SESSION_W + SESSION_GAP), y, w: SESSION_W, h: SESSION_H });
  });
  succs.forEach((s, i) => {
    ctx.positions.set(s.id, { x: root.x + rootBox.w + SESSION_GAP + i * (SESSION_W + SESSION_GAP), y, w: SESSION_W, h: SESSION_H });
  });
}

export function boundsOf(positions) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of positions.values()) {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x + p.w);
    maxY = Math.max(maxY, p.y + p.h);
  }
  if (minX === Infinity) return { x: 0, y: 0, w: 0, h: 0 };
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

// ---- edges ------------------------------------------------------------------

function spawnPath(ctx, parentId, childId) {
  const p = ctx.positions.get(parentId);
  const c = ctx.positions.get(childId);
  const x1 = p.x + CARD_W / 2;
  const y1 = p.y + CARD_H;
  const grid = ctx.grids.get(childId);
  // Below the parent's whole box, so the bus never runs through a tools column taller than the card.
  const bend = boxBottom(ctx, parentId) + Math.floor(V_GAP / 2);
  if (grid && grid.row > 0) {
    const gutter = grid.cellX - H_GAP / 2;
    const cy = c.y + CARD_H / 2;
    return `M${x1} ${y1}V${bend}H${gutter}V${cy}H${c.x}`;
  }
  return `M${x1} ${y1}V${bend}H${c.x + CARD_W / 2}V${c.y}`;
}

/** Bottom of an agent card plus its tools column. */
function boxBottom(ctx, id) {
  const p = ctx.positions.get(id);
  return p.y + (ctx.boxes.has(id) ? ctx.boxes.get(id).h : p.h);
}

function hasToolsColumn(ctx, id) {
  return (ctx.columns.get(id) || []).length > 0;
}

function bezierMid(x1, y1, c1x, c1y, c2x, c2y, x2, y2) {
  return {
    x: Math.round(0.125 * x1 + 0.375 * c1x + 0.375 * c2x + 0.125 * x2),
    y: Math.round(0.125 * y1 + 0.375 * c1y + 0.375 * c2y + 0.125 * y2),
  };
}

function curve(x1, y1, c1x, c1y, c2x, c2y, x2, y2) {
  return { d: `M${x1} ${y1}C${c1x} ${c1y} ${c2x} ${c2y} ${x2} ${y2}`, label: bezierMid(x1, y1, c1x, c1y, c2x, c2y, x2, y2) };
}

/**
 * Resume edges: to a card below, drop from the card's bottom past its tools
 * column and curve to the target's top. Otherwise an S curve between facing
 * sides when the cards are side by side (so it does not cross the cards
 * between them), else a cubic curve bowed to the right of both cards.
 */
function resumePath(ctx, fromId, toId) {
  const a = ctx.positions.get(fromId);
  const b = ctx.positions.get(toId);
  const clearY = boxBottom(ctx, fromId) + Math.floor(V_GAP / 4);
  if (fromId !== toId && b.y > clearY) {
    const x1 = a.x + Math.floor((3 * a.w) / 4);
    const x2 = b.x + Math.floor((3 * b.w) / 4);
    const mid = Math.floor((clearY + b.y) / 2);
    return {
      d: `M${x1} ${a.y + a.h}V${clearY}C${x1} ${mid} ${x2} ${mid} ${x2} ${b.y}`,
      label: bezierMid(x1, clearY, x1, mid, x2, mid, x2, b.y),
    };
  }
  const ay = a.y + Math.floor(a.h / 3);
  const by = b.y + Math.floor((2 * b.h) / 3);
  if (fromId !== toId && b.x + b.w <= a.x) {
    const mid = Math.floor((a.x + b.x + b.w) / 2);
    return curve(a.x, ay, mid, ay, mid, by, b.x + b.w, by);
  }
  if (fromId !== toId && a.x + a.w <= b.x) {
    const mid = Math.floor((a.x + a.w + b.x) / 2);
    return curve(a.x + a.w, ay, mid, ay, mid, by, b.x, by);
  }
  const x1 = a.x + a.w;
  const x2 = b.x + b.w;
  const bow = Math.min(220, 60 + Math.floor(Math.abs(by - ay) / 4) + Math.floor(Math.abs(x2 - x1) / 4));
  const cx = Math.max(x1, x2) + bow;
  return curve(x1, ay, cx, ay, cx, by, x2, by);
}

function horizontalPath(ctx, fromId, toId) {
  const a = ctx.positions.get(fromId);
  const b = ctx.positions.get(toId);
  const aRight = ctx.boxes.has(fromId) ? a.x + ctx.boxes.get(fromId).w : a.x + a.w;
  const ay = a.y + Math.floor(a.h / 2);
  const by = b.y + Math.floor(b.h / 2);
  if (aRight <= b.x && hasToolsColumn(ctx, fromId)) {
    // Leave from the card's top and pass above its tools column, so the line does not seem to start at a tool.
    const x1 = a.x + CARD_W - 2 * EDGE_STUB;
    const over = a.y - Math.floor(V_GAP / 2);
    return `M${x1} ${a.y}V${over}H${Math.floor((aRight + b.x) / 2)}V${by}H${b.x}`;
  }
  if (aRight <= b.x) return `M${aRight} ${ay}H${Math.floor((aRight + b.x) / 2)}V${by}H${b.x}`;
  const bRight = ctx.boxes.has(toId) ? b.x + ctx.boxes.get(toId).w : b.x + b.w;
  return `M${a.x} ${ay}H${Math.floor((a.x + bRight) / 2)}V${by}H${bRight}`;
}

function toolPath(ctx, ownerId, toolId) {
  const card = ctx.positions.get(ownerId);
  const tool = ctx.positions.get(toolId);
  const x1 = card.x + CARD_W;
  const ty = tool.y + Math.floor(TOOL_H / 2);
  const cy = Math.min(Math.max(ty, card.y + EDGE_STUB), card.y + CARD_H - EDGE_STUB);
  const elbow = x1 + Math.floor(H_GAP / 4);
  return `M${x1} ${cy}H${elbow}V${ty}H${tool.x}`;
}

function edgeGeometry(ctx, edge) {
  if (!ctx.positions.has(edge.from) || !ctx.positions.has(edge.to)) return null;
  switch (edge.kind) {
    case 'spawn':
      return { d: spawnPath(ctx, edge.from, edge.to) };
    case 'resume':
      return resumePath(ctx, edge.from, edge.to);
    case 'continued_in':
      return { d: horizontalPath(ctx, edge.from, edge.to) };
    case 'uses_tool':
    case 'uses_skill':
    case 'uses_more':
      return { d: toolPath(ctx, edge.from, edge.to) };
    default:
      return null;
  }
}

/**
 * Lay out a visible graph. Returns `{positions: Map<id, {x, y, w, h}>,
 * bounds: {x, y, w, h}, edges: Map<id, {d, kind, label?}>}`.
 */
export function layoutGraph(visible) {
  const ctx = makeContext(visible || {});
  const rootId = visible && visible.agents && visible.agents[0] ? visible.agents[0].node.id : null;
  if (rootId) place(ctx, rootId, 0, 0);
  placeColumns(ctx);
  placeSessions(ctx, visible.sessions || [], rootId);
  const edges = new Map();
  for (const edge of (visible && visible.edges) || []) {
    const geometry = edgeGeometry(ctx, edge);
    if (geometry) edges.set(edge.id, { ...geometry, kind: edge.kind });
  }
  return { positions: ctx.positions, bounds: boundsOf(ctx.positions), edges };
}

/** Bounds of an agent card plus every visible descendant and its tool column. */
export function subtreeBounds(layout, visible, id) {
  const ctx = makeContext(visible);
  const ids = [];
  const stack = [id];
  while (stack.length > 0) {
    const next = stack.pop();
    if (!ctx.children.has(next) || ids.includes(next)) continue;
    ids.push(next);
    stack.push(...ctx.children.get(next));
  }
  const picked = new Map();
  for (const agentId of ids) {
    if (layout.positions.has(agentId)) picked.set(agentId, layout.positions.get(agentId));
    for (const item of ctx.columns.get(agentId) || []) {
      if (layout.positions.has(item.id)) picked.set(item.id, layout.positions.get(item.id));
    }
  }
  return boundsOf(picked);
}
