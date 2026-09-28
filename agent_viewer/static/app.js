// Router and bootstrapping: one view mounted at a time, one SSE stream.

import { openStream } from './api.js';
import { h, replace } from './dom.js';
import { parseHash } from './lib/state.js';
import { createSessionView } from './views/session.js';
import { createSessionsView } from './views/sessions.js';

const THEMES = ['auto', 'light', 'dark'];
const THEME_KEY = 'agent-viewer.theme';

const main = document.getElementById('main');
const connection = document.getElementById('connection');
const themeButton = document.getElementById('theme-toggle');

let current = null; // {kind, key, view}
let stream = null;
let streamKey = null;

const CONNECTION_TEXT = Object.freeze({
  connecting: 'connecting',
  live: 'live',
  reconnecting: 'reconnecting...',
});

function setConnection(state) {
  connection.dataset.state = state;
  connection.textContent = CONNECTION_TEXT[state] || state;
  connection.title = state === 'live' ? 'Receiving live updates' : 'Live updates are not flowing; the view resyncs when they return';
}

function ensureStream(sessions, { graph = false } = {}) {
  const key = `${sessions.join('|')}${graph ? '#graph' : ''}`;
  if (stream && key === streamKey) return;
  if (stream) stream.close();
  streamKey = key;
  stream = openStream(sessions, {
    onState: setConnection,
    onEvent: (name, data) => {
      if (current && current.view.onEvent) current.view.onEvent(name, data);
    },
    onResync: () => {
      if (current && current.view.resync) current.view.resync();
    },
    onHello: () => {
      if (current && current.view.onHello) current.view.onHello();
    },
  }, { graph });
}

function mount(kind, key, factory) {
  if (current && current.view.destroy) current.view.destroy();
  current = { kind, key, view: factory() };
}

function route() {
  const r = parseHash(location.hash);
  if (r.view === 'session') {
    const key = `${r.source}:${r.sessionId}`;
    // Agent, layout, tools, drawer and tool changes go to the mounted view.
    if (current && current.kind === 'session' && current.key === key) current.view.setRoute(r);
    else mount('session', key, () => createSessionView(main, r));
    ensureStream([key], { graph: r.layout === 'graph' });
    return;
  }
  if (current && current.kind === 'list') {
    current.view.setFilters(r.filters);
    return;
  }
  mount('list', 'list', () => createSessionsView(main, r.filters));
  ensureStream([]);
}

// ---- theme -----------------------------------------------------------------

function readTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    return THEMES.includes(saved) ? saved : 'auto';
  } catch {
    return 'auto';
  }
}

function applyTheme(theme) {
  if (theme === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  themeButton.textContent = `theme: ${theme}`;
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Storage may be unavailable; the theme still applies for this page.
  }
}

themeButton.addEventListener('click', () => {
  const next = THEMES[(THEMES.indexOf(readTheme()) + 1) % THEMES.length];
  applyTheme(next);
});

// ---- start -------------------------------------------------------------------

window.addEventListener('hashchange', route);
window.addEventListener('error', (e) => {
  replace(document.getElementById('fatal'), h('p', { class: 'error-box', role: 'alert', text: `Unexpected error: ${e.message}` }));
});
applyTheme(readTheme());
setConnection('connecting');
route();
