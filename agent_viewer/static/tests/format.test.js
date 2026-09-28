import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  durationBetween, firstLine, formatBytes, formatClock, formatCompact, formatDateTime, formatDuration,
  formatInt, formatRelative, formatUsd, projectLabel, shortId, truncate,
} from '../lib/format.js';

test('formatInt adds thousands separators and shows a placeholder for missing values', () => {
  assert.equal(formatInt(979520), '979,520');
  assert.equal(formatInt(0), '0');
  assert.equal(formatInt(null), '-');
  assert.equal(formatInt(Number.NaN), '-');
});

test('formatCompact shortens large counts and carries over at unit boundaries', () => {
  assert.equal(formatCompact(999), '999');
  assert.equal(formatCompact(1000), '1k');
  assert.equal(formatCompact(979520), '980k');
  assert.equal(formatCompact(7368), '7.4k');
  assert.equal(formatCompact(1_250_000), '1.3M');
  assert.equal(formatCompact(999_999), '1M');
  assert.equal(formatCompact(undefined), '-');
});

test('formatDuration picks a readable unit for each magnitude', () => {
  assert.equal(formatDuration(777), '777ms');
  assert.equal(formatDuration(4200), '4.2s');
  assert.equal(formatDuration(32000), '32s');
  assert.equal(formatDuration(192000), '3m 12s');
  assert.equal(formatDuration(3_720_000), '1h 02m');
  assert.equal(formatDuration(90_000_000), '1d 1h');
  assert.equal(formatDuration(null), '-');
  assert.equal(formatDuration(-5), '-');
});

test('durationBetween returns milliseconds or null when a bound is missing', () => {
  assert.equal(durationBetween('2026-09-25T10:00:00.000Z', '2026-09-25T10:01:00.000Z'), 60000);
  assert.equal(durationBetween(null, '2026-09-25T10:01:00.000Z'), null);
  assert.equal(durationBetween('2026-09-25T10:01:00.000Z', '2026-09-25T10:00:00.000Z'), 0);
});

test('formatRelative describes elapsed time and treats future times as just now', () => {
  const now = Date.parse('2026-09-25T12:00:00.000Z');
  assert.equal(formatRelative('2026-09-25T12:00:00.000Z', now), 'just now');
  assert.equal(formatRelative('2026-09-25T12:00:10.000Z', now), 'just now');
  assert.equal(formatRelative('2026-09-25T11:59:30.000Z', now), '30s ago');
  assert.equal(formatRelative('2026-09-25T11:55:00.000Z', now), '5m ago');
  assert.equal(formatRelative('2026-09-25T09:00:00.000Z', now), '3h ago');
  assert.equal(formatRelative('2026-09-23T12:00:00.000Z', now), '2d ago');
  assert.equal(formatRelative('not a date', now), '-');
});

test('formatClock and formatDateTime render local time in fixed shapes', () => {
  assert.match(formatClock('2026-09-25T10:00:03.600Z'), /^\d{2}:\d{2}:\d{2}$/);
  assert.match(formatDateTime('2026-09-25T10:00:03.600Z'), /^2026-09-2\d \d{2}:\d{2}$/);
  assert.equal(formatClock(null), '-');
});

test('truncate cuts with an ellipsis only when needed', () => {
  assert.equal(truncate('short', 10), 'short');
  assert.equal(truncate('abcdefghij', 5), 'abcd…');
  assert.equal(truncate(null, 5), '');
  assert.equal(truncate('abc', 1), '…');
});

test('firstLine and shortId trim for dense display', () => {
  assert.equal(firstLine('  one\ntwo'), 'one');
  assert.equal(shortId('a1111111111111111'), 'a1111111');
  assert.equal(shortId(null), '');
});

test('projectLabel keeps the last two path parts of cwd', () => {
  assert.equal(projectLabel('/home/u/src/acme/my-project', 'slug'), 'acme/my-project');
  assert.equal(projectLabel('/tmp', 'slug'), 'tmp');
  assert.equal(projectLabel(null, '-tmp-demo'), '-tmp-demo');
  assert.equal(projectLabel(null, null), '-');
});

test('formatBytes and formatUsd format sizes and recorded costs', () => {
  assert.equal(formatBytes(512), '512 B');
  assert.equal(formatBytes(2048), '2.0 KB');
  assert.equal(formatBytes(3 * 1024 * 1024), '3.0 MB');
  assert.equal(formatUsd(0.1), '$0.1000');
  assert.equal(formatUsd(12.5), '$12.50');
  assert.equal(formatUsd(null), '-');
});
