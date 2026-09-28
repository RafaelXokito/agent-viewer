"""Server-Sent Events: client registry, bounded queues, framing, coalescing (SPEC 7.4)."""
import json
import queue
import threading
import time
from datetime import datetime, timezone

VERSION = "1"
RETRY_MS = 2000
HEARTBEAT_S = 15.0
CLIENT_QUEUE_MAX = 1000
SSE_COALESCE = 0.25
STATS_MIN_INTERVAL = 2.0
MAX_INLINE_EVENTS = 100
GRAPH_MIN_INTERVAL = 1.0
MAX_INLINE_GRAPH_BYTES = 512 * 1024
GRAPH_EVENT = "graph.update"
HEARTBEAT = b": hb\n\n"

# Events every client receives; all others are scoped to a session key.
LIST_EVENTS = frozenset(("hello", "session.upsert", "session.remove", "resync"))

CLOSE = object()      # sentinel: server shutting down
OVERFLOW = object()   # sentinel: client fell behind, send resync and close


def frame(event, data, event_id=None, retry=None):
    """One SSE message as bytes. `data` is serialized as a single JSON line."""
    lines = []
    if event_id is not None:
        lines.append("id: %d" % event_id)
    if retry is not None:
        lines.append("retry: %d" % retry)
    lines.append("event: %s" % event)
    lines.append("data: %s" % json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


class Client:
    def __init__(self, sessions, maxsize, graph=False):
        self.sessions = frozenset(sessions)
        self.graph = bool(graph)  # opted in to graph.update with `graph=1`
        self.queue = queue.Queue(maxsize=maxsize + 1)  # +1 keeps room for a sentinel
        self.maxsize = maxsize
        self.overflowed = False
        self._lock = threading.Lock()

    def wants(self, event, session_key):
        if event == GRAPH_EVENT and not self.graph:
            return False
        return event in LIST_EVENTS or session_key in self.sessions

    def offer(self, message):
        with self._lock:
            if self.overflowed:
                return
            if self.queue.qsize() >= self.maxsize:
                self.overflowed = True
                self.queue.put_nowait(OVERFLOW)
                return
            self.queue.put_nowait(message)

    def close(self):
        with self._lock:
            try:
                self.queue.put_nowait(CLOSE)
            except queue.Full:
                pass

    def next(self, timeout=HEARTBEAT_S):
        """Next framed message, None on heartbeat timeout, or a sentinel."""
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None


class Hub:
    """Fan-out of published messages to subscribed clients, with a shared id counter."""

    def __init__(self, maxsize=CLIENT_QUEUE_MAX, clock=time.time):
        self.maxsize = maxsize
        self.clock = clock
        self._lock = threading.Lock()
        self._clients = set()
        self._next_id = 0
        self._closed = False

    def _id(self):
        self._next_id += 1
        return self._next_id

    def subscribe(self, sessions=(), graph=False):
        client = Client(sessions, self.maxsize, graph)
        with self._lock:
            if self._closed:
                client.close()
            self._clients.add(client)
        return client

    def unsubscribe(self, client):
        with self._lock:
            self._clients.discard(client)

    def hello(self):
        with self._lock:
            event_id = self._id()
        data = {"serverTime": iso_now(self.clock()), "version": VERSION}
        return frame("hello", data, event_id, retry=RETRY_MS)

    def publish(self, event, data, session_key=None):
        with self._lock:
            targets = [c for c in self._clients if c.wants(event, session_key)]
            if not targets:
                return
            message = frame(event, data, self._id())
        for client in targets:
            client.offer(message)

    def watched_sessions(self):
        """Session keys at least one connected client has asked for."""
        with self._lock:
            return set().union(*(c.sessions for c in self._clients))

    def watched_graph_sessions(self):
        """Session keys at least one client with `graph=1` has asked for."""
        with self._lock:
            return set().union(*(c.sessions for c in self._clients if c.graph))

    def client_count(self):
        with self._lock:
            return len(self._clients)

    def close(self):
        with self._lock:
            self._closed = True
            clients = list(self._clients)
        for client in clients:
            client.close()


def iso_now(epoch):
    stamp = datetime.fromtimestamp(epoch, timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (stamp.microsecond // 1000)


def merge_append(old, new):
    """Combine two events.append payloads for the same agent."""
    events = old["events"] + new["events"]
    merged = {"agentKey": new["agentKey"], "fromSeq": min(old["fromSeq"], new["fromSeq"]),
              "toSeq": max(old["toSeq"], new["toSeq"]), "events": events}
    return cap_inline(merged)


def cap_inline(payload):
    """Over MAX_INLINE_EVENTS events the client fetches the range instead."""
    if len(payload["events"]) > MAX_INLINE_EVENTS or payload.get("overflow"):
        return {"agentKey": payload["agentKey"], "fromSeq": payload["fromSeq"],
                "toSeq": payload["toSeq"], "events": [], "overflow": True}
    return payload


def cap_graph(payload):
    """Over MAX_INLINE_GRAPH_BYTES serialized the client refetches /graph instead."""
    graph = payload.get("graph")
    if graph is not None:
        size = len(json.dumps(graph, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        if size <= MAX_INLINE_GRAPH_BYTES:
            return payload
    return {"sessionKey": payload["sessionKey"], "rev": payload["rev"], "graph": None,
            "overflow": True}


class Coalescer:
    """Holds changes per key for `delay` seconds and publishes the merged result.

    `min_interval` additionally rate-limits a key (stats: one per 2 s).
    `flush(now)` is called by a ticker thread in production and directly by tests.
    """

    def __init__(self, hub, clock=time.time, delay=SSE_COALESCE):
        self.hub = hub
        self.clock = clock
        self.delay = delay
        self._lock = threading.Lock()
        self._pending = {}     # key -> [due, event, data, session_key]
        self._last_sent = {}   # key -> time
        self._order = []
        self._stop = threading.Event()
        self._thread = None

    def submit(self, key, event, data, session_key=None, merge=None, min_interval=0.0):
        now = self.clock()
        with self._lock:
            entry = self._pending.get(key)
            if entry is not None:
                entry[2] = merge(entry[2], data) if merge else data
                entry[1] = event
                return
            due = now + self.delay
            if min_interval:
                due = max(due, self._last_sent.get(key, float("-inf")) + min_interval)
            self._pending[key] = [due, event, data, session_key]
            self._order.append(key)

    def reset_key(self, key):
        """Drop a pending change, used when an agent is reset and its appends are stale."""
        with self._lock:
            if self._pending.pop(key, None) is not None:
                self._order.remove(key)

    def flush(self, now=None, force=False):
        now = self.clock() if now is None else now
        due = []
        with self._lock:
            for key in list(self._order):
                entry = self._pending[key]
                if force or entry[0] <= now:
                    due.append(entry)
                    del self._pending[key]
                    self._order.remove(key)
                    self._last_sent[key] = now
        for _, event, data, session_key in due:
            self.hub.publish(event, data, session_key=session_key)
        return len(due)

    def pending(self):
        with self._lock:
            return len(self._pending)

    def start(self, tick=0.05):
        def run():
            while not self._stop.wait(tick):
                self.flush()
        self._thread = threading.Thread(target=run, name="sse-coalescer", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.flush(force=True)
