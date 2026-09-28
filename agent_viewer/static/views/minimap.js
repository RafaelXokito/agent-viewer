// Minimap element (FEATURE-graph-canvas 6.8, 10.6a): one SVG with a rect per
// visible node and one viewport rect. No text, so no transcript content.
// Pointer input pans the canvas through `onPan(viewport)`; the wheel zooms it
// through `onZoom(factor)`. Mounted and kept in sync by views/graph.js.

import { svg } from '../dom.js';
import {
  minimapSize, minimapTransform, nodeRect, panTargetForPoint, viewportRect,
} from '../lib/minimap.js';
import { wheelZoomFactor } from '../lib/viewport.js';

function kindClass(node, selectedId) {
  const base = node.kind === 'agent' ? `mm-agent mm-${node.status || 'unknown'}` : `mm-${node.kind}`;
  return node.id === selectedId ? `${base} mm-selected` : base;
}

function setRect(el, r) {
  el.setAttribute('x', String(r.x));
  el.setAttribute('y', String(r.y));
  el.setAttribute('width', String(r.w));
  el.setAttribute('height', String(r.h));
}

/**
 * `getView()` returns `{vp, w, h}` of the canvas; `onPan(vp)` applies a new
 * viewport without animation; `onZoom(factor)` zooms around the view center.
 */
export function createMinimap(parent, { getView, onPan, onZoom }) {
  let size = minimapSize(window.innerWidth);
  let transform = null;
  let visible = true;
  let dragging = false;
  let last = null;
  const nodesLayer = svg('g', { class: 'mm-nodes' });
  const viewRect = svg('rect', { class: 'mm-view', x: 0, y: 0, width: 0, height: 0 });
  const el = svg('svg', {
    class: 'graph-minimap',
    'aria-hidden': 'true',
    focusable: 'false',
    width: size.w,
    height: size.h,
    viewBox: `0 0 ${size.w} ${size.h}`,
    onPointerdown: onPointerDown,
    onPointermove: onPointerMove,
    onPointerup: onPointerUp,
    onPointercancel: onPointerUp,
    onWheel: onWheelEvent,
  }, svg('rect', { class: 'mm-bg', x: 0, y: 0, width: '100%', height: '100%' }), nodesLayer, viewRect);
  parent.appendChild(el);

  function localPoint(e) {
    const box = el.getBoundingClientRect();
    return { x: e.clientX - box.left, y: e.clientY - box.top };
  }

  function panToPointer(e) {
    if (!transform) return;
    const view = getView();
    const p = localPoint(e);
    onPan(panTargetForPoint(transform, view.vp, p.x, p.y, view.w, view.h));
  }

  function onPointerDown(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    dragging = true;
    el.setPointerCapture(e.pointerId);
    panToPointer(e);
  }

  function onPointerMove(e) {
    if (!dragging) return;
    e.preventDefault();
    panToPointer(e);
  }

  function onPointerUp(e) {
    if (!dragging) return;
    dragging = false;
    if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
  }

  function onWheelEvent(e) {
    e.preventDefault();
    e.stopPropagation();
    onZoom(wheelZoomFactor(e.deltaY, e.ctrlKey || e.metaKey));
  }

  function resize() {
    const next = minimapSize(window.innerWidth);
    if (next === size) return false;
    size = next;
    el.setAttribute('width', String(size.w));
    el.setAttribute('height', String(size.h));
    el.setAttribute('viewBox', `0 0 ${size.w} ${size.h}`);
    return true;
  }

  function rebuild(layout, nodes, selectedId) {
    last = { layout, nodes, selectedId };
    resize();
    transform = minimapTransform(layout.bounds, size.w, size.h);
    const rects = [];
    for (const node of nodes) {
      const pos = layout.positions.get(node.id);
      if (!pos) continue;
      const rect = svg('rect', { class: kindClass(node, selectedId) });
      setRect(rect, nodeRect(transform, pos));
      rects.push(rect);
    }
    nodesLayer.replaceChildren(...rects);
  }

  return {
    element: el,
    size: () => size,
    isVisible: () => visible,
    setVisible(isVisible) {
      visible = Boolean(isVisible);
      el.classList.toggle('is-hidden', !visible);
    },
    /** Rebuild node rects from the layout (G-FR-49); `nodes` are the visible nodes. */
    setLayout: rebuild,
    /** Move the viewport rect: four attributes of one element (G-PERF-11). */
    setViewport(vp, viewW, viewH) {
      if (resize() && last) rebuild(last.layout, last.nodes, last.selectedId);
      if (!transform) return;
      setRect(viewRect, viewportRect(transform, vp, viewW, viewH, size.w, size.h));
    },
    isDragging: () => dragging,
    destroy() {
      el.remove();
    },
  };
}
