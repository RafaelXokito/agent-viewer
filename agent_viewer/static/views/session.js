// Session view: header with the Tree | Graph toggle, then either the tree
// layout (agent tree, timeline, stats) or the graph layout (canvas and
// drawer). This module is the only writer of the URL for a session route.

import { api } from '../api.js';
import { h, replace } from '../dom.js';
import { durationBetween, formatDateTime, formatDuration, formatInt, formatRelative, projectLabel } from '../lib/format.js';
import {
  buildSessionHash, LAYOUTS, makeAgentKey, makeSessionKey, normalizeViewState, parseAgentKey,
} from '../lib/state.js';
import { createDrawer } from './drawer.js';
import { createGraphView } from './graph.js';
import { createStatsView } from './stats.js';
import { createTimelineView } from './timeline.js';
import { createTreeView } from './tree.js';
import { errorBox, loading, sourceBadge, statusChip } from './widgets.js';

const LAYOUT_LABELS = Object.freeze({ tree: 'Tree', graph: 'Graph' });

function splitSessionKey(value) {
  const idx = typeof value === 'string' ? value.indexOf(':') : -1;
  if (idx <= 0 || idx === value.length - 1) return null;
  return { source: value.slice(0, idx), sessionId: value.slice(idx + 1) };
}

function sessionLink(sessionKeyValue, label, layout) {
  const parsed = splitSessionKey(sessionKeyValue);
  if (!parsed) return null;
  const href = buildSessionHash(parsed.source, parsed.sessionId, null, { layout });
  return h('a', { href, title: sessionKeyValue, text: label });
}

function meta(label, value, title, isMono) {
  return h('div', null, h('dt', { text: label }), h('dd', { class: isMono && 'mono', title: title || null, text: value }));
}

export function createSessionView(root, route) {
  const { source, sessionId } = route;
  const sessionKey = makeSessionKey(source, sessionId);
  let session = null;
  let treeData = null;
  let view = normalizeViewState(route); // {layout, tools, drawer, tool}
  let routeAgentId = route.agentId || 'main';
  let selectedAgentId = null;
  let isLoaded = false;
  let destroyed = false;
  let body = null;

  const crumbs = h('div', { class: 'crumbs' }, h('a', { href: '#/', text: '← Sessions' }));
  const titleMain = h('div', { class: 'session-title' }, loading('Loading session...'));
  const layoutButtons = LAYOUTS.map((layout) => h('button', {
    type: 'button',
    class: 'btn btn-small layout-btn',
    dataset: { layout },
    text: LAYOUT_LABELS[layout],
    onClick: () => switchLayout(layout),
  }));
  const layoutToggle = h('div', { class: 'layout-toggle', role: 'group', 'aria-label': 'Layout' }, layoutButtons);
  const metaList = h('dl', { class: 'session-meta' });
  const header = h('header', { class: 'session-header' },
    crumbs, h('div', { class: 'session-title-row' }, titleMain, layoutToggle), metaList);
  const bodyEl = h('div', { class: 'session-body' });
  replace(root, h('div', { class: 'session-view' }, header, bodyEl));

  // ---- URL -----------------------------------------------------------------

  function go(next, { replace: isReplace = false } = {}) {
    const agentId = next.agentId || selectedAgentId || routeAgentId;
    const hash = buildSessionHash(source, sessionId, agentId, next);
    if (isReplace) location.replace(hash);
    else location.hash = hash;
  }

  function withView(changes) {
    return { ...view, ...changes };
  }

  /** Drop invalid or inapplicable query values from the URL (G-FR-3b). */
  function canonicalize() {
    if (!location.hash.includes('?')) return;
    const canonical = buildSessionHash(source, sessionId, routeAgentId, view);
    if (location.hash !== canonical) location.replace(canonical);
  }

  function switchLayout(layout) {
    if (layout === view.layout) return;
    go(withView({ layout }), { replace: true });
  }

  // ---- header ----------------------------------------------------------------

  function renderToggle() {
    for (const btn of layoutButtons) {
      const isActive = btn.dataset.layout === view.layout;
      btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
      btn.classList.toggle('active', isActive);
    }
  }

  function renderHeader() {
    if (!session) return;
    const s = session;
    const now = Date.now();
    document.title = `${s.title || s.sessionId} - agent-viewer`;
    const linkLayout = view.layout === 'graph' ? 'graph' : 'tree';
    replace(titleMain,
      sourceBadge(s.source),
      h('h1', { title: s.title || '', text: s.title || s.firstPrompt || s.sessionId }),
      statusChip(s.status),
      s.indexed === false && h('span', { class: 'chip', text: 'indexing' }));
    replace(metaList,
      meta('Project', projectLabel(s.cwd, s.project), s.cwd),
      meta('Branch', s.gitBranch || '-', s.gitBranch, true),
      meta('Started', formatDateTime(s.startedAt), s.startedAt),
      meta('Last activity', formatRelative(s.lastActivityAt, now), s.lastActivityAt),
      meta('Duration', formatDuration(durationBetween(s.startedAt, s.lastActivityAt))),
      meta('Agents', `${formatInt(s.agentCount)} (${formatInt(s.runningAgents || 0)} running)`),
      s.continuedFrom && h('div', null, h('dt', { text: 'Continued from' }), h('dd', null, sessionLink(s.continuedFrom, 'previous session', linkLayout))),
      s.continuedIn && h('div', null, h('dt', { text: 'Continued in' }), h('dd', null, sessionLink(s.continuedIn, 'next session', linkLayout))),
      s.prUrl && meta('PR', s.prUrl, s.prUrl, true));
    if (body && body.setSessionTitle) body.setSessionTitle(s.title || s.firstPrompt || null);
  }

  // ---- shared helpers --------------------------------------------------------

  function agentRef(agentId) {
    return { source, sessionId, agentId };
  }

  function agentKeyOf(agentId) {
    return makeAgentKey(source, sessionId, agentId || 'main');
  }

  function nodeOf(key) {
    return (treeData && treeData.nodes && treeData.nodes[key]) || null;
  }

  function ownAgentId(key) {
    const parsed = parseAgentKey(key);
    if (!parsed || parsed.source !== source || parsed.sessionId !== sessionId) return null;
    return parsed.agentId;
  }

  // ---- tree layout -------------------------------------------------------------

  function mountTreeLayout() {
    const treePanel = h('aside', { class: 'panel tree-panel', 'aria-label': 'Agent tree' });
    const timelinePanel = h('section', { class: 'panel timeline-panel', 'aria-label': 'Timeline' });
    const statsPanel = h('aside', { class: 'panel stats-panel', 'aria-label': 'Stats' });
    replace(bodyEl, h('div', { class: 'session-grid' }, treePanel, timelinePanel, statsPanel));

    const tree = createTreeView(treePanel, {
      onSelect: (key, { replace: isKeyboard }) => {
        const agentId = ownAgentId(key);
        if (agentId) go(withView({ agentId }), { replace: isKeyboard });
      },
    });
    tree.setSessionId(sessionId);
    if (treeData) tree.setTree(treeData);
    const timeline = createTimelineView(timelinePanel);
    const stats = createStatsView(statsPanel);
    let loadedAgentId = null;
    let infoToken = 0;

    async function loadAgentInfo(agentId) {
      const myToken = ++infoToken;
      try {
        const info = await api.agent(agentRef(agentId));
        if (!destroyed && myToken === infoToken) timeline.setInfo(info);
      } catch {
        // The tree node already gives the essentials; the header just lacks the file path.
      }
    }

    return {
      layout: 'tree',
      select(agentId) {
        const key = agentKeyOf(agentId);
        tree.setSelected(key);
        if (agentId === loadedAgentId) return;
        loadedAgentId = agentId;
        timeline.load(agentRef(agentId), key, tree.node(key));
        stats.load(agentRef(agentId), key, sessionKey, key === tree.rootKey() || agentId === 'main');
        loadAgentInfo(agentId);
      },
      setTree(next) {
        tree.setTree(next);
        if (selectedAgentId) tree.setSelected(agentKeyOf(selectedAgentId));
        timeline.setInfo(tree.node(timeline.key()));
      },
      onEvent(name, data) {
        if (name === 'events.append') timeline.onAppend(data);
        else if (name === 'agent.reset') timeline.onReset(data);
        else if (name === 'stats.update') stats.onUpdate(data);
      },
      resync() {
        timeline.resync();
        stats.resync();
      },
      destroy() {
        infoToken += 1;
        stats.destroy();
      },
    };
  }

  // ---- graph layout -------------------------------------------------------------

  function onGraphSelect(agentKey, { open = false, replace: isReplace = false, toolName = null } = {}) {
    const agentId = ownAgentId(agentKey);
    if (!agentId) return;
    const isSameAgent = agentId === selectedAgentId;
    let drawer = view.drawer;
    let tool = isSameAgent ? view.tool : null;
    if (open) {
      drawer = toolName ? 'timeline' : view.drawer || 'timeline';
      tool = toolName || null;
    }
    go(withView({ agentId, drawer, tool }), { replace: isReplace });
  }

  function onNavigateSession(targetKey) {
    const parsed = splitSessionKey(targetKey);
    if (parsed) location.hash = buildSessionHash(parsed.source, parsed.sessionId, null, { layout: 'graph' });
  }

  function drawerAgentHash(parsed) {
    return buildSessionHash(parsed.source, parsed.sessionId, parsed.agentId,
      { layout: 'graph', tools: view.tools, drawer: 'timeline' });
  }

  function mountGraphLayout() {
    const canvasHost = h('div', { class: 'graph-host' });
    const drawerEl = h('aside', { class: 'drawer', 'aria-label': 'Agent details' });
    let isDead = false;
    let graphToken = 0;

    const closeDrawer = () => {
      go(withView({ drawer: null, tool: null }), { replace: true });
      graphView.focusSelected();
    };
    const onKeydown = (e) => {
      if (e.key !== 'Escape' || e.defaultPrevented || !view.drawer) return;
      e.preventDefault();
      closeDrawer();
    };
    replace(bodyEl, h('div', { class: 'graph-row', onKeydown }, canvasHost, drawerEl));

    const graphView = createGraphView(canvasHost, {
      toolsMode: view.tools,
      onSelect: onGraphSelect,
      onNavigateSession,
      onToolsModeChange: (mode) => go(withView({ tools: mode }), { replace: true }),
      onRequestTreeView: () => switchLayout('tree'),
      onRetry: () => fetchGraph(),
    });
    const drawer = createDrawer(drawerEl, {
      onClose: closeDrawer,
      onOpenInTree: () => switchLayout('tree'),
      onTabChange: (tab) => go(withView({ drawer: tab }), { replace: true }),
      onClearFilter: () => go(withView({ tool: null }), { replace: true }),
      agentHash: drawerAgentHash,
    });

    async function fetchGraph() {
      const myToken = ++graphToken;
      try {
        const graph = await api.graph(source, sessionId);
        if (isDead || myToken !== graphToken) return;
        graphView.setGraph(graph, { reset: true });
        if (selectedAgentId) graphView.setSelected(agentKeyOf(selectedAgentId));
      } catch (err) {
        if (isDead || myToken !== graphToken) return;
        graphView.showError(err);
      }
    }

    function applyDrawer() {
      if (!view.drawer || !selectedAgentId) {
        drawer.close();
        return;
      }
      const key = agentKeyOf(selectedAgentId);
      drawer.open(agentRef(selectedAgentId), key, nodeOf(key), { tab: view.drawer, toolName: view.tool });
    }

    function onGraphUpdate(data) {
      if (data.sessionKey !== sessionKey) return;
      if (data.overflow || !data.graph) {
        fetchGraph();
        return;
      }
      if (Number.isInteger(data.rev) && data.rev > graphView.rev()) graphView.setGraph(data.graph, { reset: false });
    }

    fetchGraph();

    return {
      layout: 'graph',
      select(agentId) {
        graphView.setSelected(agentKeyOf(agentId));
        applyDrawer();
      },
      setToolsMode(mode) {
        graphView.setToolsMode(mode);
      },
      setSessionTitle(title) {
        graphView.setSessionTitle(title);
      },
      setTree() {
        if (drawer.isOpen()) applyDrawer();
      },
      onEvent(name, data) {
        if (name === 'graph.update') onGraphUpdate(data);
        else drawer.onEvent(name, data);
      },
      onHello() {
        fetchGraph();
      },
      resync() {
        drawer.resync();
      },
      destroy() {
        isDead = true;
        drawer.destroy();
        graphView.destroy();
      },
    };
  }

  // ---- routing ---------------------------------------------------------------------

  function mountBody() {
    if (body) body.destroy();
    body = view.layout === 'graph' ? mountGraphLayout() : mountTreeLayout();
    renderToggle();
  }

  function selectAgent(agentId) {
    selectedAgentId = agentId || 'main';
    body.select(selectedAgentId);
  }

  function setRoute(r) {
    const next = normalizeViewState(r);
    const isLayoutChange = next.layout !== view.layout;
    const isToolsChange = next.tools !== view.tools;
    view = next;
    routeAgentId = r.agentId || 'main';
    canonicalize();
    if (isLayoutChange) {
      mountBody();
      renderHeader();
    } else if (isToolsChange && body.setToolsMode) {
      body.setToolsMode(view.tools);
    }
    if (isLoaded) selectAgent(routeAgentId);
  }

  async function loadSession() {
    try {
      const detail = await api.session(source, sessionId);
      if (destroyed) return;
      session = detail;
      renderHeader();
      if (detail.tree) {
        treeData = detail.tree;
        body.setTree(treeData);
      }
    } catch (err) {
      if (destroyed) return;
      replace(titleMain, errorBox(err));
    }
  }

  canonicalize();
  mountBody();
  const ticker = setInterval(renderHeader, 10000);

  loadSession().then(() => {
    if (destroyed) return;
    isLoaded = true;
    selectAgent(routeAgentId);
  });

  return {
    sessionKey,
    setRoute,
    onEvent(name, data) {
      if (!data) return;
      if (name === 'session.upsert' && data.key === sessionKey) {
        session = data;
        renderHeader();
      } else if (name === 'tree.update' && data.sessionKey === sessionKey && data.tree) {
        treeData = data.tree;
        body.setTree(treeData);
      } else {
        body.onEvent(name, data);
      }
    },
    onHello() {
      if (body.onHello) body.onHello();
    },
    resync() {
      loadSession();
      body.resync();
    },
    destroy() {
      destroyed = true;
      clearInterval(ticker);
      body.destroy();
    },
  };
}
