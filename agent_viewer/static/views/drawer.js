// Graph layout drawer (FEATURE-graph-canvas G-FR-29 to G-FR-32): hosts the
// existing timeline and stats views for one agent, with tabs, the tool filter
// chip, close and "Open in tree view". The caller owns the URL: every user
// action is reported through a callback and comes back as an open() call.

import { api } from '../api.js';
import { h, replace } from '../dom.js';
import { makeSessionKey } from '../lib/state.js';
import { createStatsView } from './stats.js';
import { createTimelineView } from './timeline.js';
import { idChip, statusChip } from './widgets.js';

const TABS = Object.freeze([
  { id: 'timeline', label: 'Timeline' },
  { id: 'stats', label: 'Stats' },
]);

function agentTypeLabel(ref, node) {
  if (ref.agentId === 'main') return 'main';
  return (node && node.agentType) || 'unknown type';
}

function isRootAgent(ref, node) {
  return ref.agentId === 'main' || Boolean(node && node.parentKey == null);
}

/**
 * `container` is the drawer element itself; it is hidden while closed.
 * `open()` is idempotent: the same agent and filter only refresh the header.
 */
export function createDrawer(container, { onClose, onOpenInTree, onTabChange, onClearFilter, agentHash } = {}) {
  let state = null; // {ref, key, node, tab, toolName}
  let timeline = null;
  let timelineLoaded = null; // {key, toolName}
  let stats = null;
  let statsKey = null;
  let infoToken = 0;
  let isOpen = false;

  const header = h('div', { class: 'drawer-header' });
  const tabBar = h('div', { class: 'drawer-tabs', role: 'tablist', 'aria-label': 'Agent details' });
  const filterBar = h('div', { class: 'drawer-filter', hidden: true });
  const timelinePane = h('section', { class: 'panel drawer-pane', role: 'tabpanel', 'aria-label': 'Timeline' });
  const statsPane = h('section', { class: 'panel drawer-pane', role: 'tabpanel', 'aria-label': 'Stats', hidden: true });
  replace(container, header, tabBar, filterBar, h('div', { class: 'drawer-body' }, timelinePane, statsPane));
  container.hidden = true;

  function renderHeader() {
    const { ref, node } = state;
    replace(header,
      h('div', { class: 'drawer-title' },
        h('h2', { text: agentTypeLabel(ref, node) }),
        node && node.status && statusChip(node.status),
        ref.agentId !== 'main' && idChip(ref.agentId, { label: 'Copy agent id' })),
      h('div', { class: 'drawer-actions' },
        h('button', { type: 'button', class: 'btn btn-small', text: 'Open in tree view', onClick: () => onOpenInTree && onOpenInTree() }),
        h('button', {
          type: 'button', class: 'btn btn-small drawer-close', 'aria-label': 'Close drawer', title: 'Close (Escape)', text: '×',
          onClick: () => onClose && onClose(),
        })));
  }

  function onTabKeydown(e) {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const next = TABS.find((t) => t.id !== state.tab);
    if (onTabChange) onTabChange(next.id);
    const btn = tabBar.querySelector(`[data-tab="${next.id}"]`);
    if (btn) btn.focus();
  }

  function renderTabs() {
    replace(tabBar, TABS.map((t) => h('button', {
      type: 'button',
      class: ['drawer-tab', t.id === state.tab && 'active'],
      role: 'tab',
      'aria-selected': t.id === state.tab ? 'true' : 'false',
      tabindex: t.id === state.tab ? 0 : -1,
      dataset: { tab: t.id },
      text: t.label,
      onKeydown: onTabKeydown,
      onClick: () => {
        if (t.id !== state.tab && onTabChange) onTabChange(t.id);
      },
    })));
  }

  function renderFilter() {
    const name = state.tab === 'timeline' ? state.toolName : null;
    filterBar.hidden = !name;
    if (!name) {
      replace(filterBar);
      return;
    }
    const clear = () => onClearFilter && onClearFilter();
    replace(filterBar, h('span', {
      class: 'filter-chip',
      tabindex: 0,
      title: 'Backspace or Delete clears the filter',
      onKeydown: (e) => {
        if (e.key !== 'Backspace' && e.key !== 'Delete') return;
        e.preventDefault();
        clear();
      },
    },
    h('span', { text: `filtered: ${name}` }),
    h('button', { type: 'button', class: 'filter-clear', 'aria-label': `Clear tool filter ${name}`, text: '×', onClick: clear })));
  }

  async function loadAgentInfo(ref, key) {
    const myToken = ++infoToken;
    try {
      const info = await api.agent(ref);
      if (myToken === infoToken && timeline && timeline.key() === key) timeline.setInfo(info);
    } catch {
      // The tree node already gives the essentials; the header just lacks the file path.
    }
  }

  function showTimeline() {
    const { ref, key, node, toolName } = state;
    if (!timeline) timeline = createTimelineView(timelinePane, agentHash ? { agentHash } : {});
    const isLoaded = timelineLoaded && timelineLoaded.key === key && timelineLoaded.toolName === toolName;
    if (isLoaded) {
      timeline.setInfo(node);
      return;
    }
    timelineLoaded = { key, toolName };
    timeline.load(ref, key, node, { toolName });
    loadAgentInfo(ref, key);
  }

  function showStats() {
    const { ref, key, node } = state;
    if (!stats) stats = createStatsView(statsPane);
    if (statsKey === key) return;
    statsKey = key;
    stats.load(ref, key, makeSessionKey(ref.source, ref.sessionId), isRootAgent(ref, node));
  }

  return {
    open(agentRef, agentKey, treeNode, { tab = 'timeline', toolName = null } = {}) {
      const activeTab = tab === 'stats' ? 'stats' : 'timeline';
      state = { ref: agentRef, key: agentKey, node: treeNode || null, tab: activeTab, toolName: activeTab === 'timeline' ? toolName || null : null };
      isOpen = true;
      container.hidden = false;
      renderHeader();
      renderTabs();
      renderFilter();
      timelinePane.hidden = activeTab !== 'timeline';
      statsPane.hidden = activeTab !== 'stats';
      if (activeTab === 'timeline') showTimeline();
      else showStats();
    },
    close() {
      isOpen = false;
      container.hidden = true;
    },
    isOpen() {
      return isOpen;
    },
    onEvent(name, data) {
      if (name === 'events.append' && timeline) timeline.onAppend(data);
      else if (name === 'agent.reset' && timeline) timeline.onReset(data);
      else if (name === 'stats.update' && stats) stats.onUpdate(data);
    },
    resync() {
      if (timeline && timelineLoaded) timeline.resync();
      if (stats && statsKey) stats.resync();
    },
    destroy() {
      infoToken += 1;
      if (stats) stats.destroy();
      replace(container);
    },
  };
}
