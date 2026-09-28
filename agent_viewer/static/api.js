// fetch and EventSource wrapper: the only module that talks to the server.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

const STREAM_EVENTS = [
  'session.upsert',
  'session.remove',
  'tree.update',
  'events.append',
  'agent.reset',
  'stats.update',
  'graph.update',
];
const RECONNECT_MS = 2000;

const enc = encodeURIComponent;

function queryString(params) {
  if (!params) return '';
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === '') continue;
    const values = Array.isArray(value) ? value : [value];
    for (const v of values) qs.append(key, String(v));
  }
  const s = qs.toString();
  return s ? `?${s}` : '';
}

async function getJSON(path, params) {
  let res;
  try {
    res = await fetch(path + queryString(params), { headers: { Accept: 'application/json' } });
  } catch {
    throw new ApiError(0, 'network', 'Cannot reach the agent-viewer server.');
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const err = body && body.error;
    throw new ApiError(res.status, (err && err.code) || `http_${res.status}`, (err && err.message) || `HTTP ${res.status}`);
  }
  if (body == null || typeof body !== 'object') {
    throw new ApiError(res.status, 'bad_json', 'The server response was not a JSON object.');
  }
  return body;
}

function agentPath(ref) {
  return `/api/agents/${enc(ref.source)}/${enc(ref.sessionId)}/${enc(ref.agentId)}`;
}

export const api = {
  health: () => getJSON('/api/health'),
  projects: () => getJSON('/api/projects'),
  sessions: (params, cursor) => getJSON('/api/sessions', { ...params, cursor }),
  session: (source, sessionId) => getJSON(`/api/sessions/${enc(source)}/${enc(sessionId)}`),
  tree: (source, sessionId) => getJSON(`/api/sessions/${enc(source)}/${enc(sessionId)}/tree`),
  graph: (source, sessionId) => getJSON(`/api/sessions/${enc(source)}/${enc(sessionId)}/graph`),
  agent: (ref) => getJSON(agentPath(ref)),
  events: (ref, { before, after, limit, kinds, includeMeta, toolName } = {}) =>
    getJSON(`${agentPath(ref)}/events`, {
      before,
      after,
      limit,
      kinds: kinds && kinds.length ? kinds.join(',') : null,
      includeMeta: includeMeta ? 'true' : null,
      toolName: toolName || null,
    }),
  eventFull: (ref, seq) => getJSON(`${agentPath(ref)}/events/${enc(seq)}`),
  stats: (ref, scope) => getJSON(`${agentPath(ref)}/stats`, { scope }),
};

/**
 * Open the SSE stream. `sessions` lists session keys whose agent-level events
 * are wanted. Handlers: onEvent(name, data), onState('connecting' | 'live' |
 * 'reconnecting'), onResync() after the stream comes back from an outage,
 * onHello() on every hello (after onResync). With `graph: true` the stream
 * also carries graph.update for the sessions.
 */
export function openStream(sessions, handlers, { graph = false } = {}) {
  const url = `/api/stream${queryString({ session: sessions, graph: graph ? '1' : null })}`;
  let source = null;
  let state = '';
  let wasDown = false;
  let retryTimer = null;
  let isClosed = false;

  const setState = (next) => {
    if (next === state) return;
    state = next;
    if (handlers.onState) handlers.onState(next);
  };

  const scheduleReconnect = (delay) => {
    if (isClosed || retryTimer) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    }, delay);
  };

  const onHello = () => {
    setState('live');
    if (wasDown && handlers.onResync) handlers.onResync();
    wasDown = false;
    if (handlers.onHello) handlers.onHello();
  };

  const onResyncEvent = () => {
    // The server overflowed our queue: drop the stream, refetch, reconnect.
    wasDown = true;
    source.close();
    setState('reconnecting');
    scheduleReconnect(0);
  };

  const onError = () => {
    if (isClosed) return;
    wasDown = true;
    setState('reconnecting');
    // EventSource retries by itself unless the response was fatal.
    if (source.readyState === EventSource.CLOSED) scheduleReconnect(RECONNECT_MS);
  };

  function connect() {
    if (isClosed) return;
    if (source) source.close();
    source = new EventSource(url);
    source.addEventListener('hello', onHello);
    source.addEventListener('resync', onResyncEvent);
    source.addEventListener('error', onError);
    for (const name of STREAM_EVENTS) {
      source.addEventListener(name, (msg) => {
        let data;
        try {
          data = JSON.parse(msg.data);
        } catch {
          return;
        }
        if (handlers.onEvent) handlers.onEvent(name, data);
      });
    }
  }

  setState('connecting');
  connect();

  return {
    close() {
      isClosed = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (source) source.close();
    },
  };
}
