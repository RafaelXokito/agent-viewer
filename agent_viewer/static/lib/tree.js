// Pure tree helpers: flatten the flat node map of section 9.4 into display
// rows, move the keyboard selection, and collect node warnings.

/**
 * Depth-first rows in `children` order, skipping the descendants of
 * collapsed nodes. Children missing from the map are skipped, cycles are cut,
 * and nodes unreachable from the root are appended so nothing is hidden.
 */
export function flattenTree(tree, collapsed = new Set()) {
  if (!tree || !tree.nodes) return [];
  const nodes = tree.nodes;
  const rows = [];
  const seen = new Set();

  const visit = (key, level, isVisible) => {
    if (seen.has(key)) return;
    const node = nodes[key];
    if (!node) return;
    seen.add(key);
    const childKeys = (node.children || []).filter((k) => nodes[k]);
    const isCollapsed = collapsed.has(key);
    if (isVisible) {
      rows.push({ key, node, level, hasChildren: childKeys.length > 0, collapsed: isCollapsed });
    }
    for (const child of childKeys) visit(child, level + 1, isVisible && !isCollapsed);
  };

  if (tree.rootKey && nodes[tree.rootKey]) visit(tree.rootKey, 0, true);
  const detached = Object.keys(nodes).filter((k) => !seen.has(k)).sort();
  for (const key of detached) visit(key, 1, true);
  return rows;
}

/** Key of the row `delta` places away from `key`, clamped to the ends. */
export function moveSelection(rows, key, delta) {
  if (rows.length === 0) return null;
  const index = rows.findIndex((r) => r.key === key);
  if (index === -1) return rows[0].key;
  const next = Math.min(rows.length - 1, Math.max(0, index + delta));
  return rows[next].key;
}

/** Warnings from the server plus the ones implied by node fields. */
export function nodeWarnings(node) {
  if (!node) return [];
  const out = new Set(Array.isArray(node.warnings) ? node.warnings : []);
  if (node.missing) out.add('missing');
  if (node.linkedBy === 'orphan') out.add('orphan');
  return [...out];
}

const WARNING_TEXT = Object.freeze({
  orphan: 'No spawn evidence was found; attached to the session root.',
  parent_mismatch: 'The sidecar and the transcript disagree about the parent; the sidecar wins.',
  cycle: 'A parent cycle was detected and broken at this node.',
  missing: 'The transcript file for this agent does not exist or has vanished.',
  depth_mismatch: 'The recorded spawn depth differs from the reconstructed depth.',
});

export function warningText(warning) {
  return WARNING_TEXT[warning] || warning;
}

/** Display label of an agent: "main" or the short agentId. */
export function agentLabel(tree, key) {
  const node = tree && tree.nodes ? tree.nodes[key] : null;
  if (!node) {
    const tail = typeof key === 'string' ? key.split(':').pop() : '';
    return tail ? tail.slice(0, 8) : 'unknown';
  }
  return node.agentId === 'main' ? 'main' : String(node.agentId).slice(0, 8);
}
