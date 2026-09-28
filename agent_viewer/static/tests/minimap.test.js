import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  MIN_NODE_SIZE, MINIMAP_LARGE, MINIMAP_SMALL, avoidMinimap, minimapDefaultVisible, minimapSize, minimapToWorld,
  minimapTransform, nodeRect, panTargetForDrag, panTargetForPoint, viewportRect, worldToMinimap,
} from '../lib/minimap.js';
import { screenToWorld, worldToScreen } from '../lib/viewport.js';

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
const MM = MINIMAP_LARGE;

test('wide bounds fill the minimap width with the aspect ratio kept and are centered vertically', () => {
  const b = { x: 0, y: 0, w: 2000, h: 200 };
  const t = minimapTransform(b, MM.w, MM.h);
  close(t.scale, MM.w / (2000 * 1.1));
  const tl = worldToMinimap(t, b.x - 100, b.y - 10);
  const br = worldToMinimap(t, b.x + b.w + 100, b.y + b.h + 10);
  close(tl.x, 0);
  close(br.x, MM.w);
  close((tl.y + br.y) / 2, MM.h / 2);
});

test('tall bounds fill the minimap height and are centered horizontally', () => {
  const b = { x: -500, y: 100, w: 300, h: 3000 };
  const t = minimapTransform(b, MM.w, MM.h);
  close(t.scale, MM.h / (3000 * 1.1));
  const center = worldToMinimap(t, b.x + b.w / 2, b.y + b.h / 2);
  close(center.x, MM.w / 2);
  close(center.y, MM.h / 2);
});

test('a single node is scaled to fit, not divided by zero', () => {
  const t = minimapTransform({ x: 10, y: 10, w: 240, h: 88 }, MM.w, MM.h);
  assert.ok(Number.isFinite(t.scale) && t.scale > 0);
  const t0 = minimapTransform({ x: 0, y: 0, w: 0, h: 0 }, MM.w, MM.h);
  assert.ok(Number.isFinite(t0.scale));
});

test('world to minimap and back is a round trip', () => {
  const t = minimapTransform({ x: -300, y: 40, w: 1800, h: 900 }, MM.w, MM.h);
  const m = worldToMinimap(t, 123, 456);
  const w = minimapToWorld(t, m.x, m.y);
  close(w.x, 123);
  close(w.y, 456);
});

test('node rectangles are integers and at least 2 x 2 px', () => {
  const t = minimapTransform({ x: 0, y: 0, w: 100000, h: 1000 }, MM.w, MM.h);
  const r = nodeRect(t, { x: 5000, y: 10, w: 160, h: 26 });
  assert.equal(r.w, MIN_NODE_SIZE);
  assert.equal(r.h, MIN_NODE_SIZE);
  for (const v of Object.values(r)) assert.ok(Number.isInteger(v));
});

test('the viewport rectangle follows pan and zoom and is clamped when the graph is all visible', () => {
  const b = { x: 0, y: 0, w: 4000, h: 2000 };
  const t = minimapTransform(b, MM.w, MM.h);
  const vp = { x: -1000, y: -500, k: 1 };
  const r = viewportRect(t, vp, 800, 600, MM.w, MM.h);
  const expect = worldToMinimap(t, 1000, 500);
  close(r.x, expect.x);
  close(r.y, expect.y);
  close(r.w, 800 * t.scale);
  const zoomed = viewportRect(t, { ...vp, k: 2 }, 800, 600, MM.w, MM.h);
  assert.ok(zoomed.w < r.w, 'zooming in shrinks it');
  const all = viewportRect(t, { x: 5000, y: 5000, k: 0.1 }, 800, 600, MM.w, MM.h);
  assert.ok(all.x >= 0 && all.y >= 0 && all.x + all.w <= MM.w && all.y + all.h <= MM.h);
});

test('a click centers the viewport on that world point without changing zoom', () => {
  const t = minimapTransform({ x: 0, y: 0, w: 4000, h: 2000 }, MM.w, MM.h);
  const vp = { x: 0, y: 0, k: 0.5 };
  const next = panTargetForPoint(t, vp, 150, 100, 800, 600);
  assert.equal(next.k, 0.5);
  const world = minimapToWorld(t, 150, 100);
  const center = screenToWorld(next, 400, 300);
  close(center.x, world.x);
  close(center.y, world.y);
});

test('a drag delta pans by the same world distance', () => {
  const t = minimapTransform({ x: 0, y: 0, w: 4000, h: 2000 }, MM.w, MM.h);
  const vp = { x: 10, y: 20, k: 0.8 };
  const next = panTargetForDrag(t, vp, 10, -5);
  const c0 = screenToWorld(vp, 400, 300);
  const c1 = screenToWorld(next, 400, 300);
  close(c1.x - c0.x, 10 / t.scale);
  close(c1.y - c0.y, -5 / t.scale);
});

test('G-FR-52: a card under the minimap is moved above it, or left of it when it cannot fit above', () => {
  const vp = { x: 0, y: 0, k: 1 };
  const card = { x: 700, y: 520, w: 240, h: 88 };
  const next = avoidMinimap(vp, card, 1000, 700, MM);
  const s = worldToScreen(next, card.x, card.y);
  assert.ok(s.y + card.h <= 700 - MM.h - 8);
  assert.equal(next.x, 0);
  assert.equal(avoidMinimap(vp, { x: 10, y: 10, w: 240, h: 88 }, 1000, 700, MM), vp);
  const shortView = avoidMinimap(vp, { x: 700, y: 20, w: 240, h: 88 }, 1000, 160, MM);
  assert.equal(shortView.y, 0);
  assert.ok(worldToScreen(shortView, 700, 20).x + 240 <= 1000 - MM.w - 8);
  assert.equal(avoidMinimap(vp, card, 1000, 700, null), vp);
});

test('size and default visibility depend on the 720 px breakpoint', () => {
  assert.equal(minimapSize(721), MINIMAP_LARGE);
  assert.equal(minimapSize(720), MINIMAP_SMALL);
  assert.deepEqual(MINIMAP_SMALL, { w: 160, h: 112 });
  assert.equal(minimapDefaultVisible(1280), true);
  assert.equal(minimapDefaultVisible(390), false);
});
