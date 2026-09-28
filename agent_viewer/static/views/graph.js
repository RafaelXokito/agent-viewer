// Graph canvas view (FEATURE-graph-canvas sections 6.3 to 6.8, 10): one
// transformed "world" with HTML cards over an SVG edge layer, a toolbar,
// legend, minimap, live region, culling and live-update animation.
// Interface: section 12.4.2. The session view owns the URL; this view only
// reports intent through the callbacks.

import { h, replace, svg } from '../dom.js';
import { MORE, activeEdgeIds, deriveVisible, describeAgent, diffGraphs, normalizeToolsMode, toolsHintText } from '../lib/graph_model.js';
import { layoutGraph, subtreeBounds } from '../lib/graph_layout.js';
import { avoidMinimap, minimapDefaultVisible } from '../lib/minimap.js';
import { moveSelection } from '../lib/tree.js';
import {
  PAN_STEP, ZOOM_STEP, centerOn, clampZoom, ensureVisible, fitBounds, initialViewport, intersects, lodBand, panBy,
  toolsVisibleAt, visibleWorldRect, wheelZoomFactor, zoomAt, zoomBy,
} from '../lib/viewport.js';
import { agentCard, edgeDefs, edgeElement, legendContent, morePill, placeAt, sessionCard, skillPill, toolPill } from './graph_cards.js';
import { createMinimap } from './minimap.js';
import { errorBox } from './widgets.js';

const CULL_THRESHOLD = 300;
const CULL_INTERVAL_MS = 100;
const DRAG_THRESHOLD = 4;
const NEW_HIGHLIGHT_MS = 2000;
const LEAVE_MS = 150;
const FLASH_MS = 600;
const ANNOUNCE_INTERVAL_MS = 5000;
const LINE_HEIGHT_PX = 16;
const PAN_KEYS = Object.freeze({ ArrowLeft: [1, 0], ArrowRight: [-1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] });
const CARD_KEYS = new Set(['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' ']);

let instanceSeq = 0;

function reducedMotion() {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function isControl(el) {
  return Boolean(el && el.closest && el.closest('input, select, textarea, .graph-toolbar, .graph-legend, .graph-banners, .graph-message'));
}

function shiftLayout(layout, offset) {
  const positions = new Map();
  for (const [id, p] of layout.positions) positions.set(id, { x: p.x + offset.x, y: p.y + offset.y, w: p.w, h: p.h });
  const b = layout.bounds;
  return { positions, bounds: { x: b.x + offset.x, y: b.y + offset.y, w: b.w, h: b.h }, edges: layout.edges };
}

function toolbarButton(text, onClick, label) {
  return h('button', { type: 'button', class: 'btn btn-small', 'aria-label': label || null, text, onClick });
}

export function createGraphView(container, {
  toolsMode = 'all', onSelect, onNavigateSession, onToolsModeChange, onRequestTreeView, onRetry,
} = {}) {
  const suffix = (instanceSeq += 1);
  const s = {
    graph: null,
    toolsMode: normalizeToolsMode(toolsMode),
    selectedId: null,
    sessionTitle: null,
    collapsed: new Set(),
    vp: { x: 0, y: 0, k: 1 },
    view: { w: 0, h: 0 },
    offset: { x: 0, y: 0 },
    visible: null,
    layout: null,
    initialDone: false,
    newAgents: [],
    destroyed: false,
    animating: false,
  };
  const nodeEls = new Map(); // id -> {el, sig, layer, attached}
  const edgeEls = new Map(); // id -> {el, sig, from, to, attached}
  const timers = new Set();
  const pointers = new Map();
  let drag = null;
  let pinch = null;
  let suppressClick = false;
  let frameQueued = false;
  let cullQueued = false;
  let lastCull = 0;
  let lastAnnounce = 0;
  let pendingAnnounce = { agents: 0, status: null };
  let announceQueued = false;

  // ---- DOM -----------------------------------------------------------------

  const edgeLayer = svg('g', { class: 'graph-edge-layer' });
  const edgeSvg = svg('svg', { class: 'graph-edges', 'aria-hidden': 'true', width: 1, height: 1 }, edgeDefs(suffix), edgeLayer);
  const nodeLayer = h('div', { class: 'graph-nodes' });
  const cardLayer = h('div', { class: 'graph-cards', role: 'tree', 'aria-label': 'Agents' });
  const world = h('div', { class: 'graph-world' }, edgeSvg, nodeLayer, cardLayer);
  const zoomLabel = h('span', { class: 'graph-zoom', text: '100%' });
  const toolsSelect = h('select', { 'aria-label': 'Tool and skill nodes', onChange: (e) => changeToolsMode(e.target.value) },
    h('option', { value: 'none', text: 'tools: none' }),
    h('option', { value: 'selected', text: 'tools: selected' }),
    h('option', { value: 'all', text: 'tools: all' }));
  const legendBtn = h('button', { type: 'button', class: 'btn btn-small', 'aria-pressed': 'false', text: 'Legend', onClick: () => toggleLegend() });
  const minimapBtn = h('button', { type: 'button', class: 'btn btn-small', 'aria-pressed': 'false', text: 'Minimap', onClick: () => setMinimapVisible(!minimap.isVisible()) });
  const indexingChip = h('span', { class: 'chip', hidden: true, text: 'indexing' });
  const toolsHint = h('span', { class: 'graph-hint muted', hidden: true, text: 'Select an agent to see its tools' });
  const toolbar = h('div', { class: 'graph-toolbar', role: 'toolbar', 'aria-label': 'Canvas controls' },
    toolbarButton('-', () => zoomCenter(1 / ZOOM_STEP), 'Zoom out'),
    zoomLabel,
    toolbarButton('+', () => zoomCenter(ZOOM_STEP), 'Zoom in'),
    toolbarButton('100%', () => zoomTo(1), 'Zoom to 100%'),
    toolbarButton('Fit', () => fit(), 'Fit the graph'),
    toolsSelect, legendBtn, minimapBtn, indexingChip, toolsHint);
  const banners = h('div', { class: 'graph-banners' });
  const legend = h('div', { class: 'graph-legend', hidden: true, role: 'region', 'aria-label': 'Legend' }, legendContent(suffix));
  const message = h('div', { class: 'graph-message', role: 'status' }, h('p', { class: 'loading', text: 'Loading graph...' }));
  const newPill = h('button', { type: 'button', class: 'new-pill graph-new-pill', hidden: true, onClick: () => goToNewest() });
  const live = h('div', { class: 'gsr-only', role: 'status', 'aria-live': 'polite' });
  const canvas = h('div', {
    class: 'graph-canvas',
    tabindex: 0,
    role: 'region',
    'aria-roledescription': 'canvas',
    'aria-label': 'Agent graph. Arrow keys move between cards, Enter opens, plus and minus zoom, 0 fits, 1 is 100%, Shift and arrows pan, m toggles the minimap.',
    dataset: { lod: 'full', tools: 'on' },
    onPointerdown: onPointerDown,
    onPointermove: onPointerMove,
    onPointerup: onPointerUp,
    onPointercancel: onPointerUp,
    onWheel: onWheel,
    onClick: onClick,
    onDblclick: onDblclick,
    onKeydown: onKeydown,
    onScroll: () => {
      canvas.scrollLeft = 0;
      canvas.scrollTop = 0;
    },
  }, world, toolbar, banners, legend, message, newPill, live);
  const root = h('div', { class: 'graph-view' }, canvas);
  replace(container, root);
  toolsSelect.value = s.toolsMode;

  const minimap = createMinimap(canvas, {
    getView: () => ({ vp: s.vp, w: s.view.w, h: s.view.h }),
    onPan: (vp) => setViewport(vp),
    onZoom: (factor) => zoomCenter(factor),
  });
  setMinimapVisible(minimapDefaultVisible(window.innerWidth));

  const resizeObserver = new ResizeObserver(() => onResize());
  resizeObserver.observe(canvas);

  function later(fn, ms) {
    const id = setTimeout(() => {
      timers.delete(id);
      if (!s.destroyed) fn();
    }, ms);
    timers.add(id);
  }

  // ---- viewport ----------------------------------------------------------------

  function setViewport(vp) {
    s.vp = { x: Math.round(vp.x * 100) / 100, y: Math.round(vp.y * 100) / 100, k: clampZoom(vp.k) };
    if (frameQueued) return;
    frameQueued = true;
    requestAnimationFrame(applyFrame);
  }

  /** One frame: world transform, zoom label, LOD, minimap rectangle, throttled culling. */
  function applyFrame() {
    frameQueued = false;
    if (s.destroyed) return;
    const { x, y, k } = s.vp;
    world.style.setProperty('transform', `translate(${x}px, ${y}px) scale(${k})`);
    zoomLabel.textContent = `${Math.round(k * 100)}%`;
    canvas.dataset.lod = lodBand(k);
    canvas.dataset.tools = toolsVisibleAt(k) ? 'on' : 'off';
    minimap.setViewport(s.vp, s.view.w, s.view.h);
    if (Date.now() - lastCull >= CULL_INTERVAL_MS) cull();
    else if (!cullQueued) {
      cullQueued = true;
      later(() => {
        cullQueued = false;
        cull();
      }, CULL_INTERVAL_MS);
    }
    updateNewPill();
  }

  function zoomCenter(factor) {
    setViewport(zoomBy(s.vp, factor, s.view.w, s.view.h));
  }

  function zoomTo(k) {
    setViewport(zoomAt(s.vp, k, s.view.w / 2, s.view.h / 2));
  }

  function fit() {
    if (!s.layout || s.view.w === 0) return;
    setViewport(fitBounds(s.layout.bounds, s.view.w, s.view.h));
  }

  function positionOf(id) {
    return s.layout && id ? s.layout.positions.get(id) || null : null;
  }

  /** G-FR-39 and G-FR-52: pan so the card is fully visible and not under the minimap. */
  function revealCard(id) {
    const pos = positionOf(id);
    if (!pos || s.view.w === 0) return;
    let vp = ensureVisible(s.vp, pos, s.view.w, s.view.h);
    if (minimap.isVisible()) vp = avoidMinimap(vp, pos, s.view.w, s.view.h, minimap.size());
    if (vp !== s.vp) setViewport(vp);
  }

  function onResize() {
    const box = canvas.getBoundingClientRect();
    s.view = { w: Math.round(box.width), h: Math.round(box.height) };
    if (s.view.w === 0) return;
    if (s.layout && !s.initialDone) applyInitialViewport();
    setViewport(s.vp);
  }

  function applyInitialViewport() {
    const rootPos = positionOf(s.visible.rootId);
    const selected = s.selectedId && s.selectedId !== s.visible.rootId ? positionOf(s.selectedId) : null;
    s.initialDone = true;
    setViewport(initialViewport(s.layout.bounds, rootPos, selected, s.view.w, s.view.h));
  }

  // ---- culling (G-PERF-8) ------------------------------------------------------

  function attach(entry, layer, shouldAttach) {
    if (shouldAttach && !entry.attached) layer.appendChild(entry.el);
    if (!shouldAttach && entry.attached) entry.el.remove();
    entry.attached = shouldAttach;
  }

  function cull() {
    lastCull = Date.now();
    if (!s.layout) return;
    const isCulling = nodeEls.size > CULL_THRESHOLD && s.view.w > 0;
    const area = isCulling ? visibleWorldRect(s.vp, s.view.w, s.view.h, 1) : null;
    const inView = (id) => {
      if (!isCulling) return true;
      const pos = s.layout.positions.get(id);
      return Boolean(pos && intersects(pos, area));
    };
    for (const [id, entry] of nodeEls) attach(entry, entry.layer, inView(id) || entry.el.contains(document.activeElement));
    for (const entry of edgeEls.values()) attach(entry, edgeLayer, inView(entry.from) || inView(entry.to));
    keepCardOrder();
  }

  /** Keep the attached cards in depth-first DOM order so the ARIA tree reads in order. */
  function keepCardOrder() {
    let previous = null;
    for (const a of s.visible.agents) {
      const entry = nodeEls.get(a.node.id);
      if (!entry || !entry.attached) continue;
      if (previous && entry.el.previousElementSibling !== previous) previous.after(entry.el);
      if (!previous && entry.el !== cardLayer.firstElementChild) cardLayer.prepend(entry.el);
      previous = entry.el;
    }
  }

  // ---- rendering -------------------------------------------------------------

  function focusedCardId() {
    const el = document.activeElement;
    const card = el && cardLayer.contains(el) ? el.closest('.gcard') : null;
    return card ? card.dataset.id : null;
  }

  function tabbableId() {
    return s.visible.agents.some((a) => a.node.id === s.selectedId) ? s.selectedId : s.visible.rootId;
  }

  function isOwnerRunning(ownerId) {
    const owner = s.visible.index.agents.get(ownerId);
    return Boolean(owner && owner.status === 'running');
  }

  function buildAgent(node, ctx) {
    const entry = ctx.entries.get(node.id);
    const opts = {
      selected: node.id === s.selectedId,
      tabbable: node.id === ctx.tabbable,
      description: describeAgent(s.graph, node.id, s.visible.index),
      sessionTitle: node.isRoot ? s.sessionTitle : null,
      onToggle: toggleCollapse,
    };
    const sig = JSON.stringify([node, entry.level, entry.hasChildren, entry.collapsed, entry.hidden,
      opts.selected, opts.tabbable, opts.description, opts.sessionTitle]);
    return { sig, layer: cardLayer, make: () => agentCard(entry, opts) };
  }

  /** `{sig, layer, make}` for one visible node, or null for an unknown kind. */
  function buildNode(node, ctx) {
    if (node.kind === 'agent') return buildAgent(node, ctx);
    if (node.kind === 'session') return { sig: JSON.stringify(node), layer: nodeLayer, make: () => sessionCard(node) };
    if (node.kind === 'tool') {
      const active = node.pending > 0 && isOwnerRunning(node.ownerId);
      return { sig: JSON.stringify([node, active]), layer: nodeLayer, make: () => toolPill(node, active) };
    }
    if (node.kind === 'skill') return { sig: JSON.stringify(node), layer: nodeLayer, make: () => skillPill(node) };
    if (node.kind === MORE) return { sig: JSON.stringify(node), layer: nodeLayer, make: () => morePill(node) };
    return null;
  }

  function markTransient(el, cls, ms) {
    el.classList.add(cls);
    later(() => el.classList.remove(cls), ms);
  }

  function patchNode(node, built, pos, ctx) {
    const prev = nodeEls.get(node.id);
    if (prev && prev.sig === built.sig) {
      placeAt(prev.el, pos);
      return;
    }
    const el = placeAt(built.make(), pos);
    if (prev) {
      if (prev.attached) prev.el.replaceWith(el);
      if (s.animating && node.kind === 'tool') markTransient(el, 'is-flash', FLASH_MS);
      if (ctx.focused === node.id) el.focus({ preventScroll: true });
    } else if (s.animating && ctx.added.has(node.id)) {
      markTransient(el, 'is-new', NEW_HIGHLIGHT_MS);
    }
    nodeEls.set(node.id, { el, sig: built.sig, layer: built.layer, attached: Boolean(prev && prev.attached) });
  }

  /** Diff the visible nodes against the DOM: only added, removed and changed elements are touched. */
  function syncNodes(added) {
    const ctx = { entries: new Map(s.visible.agents.map((a) => [a.node.id, a])), tabbable: tabbableId(), focused: focusedCardId(), added };
    const seen = new Set();
    for (const node of s.visible.nodes) {
      const built = buildNode(node, ctx);
      const pos = s.layout.positions.get(node.id);
      if (!built || !pos) continue;
      seen.add(node.id);
      patchNode(node, built, pos, ctx);
    }
    for (const [id, entry] of nodeEls) {
      if (seen.has(id)) continue;
      nodeEls.delete(id);
      removeLater(entry.el);
    }
  }

  function removeLater(el) {
    if (!s.animating || !el.isConnected) {
      el.remove();
      return;
    }
    el.classList.add('is-leaving');
    later(() => el.remove(), LEAVE_MS);
  }

  function syncEdges() {
    const seen = new Set();
    const active = activeEdgeIds(s.visible);
    for (const edge of s.visible.edges) {
      const geometry = s.layout.edges.get(edge.id);
      if (!geometry) continue;
      seen.add(edge.id);
      const isActive = active.has(edge.id);
      const sig = JSON.stringify([edge, geometry, isActive]);
      const prev = edgeEls.get(edge.id);
      if (prev && prev.sig === sig) continue;
      const el = edgeElement(edge, geometry, suffix, isActive);
      if (prev && prev.attached) prev.el.replaceWith(el);
      edgeEls.set(edge.id, { el, sig, from: edge.from, to: edge.to, attached: Boolean(prev && prev.attached) });
    }
    for (const [id, entry] of edgeEls) {
      if (seen.has(id)) continue;
      edgeEls.delete(id);
      entry.el.remove();
    }
    // Edge paths are in raw layout coordinates; the anchor offset moves them as a group.
    edgeLayer.setAttribute('transform', `translate(${s.offset.x} ${s.offset.y})`);
  }

  /** Re-derive, re-layout and patch the DOM, keeping the anchor card still (10.7). */
  function rebuild({ animate = false, added = new Set() } = {}) {
    if (!s.graph) return;
    const anchorId = focusedCardId() || s.selectedId || (s.visible && s.visible.rootId);
    const before = positionOf(anchorId);
    s.visible = deriveVisible(s.graph, { toolsMode: s.toolsMode, selectedId: s.selectedId, collapsed: s.collapsed });
    const raw = layoutGraph(s.visible);
    const after = raw.positions.get(anchorId);
    if (before && after) s.offset = { x: before.x - after.x, y: before.y - after.y };
    s.layout = shiftLayout(raw, s.offset);
    s.animating = animate && !reducedMotion();
    canvas.classList.toggle('no-anim', !s.animating);
    syncNodes(added);
    syncEdges();
    cull();
    s.animating = false;
    renderChrome();
    minimap.setLayout(s.layout, s.visible.nodes, s.selectedId);
    if (s.view.w > 0 && !s.initialDone) applyInitialViewport();
    setViewport(s.vp);
  }

  // ---- chrome: toolbar state, banners, messages ------------------------------

  function truncatedBanner() {
    const shown = s.graph.nodes.filter((n) => n.kind === 'tool' || n.kind === 'skill').length;
    const omitted = s.graph.nodes.reduce((acc, n) => acc + (n.kind === 'agent' ? n.toolsOmitted || 0 : 0), 0);
    return h('div', { class: 'graph-banner', role: 'status' },
      h('span', { text: `Showing ${shown} of ${shown + omitted} tool nodes. Switch tools to none or selected, or use the tree view.` }),
      h('button', { type: 'button', class: 'btn btn-small', text: 'Tree view', onClick: () => onRequestTreeView && onRequestTreeView() }));
  }

  function renderChrome() {
    const g = s.graph;
    indexingChip.hidden = g.indexed !== false;
    toolsSelect.value = s.toolsMode;
    const hint = toolsHintText(s.visible, s.selectedId);
    toolsHint.hidden = !hint;
    if (hint) toolsHint.textContent = hint;
    replace(banners,
      g.truncated && truncatedBanner(),
      s.visible.guarded && h('div', { class: 'graph-banner', role: 'status', text: 'Too many nodes for all tools; showing the selected agent only.' }));
    const onlyRoot = s.visible.agents.length <= 1 && s.visible.sessions.length === 0
      && !g.nodes.some((n) => n.kind === 'tool' || n.kind === 'skill');
    replace(message, onlyRoot && h('p', { class: 'empty', text: 'No subagents, tools or skills yet. The graph updates live.' }));
    message.classList.toggle('is-hint', onlyRoot);
  }

  // ---- live updates (G-FR-35, G-FR-42) ------------------------------------------

  function announce(update) {
    pendingAnnounce = {
      agents: pendingAnnounce.agents + (update.agents || 0),
      status: update.status || pendingAnnounce.status,
    };
    if (announceQueued) return;
    announceQueued = true;
    later(flushAnnounce, Math.max(0, lastAnnounce + ANNOUNCE_INTERVAL_MS - Date.now()));
  }

  function flushAnnounce() {
    announceQueued = false;
    const { agents, status } = pendingAnnounce;
    pendingAnnounce = { agents: 0, status: null };
    const parts = [agents > 0 && `${agents} new agent${agents === 1 ? '' : 's'}`, status].filter(Boolean);
    if (parts.length === 0) return;
    lastAnnounce = Date.now();
    live.textContent = parts.join('. ');
  }

  function noteChanges(prev, next, diff) {
    const kinds = new Map(next.nodes.map((n) => [n.id, n]));
    const agentsAdded = diff.added.filter((id) => kinds.get(id).kind === 'agent');
    const oldSel = prev.nodes.find((n) => n.id === s.selectedId);
    const newSel = kinds.get(s.selectedId);
    const status = oldSel && newSel && oldSel.status !== newSel.status ? `selected agent is now ${newSel.status}` : null;
    if (agentsAdded.length > 0 || status) announce({ agents: agentsAdded.length, status });
    return agentsAdded;
  }

  function trackOffscreen(agentIds) {
    if (s.view.w === 0) return;
    const area = visibleWorldRect(s.vp, s.view.w, s.view.h, 0);
    for (const id of agentIds) {
      const pos = positionOf(id);
      if (pos && !intersects(pos, area)) s.newAgents.push(id);
    }
    updateNewPill();
  }

  function updateNewPill() {
    s.newAgents = s.newAgents.filter((id) => positionOf(id));
    const n = s.newAgents.length;
    newPill.hidden = n === 0;
    if (n > 0) newPill.textContent = `${n} new agent${n === 1 ? '' : 's'}`;
  }

  function goToNewest() {
    const pos = positionOf(s.newAgents[s.newAgents.length - 1]);
    s.newAgents = [];
    updateNewPill();
    if (pos) setViewport(centerOn(pos, s.view.w, s.view.h, s.vp.k));
  }

  // ---- selection and interaction ---------------------------------------------

  function expandAncestors(id) {
    const agents = s.visible ? s.visible.index.agents : null;
    let cursor = agents && agents.get(id) ? agents.get(id).parentId : null;
    const guard = new Set();
    while (cursor && !guard.has(cursor)) {
      guard.add(cursor);
      s.collapsed.delete(cursor);
      cursor = agents.get(cursor) ? agents.get(cursor).parentId : null;
    }
  }

  function selectLocal(id) {
    if (s.selectedId === id) return;
    s.selectedId = id;
    expandAncestors(id);
    rebuild({ animate: true });
  }

  function focusCard(id) {
    const entry = nodeEls.get(id);
    if (!entry) return;
    if (!entry.attached) attach(entry, cardLayer, true);
    entry.el.focus({ preventScroll: true });
    revealCard(id);
  }

  function report(id, open, isKeyboard, toolName = null) {
    if (onSelect) onSelect(id, { open, replace: isKeyboard, toolName });
  }

  function keyboardSelect(id) {
    if (!id) return;
    selectLocal(id);
    focusCard(id);
    report(id, false, true);
  }

  function toggleCollapse(id) {
    if (s.collapsed.has(id)) s.collapsed.delete(id);
    else s.collapsed.add(id);
    rebuild({ animate: true });
  }

  function changeToolsMode(mode) {
    const next = normalizeToolsMode(mode);
    if (next === s.toolsMode) return;
    s.toolsMode = next;
    rebuild({ animate: true });
    if (onToolsModeChange) onToolsModeChange(next);
  }

  function toggleLegend() {
    legend.hidden = !legend.hidden;
    legendBtn.setAttribute('aria-pressed', legend.hidden ? 'false' : 'true');
  }

  function setMinimapVisible(isVisible) {
    minimap.setVisible(isVisible);
    minimapBtn.setAttribute('aria-pressed', isVisible ? 'true' : 'false');
    if (isVisible) minimap.setViewport(s.vp, s.view.w, s.view.h);
  }

  function nodeAt(target) {
    const el = target && target.closest ? target.closest('.gnode') : null;
    if (!el || !world.contains(el) || !s.visible) return null;
    return s.visible.nodes.find((n) => n.id === el.dataset.id) || null;
  }

  function activate(node) {
    if (node.kind === 'agent') {
      selectLocal(node.id);
      report(node.id, true, false);
    } else if (node.kind === 'session') {
      if (!node.missing && onNavigateSession) onNavigateSession(node.sessionKey);
    } else {
      selectLocal(node.ownerId);
      report(node.ownerId, true, false, node.kind === 'tool' ? node.name : null);
    }
  }

  function onClick(e) {
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    const node = nodeAt(e.target);
    if (node) activate(node);
  }

  /** G-FR-33: zoom to fit the card's visible subtree. */
  function onDblclick(e) {
    const node = nodeAt(e.target);
    if (!node || node.kind !== 'agent' || !s.layout) return;
    setViewport(fitBounds(subtreeBounds(s.layout, s.visible, node.id), s.view.w, s.view.h));
  }

  // ---- pointer: drag pan and pinch zoom (G-FR-16, G-FR-17, G-FR-20) ----------

  function pinchState() {
    const [a, b] = [...pointers.values()];
    const box = canvas.getBoundingClientRect();
    return {
      dist: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)),
      mid: { x: (a.x + b.x) / 2 - box.left, y: (a.y + b.y) / 2 - box.top },
    };
  }

  function onPointerDown(e) {
    if (isControl(e.target) || e.target.closest('.gcard-collapse, .graph-minimap, .graph-new-pill')) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2) {
      pinch = { ...pinchState(), vp: s.vp };
      drag = null;
    } else if (pointers.size === 1) {
      drag = { x: e.clientX, y: e.clientY, vp: s.vp, moved: false, id: e.pointerId };
    }
  }

  function onPointerMove(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pointers.size >= 2) {
      e.preventDefault();
      const now = pinchState();
      const zoomed = zoomAt(pinch.vp, pinch.vp.k * (now.dist / pinch.dist), pinch.mid.x, pinch.mid.y);
      setViewport(panBy(zoomed, now.mid.x - pinch.mid.x, now.mid.y - pinch.mid.y));
      return;
    }
    if (!drag || drag.id !== e.pointerId) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    if (!drag.moved) {
      drag.moved = true;
      canvas.setPointerCapture(e.pointerId);
      canvas.classList.add('is-panning');
    }
    e.preventDefault();
    setViewport(panBy(drag.vp, dx, dy));
  }

  function onPointerUp(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    if (drag && drag.moved) suppressClick = e.type === 'pointerup';
    if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
    canvas.classList.remove('is-panning');
    drag = null;
    if (pointers.size < 2) pinch = null;
    cull();
  }

  function onWheel(e) {
    if (isControl(e.target) || e.target.closest('.graph-minimap')) return;
    e.preventDefault();
    const unit = e.deltaMode === 1 ? LINE_HEIGHT_PX : e.deltaMode === 2 ? s.view.h : 1;
    if (e.ctrlKey || e.metaKey) {
      const box = canvas.getBoundingClientRect();
      const factor = wheelZoomFactor(e.deltaY * unit, e.ctrlKey && !e.metaKey);
      setViewport(zoomAt(s.vp, s.vp.k * factor, e.clientX - box.left, e.clientY - box.top));
      return;
    }
    setViewport(panBy(s.vp, -e.deltaX * unit, -e.deltaY * unit));
  }

  // ---- keyboard (G-FR-38, G-FR-41) -------------------------------------------

  function horizontalKey(key, entry) {
    const id = entry.node.id;
    if (key === 'ArrowRight') {
      if (entry.hasChildren && entry.collapsed) return toggleCollapse(id);
      if (entry.hasChildren) return keyboardSelect(s.visible.agents[s.visible.agents.indexOf(entry) + 1].node.id);
      return undefined;
    }
    if (entry.hasChildren && !entry.collapsed) return toggleCollapse(id);
    const parent = entry.node.parentId;
    return keyboardSelect(parent && s.visible.index.agents.has(parent) ? parent : null);
  }

  function cardKey(key, id) {
    const rows = s.visible.agents.map((a) => ({ key: a.node.id }));
    const entry = s.visible.agents.find((a) => a.node.id === id);
    if (!entry) return undefined;
    switch (key) {
      case 'ArrowDown': return keyboardSelect(moveSelection(rows, id, 1));
      case 'ArrowUp': return keyboardSelect(moveSelection(rows, id, -1));
      case 'Home': return keyboardSelect(rows[0].key);
      case 'End': return keyboardSelect(rows[rows.length - 1].key);
      case 'ArrowRight':
      case 'ArrowLeft': return horizontalKey(key, entry);
      default: return activate(entry.node);
    }
  }

  function shortcutKey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    const actions = {
      '+': () => zoomCenter(ZOOM_STEP),
      '=': () => zoomCenter(ZOOM_STEP),
      '-': () => zoomCenter(1 / ZOOM_STEP),
      _: () => zoomCenter(1 / ZOOM_STEP),
      0: () => fit(),
      1: () => zoomTo(1),
      m: () => setMinimapVisible(!minimap.isVisible()),
      M: () => setMinimapVisible(!minimap.isVisible()),
    };
    if (!actions[e.key]) return false;
    actions[e.key]();
    return true;
  }

  function onKeydown(e) {
    if (isControl(e.target) || !s.visible) return;
    const card = e.target.closest ? e.target.closest('.gcard') : null;
    const onButton = Boolean(e.target.closest('button'));
    if (PAN_KEYS[e.key] && (e.shiftKey || (!card && !onButton))) {
      e.preventDefault();
      const [dx, dy] = PAN_KEYS[e.key];
      setViewport(panBy(s.vp, dx * PAN_STEP, dy * PAN_STEP));
      return;
    }
    if (card && !onButton && CARD_KEYS.has(e.key)) {
      e.preventDefault();
      cardKey(e.key, card.dataset.id);
      return;
    }
    if (shortcutKey(e)) e.preventDefault();
  }

  // ---- errors ----------------------------------------------------------------

  function showError(err, { onRetry: retry = onRetry } = {}) {
    const isNotFound = err && (err.status === 404 || err.code === 'not_found');
    message.classList.remove('is-hint');
    replace(message, h('div', { class: 'graph-error' },
      errorBox(err),
      isNotFound
        ? h('a', { href: '#/', text: 'Back to the session list' })
        : h('div', { class: 'graph-error-actions' },
          retry && h('button', { type: 'button', class: 'btn', text: 'Retry', onClick: () => retry() }),
          h('button', { type: 'button', class: 'btn', text: 'Switch to tree view', onClick: () => onRequestTreeView && onRequestTreeView() }))));
  }

  // ---- public interface (12.4.2) ----------------------------------------------

  return {
    setGraph(graph, { reset = false } = {}) {
      if (!graph || !Array.isArray(graph.nodes)) return;
      const prev = s.graph;
      if (!reset && prev && Number.isInteger(graph.rev) && graph.rev <= (prev.rev || 0)) return;
      s.graph = graph;
      const isUpdate = Boolean(prev) && !reset;
      const added = isUpdate ? noteChanges(prev, graph, diffGraphs(prev, graph)) : [];
      rebuild({ animate: isUpdate, added: new Set(added) });
      if (added.length > 0) trackOffscreen(added);
    },
    rev() {
      return s.graph && Number.isInteger(s.graph.rev) ? s.graph.rev : 0;
    },
    setSelected(agentKey) {
      const id = agentKey || null;
      if (s.selectedId === id) return;
      s.selectedId = id;
      expandAncestors(id);
      rebuild({ animate: true });
      if (s.initialDone) revealCard(id);
    },
    setToolsMode(mode) {
      const next = normalizeToolsMode(mode);
      if (next === s.toolsMode) return;
      s.toolsMode = next;
      rebuild({ animate: true });
    },
    /** Optional addition: the session title shown on the root card (10.3 row 2). */
    setSessionTitle(title) {
      const next = title || null;
      if (next === s.sessionTitle) return;
      s.sessionTitle = next;
      rebuild();
    },
    focusSelected() {
      const id = s.visible ? tabbableId() : null;
      if (id && nodeEls.has(id)) focusCard(id);
      else canvas.focus({ preventScroll: true });
    },
    fit,
    setMinimapVisible,
    showError,
    destroy() {
      s.destroyed = true;
      resizeObserver.disconnect();
      for (const id of timers) clearTimeout(id);
      timers.clear();
      minimap.destroy();
      root.remove();
    },
  };
}
