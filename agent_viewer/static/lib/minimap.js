// Pure minimap math (FEATURE-graph-canvas 6.8 and 10.6a): scale and offset
// over the padded layout bounds, world and minimap conversions, the viewport
// rectangle, click and drag pan targets, and the G-FR-52 offset.

import { screenToWorld, worldToScreen } from './viewport.js';

export const MINIMAP_LARGE = Object.freeze({ w: 200, h: 140 });
export const MINIMAP_SMALL = Object.freeze({ w: 160, h: 112 });
export const MINIMAP_MARGIN = 8;
export const MINIMAP_BREAKPOINT = 720;
export const MIN_NODE_SIZE = 2;
const PAD_RATIO = 0.05;

export function minimapSize(viewportWidth) {
  return viewportWidth <= MINIMAP_BREAKPOINT ? MINIMAP_SMALL : MINIMAP_LARGE;
}

export function minimapDefaultVisible(viewportWidth) {
  return viewportWidth > MINIMAP_BREAKPOINT;
}

/**
 * Transform `{scale, ox, oy}` mapping world to minimap: the bounds padded by
 * 5%, scaled to fit with the aspect ratio kept, and centered.
 */
export function minimapTransform(bounds, mmW, mmH) {
  const bw = Math.max(1, bounds.w);
  const bh = Math.max(1, bounds.h);
  const padX = bw * PAD_RATIO;
  const padY = bh * PAD_RATIO;
  const pw = bw + 2 * padX;
  const ph = bh + 2 * padY;
  const scale = Math.min(mmW / pw, mmH / ph);
  const ox = (mmW - pw * scale) / 2 - (bounds.x - padX) * scale;
  const oy = (mmH - ph * scale) / 2 - (bounds.y - padY) * scale;
  return { scale, ox, oy };
}

export function worldToMinimap(t, wx, wy) {
  return { x: t.ox + wx * t.scale, y: t.oy + wy * t.scale };
}

export function minimapToWorld(t, mx, my) {
  return { x: (mx - t.ox) / t.scale, y: (my - t.oy) / t.scale };
}

/** Integer minimap rectangle for a node, at least MIN_NODE_SIZE on each side. */
export function nodeRect(t, pos) {
  const p = worldToMinimap(t, pos.x, pos.y);
  return {
    x: Math.round(p.x),
    y: Math.round(p.y),
    w: Math.max(MIN_NODE_SIZE, Math.round(pos.w * t.scale)),
    h: Math.max(MIN_NODE_SIZE, Math.round(pos.h * t.scale)),
  };
}

function clampSpan(start, end, max) {
  const a = Math.min(Math.max(start, 0), max);
  const b = Math.min(Math.max(end, 0), max);
  return [a, Math.max(0, b - a)];
}

/** Minimap rectangle of the canvas viewport, clamped to the minimap area. */
export function viewportRect(t, vp, viewW, viewH, mmW, mmH) {
  const a = screenToWorld(vp, 0, 0);
  const b = screenToWorld(vp, viewW, viewH);
  const tl = worldToMinimap(t, a.x, a.y);
  const br = worldToMinimap(t, b.x, b.y);
  const [x, w] = clampSpan(tl.x, br.x, mmW);
  const [y, h] = clampSpan(tl.y, br.y, mmH);
  return { x, y, w, h };
}

/** Viewport centering the world point under minimap point (mx, my), zoom unchanged. */
export function panTargetForPoint(t, vp, mx, my, viewW, viewH) {
  const w = minimapToWorld(t, mx, my);
  return { x: viewW / 2 - w.x * vp.k, y: viewH / 2 - w.y * vp.k, k: vp.k };
}

/** Viewport after dragging the minimap by (dmx, dmy) minimap pixels. */
export function panTargetForDrag(t, vp, dmx, dmy) {
  return { x: vp.x - (dmx / t.scale) * vp.k, y: vp.y - (dmy / t.scale) * vp.k, k: vp.k };
}

/**
 * G-FR-52: if the world `rect` would sit under the minimap (bottom-right,
 * `mm` = {w, h}), pan it up above the minimap, or left of it when the card
 * does not fit above.
 */
export function avoidMinimap(vp, rect, viewW, viewH, mm, margin = MINIMAP_MARGIN) {
  if (!mm) return vp;
  const s = worldToScreen(vp, rect.x, rect.y);
  const w = rect.w * vp.k;
  const h = rect.h * vp.k;
  const left = viewW - mm.w - margin;
  const top = viewH - mm.h - margin;
  const overlaps = s.x < viewW - margin && s.x + w > left && s.y < viewH - margin && s.y + h > top;
  if (!overlaps) return vp;
  const dy = top - margin - (s.y + h);
  if (s.y + dy >= margin) return { x: vp.x, y: vp.y + dy, k: vp.k };
  return { x: vp.x + (left - margin - (s.x + w)), y: vp.y, k: vp.k };
}
