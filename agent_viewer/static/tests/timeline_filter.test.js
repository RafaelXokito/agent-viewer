import { test } from 'node:test';
import assert from 'node:assert/strict';
import { matchesToolFilter } from '../lib/events.js';

test('matchesToolFilter keeps every event when no filter is set', () => {
  assert.equal(matchesToolFilter({ kind: 'text' }, null), true);
  assert.equal(matchesToolFilter({ kind: 'text' }, ''), true);
});

test('matchesToolFilter matches toolName exactly and case-sensitively', () => {
  assert.equal(matchesToolFilter({ kind: 'tool_call', toolName: 'Bash' }, 'Bash'), true);
  assert.equal(matchesToolFilter({ kind: 'tool_result', toolName: 'Bash' }, 'Bash'), true);
  assert.equal(matchesToolFilter({ kind: 'tool_call', toolName: 'bash' }, 'Bash'), false);
  assert.equal(matchesToolFilter({ kind: 'tool_call', toolName: 'Bash2' }, 'Bash'), false);
  assert.equal(matchesToolFilter({ kind: 'tool_call', toolName: 'Read' }, 'Bash'), false);
});

test('matchesToolFilter drops events without a toolName while filtered', () => {
  assert.equal(matchesToolFilter({ kind: 'text', toolName: null }, 'Bash'), false);
  assert.equal(matchesToolFilter({ kind: 'meta' }, 'Bash'), false);
  assert.equal(matchesToolFilter(null, 'Bash'), false);
});
