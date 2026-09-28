import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ZOOM_MAX, ZOOM_MIN, centerOn, clampZoom, ensureVisible, fitBounds, fitZoom, initialViewport, intersects, keepAnchor,
  lodBand, panBy, screenToWorld, toolsVisibleAt, visibleWorldRect, wheelZoomFactor, worldToScreen, zoomAt, zoomBy,
} from '../lib/viewport.js';

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test('zooming around a point keeps that world point under the cursor', () => {
  const vp = { x: 40, y: -20, k: 0.8 };
  const before = screenToWorld(vp, 300, 200);
  const next = zoomAt(vp, 1.7, 300, 200);
  const after = screenToWorld(next, 300, 200);
  close(before.x, after.x);
  close(before.y, after.y);
  assert.equal(next.k, 1.7);
  assert.deepEqual(vp, { x: 40, y: -20, k: 0.8 }, 'input not mutated');
});

test('zoom is clamped to [0.1, 2.0]', () => {
  assert.equal(clampZoom(5), ZOOM_MAX);
  assert.equal(clampZoom(0.01), ZOOM_MIN);
  assert.equal(clampZoom(NaN), 1);
  assert.equal(zoomAt({ x: 0, y: 0, k: 1 }, 10, 0, 0).k, 2);
  assert.equal(zoomBy({ x: 0, y: 0, k: 0.1 }, 0.5, 100, 100).k, 0.1);
});

test('screen to world and back is a round trip', () => {
  const vp = { x: 13, y: 77, k: 0.35 };
  const w = screenToWorld(vp, 512, 384);
  const s = worldToScreen(vp, w.x, w.y);
  close(s.x, 512);
  close(s.y, 384);
});

test('fit centers the bounds and never zooms past 100%', () => {
  const bounds = { x: 100, y: 50, w: 2000, h: 500 };
  const vp = fitBounds(bounds, 1000, 800);
  close(vp.k, (1000 - 64) / 2000);
  const center = worldToScreen(vp, 1100, 300);
  close(center.x, 500);
  close(center.y, 400);
  assert.equal(fitZoom({ x: 0, y: 0, w: 10, h: 10 }, 1000, 800), 1);
  assert.equal(fitZoom({ x: 0, y: 0, w: 1e6, h: 10 }, 1000, 800), ZOOM_MIN);
});

test('initial viewport: fit when it fits at 0.6, else root at 100% near the top, selected centered', () => {
  const small = { x: 0, y: 0, w: 800, h: 400 };
  assert.equal(initialViewport(small, null, null, 1200, 800).k, 1);
  const wide = { x: 0, y: 0, w: 5000, h: 400 };
  const root = { x: 2400, y: 0, w: 240, h: 88 };
  const vp = initialViewport(wide, root, null, 1200, 800);
  assert.equal(vp.k, 1);
  close(worldToScreen(vp, 2520, 0).x, 600);
  assert.ok(worldToScreen(vp, 0, 0).y > 0);
  const sel = { x: 4000, y: 300, w: 240, h: 88 };
  const centered = initialViewport(wide, root, sel, 1200, 800);
  close(worldToScreen(centered, 4120, 344).x, 600);
  close(worldToScreen(centered, 4120, 344).y, 400);
});

test('ensure visible pans the least amount to show a card with a margin', () => {
  const vp = { x: 0, y: 0, k: 1 };
  assert.equal(ensureVisible(vp, { x: 100, y: 100, w: 240, h: 88 }, 1000, 800), vp);
  const right = ensureVisible(vp, { x: 900, y: 100, w: 240, h: 88 }, 1000, 800);
  assert.equal(right.x, 1000 - 24 - 1140);
  assert.equal(right.y, 0);
  const up = ensureVisible(vp, { x: 100, y: -300, w: 240, h: 88 }, 1000, 800);
  assert.equal(up.y, 324);
  const huge = ensureVisible(vp, { x: 500, y: 0, w: 2000, h: 88 }, 1000, 800);
  assert.equal(huge.x, 24 - 500, 'too wide: align the left edge');
});

test('level of detail bands and tool visibility follow the zoom', () => {
  assert.equal(lodBand(1), 'full');
  assert.equal(lodBand(0.6), 'full');
  assert.equal(lodBand(0.59), 'compact');
  assert.equal(lodBand(0.3), 'compact');
  assert.equal(lodBand(0.29), 'block');
  assert.equal(toolsVisibleAt(0.45), true);
  assert.equal(toolsVisibleAt(0.44), false);
});

test('visible world rect, intersection, pan and anchor helpers', () => {
  const r = visibleWorldRect({ x: -100, y: 0, k: 2 }, 800, 600, 0);
  assert.deepEqual(r, { x: 50, y: 0, w: 400, h: 300 });
  const m = visibleWorldRect({ x: 0, y: 0, k: 1 }, 100, 100, 1);
  assert.deepEqual(m, { x: -100, y: -100, w: 300, h: 300 });
  assert.ok(intersects({ x: 0, y: 0, w: 10, h: 10 }, { x: 5, y: 5, w: 10, h: 10 }));
  assert.ok(!intersects({ x: 0, y: 0, w: 10, h: 10 }, { x: 10, y: 0, w: 10, h: 10 }));
  assert.deepEqual(panBy({ x: 1, y: 2, k: 3 }, 10, -2), { x: 11, y: 0, k: 3 });
  const vp = { x: 0, y: 0, k: 0.5 };
  const anchored = keepAnchor(vp, { x: 100, y: 100 }, { x: 300, y: 50 });
  assert.deepEqual(worldToScreen(anchored, 300, 50), worldToScreen(vp, 100, 100));
  assert.equal(keepAnchor(vp, null, { x: 1, y: 1 }), vp);
  assert.deepEqual(centerOn({ x: 0, y: 0, w: 100, h: 100 }, 200, 200, 1), { x: 50, y: 50, k: 1 });
});

test('wheel zoom factor: up zooms in, down zooms out, pinch has more gain', () => {
  assert.ok(wheelZoomFactor(-100, false) > 1);
  assert.ok(wheelZoomFactor(100, false) < 1);
  assert.ok(wheelZoomFactor(-10, true) > wheelZoomFactor(-10, false));
});
