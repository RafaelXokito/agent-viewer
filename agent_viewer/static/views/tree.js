// Agent tree panel (section 8.2): collapsible, keyboard navigable, with
// agentIds, statuses, warnings and resume edges.

import { h, replace, setVar } from '../dom.js';
import { formatClock, formatCompact, formatInt, truncate } from '../lib/format.js';
import { agentLabel, flattenTree, moveSelection, nodeWarnings, warningText } from '../lib/tree.js';
import { copyButton, emptyState, idChip, statusDot } from './widgets.js';

const DESCRIPTION_MAX = 60;

/**
 * `onSelect(key, {replace})` is called when the user picks a node; the
 * caller updates the URL, which calls back into `setSelected`.
 */
export function createTreeView(container, { onSelect }) {
  let tree = null;
  let selectedKey = null;
  let sessionId = null;
  const collapsed = new Set();

  const header = h('div', { class: 'panel-header' });
  const list = h('div', {
    class: 'tree',
    role: 'tree',
    tabindex: 0,
    'aria-label': 'Agent tree',
    onKeydown: onKeydown,
  });
  replace(container, header, h('div', { class: 'panel-body' }, list));

  function renderHeader() {
    const count = tree && tree.nodes ? Object.keys(tree.nodes).length : 0;
    replace(header,
      h('h2', { text: `Agents (${count})` }),
      sessionId && h('div', { class: 'session-id' },
        h('span', { class: 'muted', text: 'session' }),
        h('code', { class: 'mono', title: sessionId, text: sessionId }),
        copyButton(sessionId, 'Copy session id')));
  }

  function toggle(key) {
    if (collapsed.has(key)) collapsed.delete(key);
    else collapsed.add(key);
    render();
  }

  function renderResumes(node) {
    if (!node.resumes || node.resumes.length === 0) return null;
    return h('ul', { class: 'resumes' }, node.resumes.map((r) =>
      h('li', { text: `resumed by ${agentLabel(tree, r.byAgentKey)} at ${formatClock(r.timestamp)}`, title: r.timestamp || '' })));
  }

  function renderRow(row) {
    const { key, node, level, hasChildren } = row;
    const isSelected = key === selectedKey;
    const warnings = nodeWarnings(node);
    const models = (node.models || []).join(', ');
    const resumeCount = (node.resumes || []).length;
    const typeLabel = node.agentId === 'main' ? 'main' : node.agentType || 'unknown type';

    const toggler = hasChildren
      ? h('button', {
        type: 'button',
        class: 'twisty',
        tabindex: -1,
        'aria-label': row.collapsed ? 'Expand' : 'Collapse',
        text: row.collapsed ? '▸' : '▾',
        onClick: (e) => {
          e.stopPropagation();
          toggle(key);
        },
      })
      : h('span', { class: 'twisty twisty-leaf', 'aria-hidden': 'true' });

    const item = h('div', {
      class: ['tree-node', isSelected && 'selected', node.missing && 'is-missing'],
      role: 'treeitem',
      'aria-level': level + 1,
      'aria-selected': isSelected ? 'true' : 'false',
      'aria-expanded': hasChildren ? (row.collapsed ? 'false' : 'true') : null,
      dataset: { key },
      onClick: () => select(key, false),
    },
    h('div', { class: 'node-line' },
      toggler,
      statusDot(node.status),
      h('span', { class: 'node-type', text: typeLabel }),
      node.agentId !== 'main' && idChip(node.agentId, { label: 'Copy agent id' }),
      resumeCount > 0 && h('span', { class: 'badge badge-resume', title: `${resumeCount} resume(s)`, text: `↻${resumeCount}` }),
      node.isFork && h('span', { class: 'badge', text: 'fork' }),
      node.forkedSkill && h('span', { class: 'badge', title: 'forked skill', text: node.forkedSkill }),
      warnings.map((w) => h('span', { class: 'warn', title: `${w}: ${warningText(w)}`, 'aria-label': `warning ${w}`, text: '⚠' }))),
    (node.description || node.name) && h('div', {
      class: 'node-desc', title: node.description || node.name, text: truncate(node.description || node.name, DESCRIPTION_MAX),
    }),
    h('div', { class: 'node-meta' },
      models && h('span', { class: 'mono', title: models, text: truncate(models, 40) }),
      h('span', { title: `${formatInt(node.tokensTotal)} tokens`, text: `${formatCompact(node.tokensTotal)} tok` }),
      h('span', { text: `${formatInt(node.eventCount)} ev` }),
      node.linkedBy && node.linkedBy !== 'root' && h('span', { class: 'muted', title: 'how the parent was found', text: node.linkedBy })),
    renderResumes(node));
    setVar(item, '--level', level);
    return item;
  }

  function render() {
    renderHeader();
    if (!tree || !tree.nodes) {
      replace(list, emptyState('No agents yet.'));
      return;
    }
    const rows = flattenTree(tree, collapsed);
    replace(list, rows.map(renderRow));
  }

  function select(key, isKeyboard) {
    if (!key || !tree || !tree.nodes[key]) return;
    if (onSelect) onSelect(key, { replace: isKeyboard });
  }

  function onKeydown(e) {
    if (!tree) return;
    const rows = flattenTree(tree, collapsed);
    const current = selectedKey;
    const node = current ? tree.nodes[current] : null;
    const hasKids = node && (node.children || []).some((k) => tree.nodes[k]);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      select(moveSelection(rows, current, e.key === 'ArrowDown' ? 1 : -1), true);
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      if (hasKids && !collapsed.has(current)) toggle(current);
      else if (node && node.parentKey) select(node.parentKey, true);
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      if (hasKids && collapsed.has(current)) toggle(current);
    }
  }

  function scrollSelectedIntoView() {
    const el = list.querySelector('.tree-node.selected');
    if (el) el.scrollIntoView({ block: 'nearest' });
  }

  return {
    setSessionId(id) {
      sessionId = id;
      renderHeader();
    },
    setTree(next) {
      tree = next;
      render();
    },
    setSelected(key) {
      selectedKey = key;
      // Reveal the selection: expand every collapsed ancestor.
      let cursor = tree && tree.nodes[key] ? tree.nodes[key].parentKey : null;
      const guard = new Set();
      while (cursor && !guard.has(cursor)) {
        guard.add(cursor);
        collapsed.delete(cursor);
        cursor = tree.nodes[cursor] ? tree.nodes[cursor].parentKey : null;
      }
      render();
      scrollSelectedIntoView();
    },
    node(key) {
      return tree && tree.nodes ? tree.nodes[key] || null : null;
    },
    rootKey() {
      return tree ? tree.rootKey : null;
    },
  };
}
