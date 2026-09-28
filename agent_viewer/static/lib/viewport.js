// Pure viewport math for the graph canvas (FEATURE-graph-canvas 6.3, 9, 10.5).
// A viewport is `{x, y, k}`: screen = world * k + (x, y). Every function
// returns a new object and never mutates its input.

export const ZOOM_MIN = 0.1;
export const ZOOM_MAX = 2.0;
export const ZOOM_STEP = 1.2;
export const FIT_MIN_ZOOM = 0.6;
export const FIT_PADDING = 32;
export const PAN_STEP = 100;
export const LOD_FULL = 0.6;
export const LOD_COMPACT = 0.3;
export const TOOLS_MIN_ZOOM = 0.45;
const ENSURE_MARGIN = 24;
const TOP_MARGIN = 56;

export function clampZoom(k) {
  if (!Number.isFinite(k)) return 1;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, k));
}

export function screenToWorld(vp, sx, sy) {
  return { x: (sx - vp.x) / vp.k, y: (sy - vp.y) / vp.k };
}

export function worldToScreen(vp, wx, wy) {
  return { x: wx * vp.k + vp.x, y: wy * vp.k + vp.y };
}

export function panBy(vp, dx, dy) {
  return { x: vp.x + dx, y: vp.y + dy, k: vp.k };
}

/** Zoom to `k` (clamped) keeping the world point under screen (sx, sy) fixed. */
export function zoomAt(vp, k, sx, sy) {
  const next = clampZoom(k);
  const w = screenToWorld(vp, sx, sy);
  return { x: sx - w.x * next, y: sy - w.y * next, k: next };
}

/** Zoom by a factor around the view center. */
export function zoomBy(vp, factor, viewW, viewH) {
  return zoomAt(vp, vp.k * factor, viewW / 2, viewH / 2);
}

/** Zoom factor for a wheel delta: pinch (ctrlKey) deltas are small, so they get a larger gain. */
export function wheelZoomFactor(deltaY, isPinch) {
  const gain = isPinch ? 0.01 : 0.002;
  return Math.exp(-deltaY * gain);
}

/** Zoom that fits `bounds` in the view with padding, capped at 1 and clamped. */
export function fitZoom(bounds, viewW, viewH, padding = FIT_PADDING) {
  const bw = Math.max(1, bounds.w);
  const bh = Math.max(1, bounds.h);
  const k = Math.min((viewW - 2 * padding) / bw, (viewH - 2 * padding) / bh, 1);
  return clampZoom(k);
}

/** Viewport centering `rect` at zoom `k`. */
export function centerOn(rect, viewW, viewH, k) {
  const cx = rect.x + rect.w / 2;
  const cy = rect.y + rect.h / 2;
  return { x: viewW / 2 - cx * k, y: viewH / 2 - cy * k, k };
}

export function fitBounds(bounds, viewW, viewH, padding = FIT_PADDING) {
  return centerOn(bounds, viewW, viewH, fitZoom(bounds, viewW, viewH, padding));
}

/**
 * First-render viewport (G-FR-19): center the selected card at zoom 1 if
 * given; else fit when the fit zoom is at least FIT_MIN_ZOOM; else show the
 * root card at zoom 1 near the top center.
 */
export function initialViewport(bounds, rootRect, selectedRect, viewW, viewH) {
  if (selectedRect) return centerOn(selectedRect, viewW, viewH, 1);
  const k = fitZoom(bounds, viewW, viewH);
  if (k >= FIT_MIN_ZOOM || !rootRect) return centerOn(bounds, viewW, viewH, k);
  return { x: viewW / 2 - (rootRect.x + rootRect.w / 2), y: TOP_MARGIN - rootRect.y, k: 1 };
}

/** World rectangle visible in the view, grown by `margin` view sizes on each side. */
export function visibleWorldRect(vp, viewW, viewH, margin = 0) {
  const tl = screenToWorld(vp, 0 - margin * viewW, 0 - margin * viewH);
  const br = screenToWorld(vp, viewW * (1 + margin), viewH * (1 + margin));
  return { x: tl.x, y: tl.y, w: br.x - tl.x, h: br.y - tl.y };
}

export function intersects(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

function axisShift(start, size, viewSize, margin) {
  if (size + 2 * margin > viewSize) return margin - start;
  if (start < margin) return margin - start;
  if (start + size > viewSize - margin) return viewSize - margin - (start + size);
  return 0;
}

/** Smallest pan making the world `rect` fully visible with a margin (G-FR-39). */
export function ensureVisible(vp, rect, viewW, viewH, margin = ENSURE_MARGIN) {
  const s = worldToScreen(vp, rect.x, rect.y);
  const dx = axisShift(s.x, rect.w * vp.k, viewW, margin);
  const dy = axisShift(s.y, rect.h * vp.k, viewH, margin);
  if (dx === 0 && dy === 0) return vp;
  return panBy(vp, dx, dy);
}

/** Level of detail for a zoom (G-PERF-10). */
export function lodBand(k) {
  if (k >= LOD_FULL) return 'full';
  if (k >= LOD_COMPACT) return 'compact';
  return 'block';
}

export function toolsVisibleAt(k) {
  return k >= TOOLS_MIN_ZOOM;
}

/** Pan keeping the screen position of an anchor fixed after it moved in world space. */
export function keepAnchor(vp, before, after) {
  if (!before || !after) return vp;
  return panBy(vp, (before.x - after.x) * vp.k, (before.y - after.y) * vp.k);
}
