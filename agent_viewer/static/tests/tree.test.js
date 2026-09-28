import { test } from 'node:test';
import assert from 'node:assert/strict';
import { agentLabel, flattenTree, moveSelection, nodeWarnings, warningText } from '../lib/tree.js';

const node = (agentId, children = [], extra = {}) => ({
  key: `claude:s-main:${agentId}`,
  agentId,
  children: children.map((c) => `claude:s-main:${c}`),
  warnings: [],
  ...extra,
});

// The expected tree of SPEC section 13.2.
const TREE = {
  sessionKey: 'claude:s-main',
  rootKey: 'claude:s-main:main',
  nodes: Object.fromEntries([
    node('main', ['a1111111111111111', 'a3333333333333333', 'a4444444444444444']),
    node('a1111111111111111', ['a2222222222222222']),
    node('a2222222222222222'),
    node('a3333333333333333'),
    node('a4444444444444444', [], { linkedBy: 'orphan', warnings: ['orphan'] }),
  ].map((n) => [n.key, n])),
};

const ids = (rows) => rows.map((r) => `${r.level}:${r.node.agentId}`);

test('flattenTree walks depth-first in children order with levels', () => {
  assert.deepEqual(ids(flattenTree(TREE)), [
    '0:main', '1:a1111111111111111', '2:a2222222222222222', '1:a3333333333333333', '1:a4444444444444444',
  ]);
  const rows = flattenTree(TREE);
  assert.equal(rows[0].hasChildren, true);
  assert.equal(rows[2].hasChildren, false);
});

test('flattenTree hides the descendants of collapsed nodes', () => {
  const rows = flattenTree(TREE, new Set(['claude:s-main:a1111111111111111']));
  assert.deepEqual(ids(rows), ['0:main', '1:a1111111111111111', '1:a3333333333333333', '1:a4444444444444444']);
  assert.equal(rows[1].collapsed, true);
});

test('flattenTree survives cycles, dangling children and detached nodes', () => {
  const broken = {
    rootKey: 'k:main',
    nodes: {
      'k:main': { agentId: 'main', children: ['k:a', 'k:missing'] },
      'k:a': { agentId: 'a', children: ['k:main'] },
      'k:loose': { agentId: 'loose', children: [] },
    },
  };
  const rows = flattenTree(broken);
  assert.deepEqual(rows.map((r) => r.key), ['k:main', 'k:a', 'k:loose']);
  assert.equal(rows[0].hasChildren, true);
  assert.deepEqual(flattenTree(null), []);
});

test('moveSelection steps through rows and clamps at both ends', () => {
  const rows = flattenTree(TREE);
  assert.equal(moveSelection(rows, 'claude:s-main:main', 1), 'claude:s-main:a1111111111111111');
  assert.equal(moveSelection(rows, 'claude:s-main:main', -1), 'claude:s-main:main');
  assert.equal(moveSelection(rows, 'claude:s-main:a4444444444444444', 1), 'claude:s-main:a4444444444444444');
  assert.equal(moveSelection(rows, 'unknown', 1), 'claude:s-main:main');
  assert.equal(moveSelection([], 'x', 1), null);
});

test('nodeWarnings adds implied warnings without duplicates', () => {
  assert.deepEqual(nodeWarnings(TREE.nodes['claude:s-main:a4444444444444444']), ['orphan']);
  assert.deepEqual(nodeWarnings({ missing: true, warnings: ['cycle'] }), ['cycle', 'missing']);
  assert.deepEqual(nodeWarnings(null), []);
  assert.match(warningText('parent_mismatch'), /sidecar/);
  assert.equal(warningText('something_new'), 'something_new');
});

test('agentLabel names main, shortens ids, and copes with unknown keys', () => {
  assert.equal(agentLabel(TREE, 'claude:s-main:main'), 'main');
  assert.equal(agentLabel(TREE, 'claude:s-main:a1111111111111111'), 'a1111111');
  assert.equal(agentLabel(TREE, 'claude:s-main:a9999999999999999'), 'a9999999');
});
