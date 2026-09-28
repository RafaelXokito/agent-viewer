"""In-memory store of sessions, AgentStates and FileTails, with a change feed (SPEC 7, 9).

All mutation happens on the watcher thread through `scan`, `index_step`,
`poll` and `refresh`; HTTP handler threads only read. One re-entrant lock
guards everything, and file reads are chunked (1 MiB) so it is held briefly.

WS1 is used only through the section 14.5.1 interface; the few places that
depend on WS1 value shapes are the `_ws1_*` helpers at the bottom.
"""
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from . import api as api_mod
from . import graph as graph_mod
from . import sse
from .accumulate import AgentState
from .claude_parser import ClaudeParser
from .model import format_ts
from .discovery import discover, omp_session_id_from_stem, read_sidecar
from .omp_parser import OmpParser
from .stats import agent_stats, subtree_stats
from .status import StatusConfig, decide_status, session_status, signals_for_agent
from .tree import build_tree
from .tail import FileTail

VERSION = "1"
INDEX_CHUNK = 1024 * 1024
HEAD_TAIL_BYTES = 64 * 1024
HOT_WINDOW = 15 * 60.0
PARSERS = {"claude": ClaudeParser, "omp": OmpParser}
SIDECAR_KINDS = ("meta", "forked_skill")
TRANSCRIPT_KINDS = ("main", "subagent", "omp_root", "omp_child")
ROOT_KINDS = ("main", "omp_root")


@dataclass
class StoreConfig:
    claude_root: Optional[str] = None
    omp_root: Optional[str] = None
    idle_window: float = 30 * 60.0
    stale_after: float = 10 * 60.0
    recent_window: float = api_mod.RECENT_WINDOW
    hot_window: float = HOT_WINDOW
    index_chunk: int = INDEX_CHUNK


class NullPublisher:
    def submit(self, *args, **kwargs):
        pass

    def reset_key(self, key):
        pass


class _Agent:
    def __init__(self, sf, session_key, key, max_read):
        self.sf = sf
        self.key = key
        self.session_key = session_key
        self.agent_id = sf.agent_hint
        self.is_root = sf.kind in ROOT_KINDS
        self.path = sf.path
        self.tail = FileTail(sf.path, max_read=max_read)
        self.state: AgentState
        self.meta = None
        self.forked = None
        self.sidecar_mtimes = {}
        self.indexed = False
        self.missing = False
        self.mtime = 0.0
        self.status = None
        self.stats_dirty = True
        self.new_state()

    def new_state(self):
        parser = PARSERS[self.sf.source](self.key)
        self.state = AgentState(self.key, parser, file=self.path)
        if self.forked and self.forked.get("skillName"):
            self.state.add_forked_skill(self.forked["skillName"])


class _Session:
    def __init__(self, source, session_id, project):
        self.source = source
        self.session_id = session_id
        self.key = "%s:%s" % (source, session_id)
        self.root_key = self.key + ":main"
        self.project = project
        self.agents = {}
        self.quick = {}
        self.first_ts = None
        self.last_ts = None
        self.summary = None
        self.tree = None
        self.tree_obj = None
        self.dirty = True
        self.graph = None           # last built graph, without rev
        self.graph_rev = 0          # bumped each time the built graph changes
        self.graph_published = 0    # rev of the last graph.update submitted
        self.graph_chain = frozenset()  # session keys of the last graph's session nodes


class Store:
    def __init__(self, config, publisher=None, clock=time.time, watched_sessions=None,
                 watched_graph_sessions=None):
        self.config = config
        # Sessions open in a browser; their files are polled at the fast interval (A2.4).
        self.watched_sessions = watched_sessions or frozenset
        # Sessions open in graph layout; only their graphs are built and published (G-PERF-4).
        self.watched_graph_sessions = watched_graph_sessions or frozenset
        self.publisher = publisher or NullPublisher()
        self.clock = clock
        self.status_config = StatusConfig(idle_window=config.idle_window,
                                          stale_after=config.stale_after)
        self.lock = threading.RLock()
        self._sessions = {}
        self.agents = {}
        self.queue = []          # agent keys waiting for a full index, front first
        self._continued_from = {}
        self._graph_watch = frozenset()   # graph-watched session keys, per refresh pass
        self._summaries_changed = set()   # session keys whose summary changed this pass

    # ================================================================ writing

    def scan(self):
        """Discover files under both roots; new ones are queued for indexing."""
        files = discover(self.config.claude_root, self.config.omp_root)
        seen = set()
        with self.lock:
            for sf in files:
                if sf.kind in TRANSCRIPT_KINDS:
                    seen.add(self._add_transcript(sf))
            for sf in files:
                if sf.kind in SIDECAR_KINDS:
                    self._apply_sidecar(sf)
            for key, agent in self.agents.items():
                if key not in seen and not agent.missing:
                    agent.missing = True
                    self._sessions[agent.session_key].dirty = True
            self._sort_queue()

    def _session_id(self, sf):
        if sf.source == "omp":
            return omp_session_id_from_stem(sf.session_hint)
        return sf.session_hint

    def _add_transcript(self, sf):
        session_id = self._session_id(sf)
        session_key = "%s:%s" % (sf.source, session_id)
        key = "%s:%s" % (session_key, sf.agent_hint)
        existing = self.agents.get(key)
        if existing is not None:
            if existing.missing:
                existing.missing = False
                self._sessions[session_key].dirty = True
            return key
        session = self._sessions.get(session_key)
        if session is None:
            session = self._sessions[session_key] = _Session(sf.source, session_id, sf.project)
        agent = _Agent(sf, session_key, key, self.config.index_chunk)
        session.agents[key] = agent
        session.dirty = True
        self.agents[key] = agent
        self.queue.append(key)
        if agent.is_root:
            session.quick = self._quick_summary(agent)
        return key

    def _apply_sidecar(self, sf):
        key = "%s:%s:%s" % (sf.source, self._session_id(sf), sf.agent_hint)
        agent = self.agents.get(key)
        if agent is None:
            return
        try:
            mtime = os.stat(sf.path).st_mtime
        except OSError:
            return
        if agent.sidecar_mtimes.get(sf.kind) == mtime:
            return
        agent.sidecar_mtimes[sf.kind] = mtime
        value = read_sidecar(sf.path)
        if sf.kind == "meta":
            agent.meta = value
        else:
            first = agent.forked is None
            agent.forked = value
            if first and value and value.get("skillName"):
                agent.state.add_forked_skill(value["skillName"])
            agent.stats_dirty = True
        self._sessions[agent.session_key].dirty = True

    def _sort_queue(self):
        """Newest files first, so live and recent sessions are complete first."""
        self.queue.sort(key=self._queue_key)

    def _queue_key(self, key):
        try:
            return -os.stat(self.agents[key].path).st_mtime
        except OSError:
            return 0.0

    def prioritize(self, session_keys):
        """Move the files of these sessions to the front of the indexer queue."""
        with self.lock:
            front = [k for k in self.queue if self.agents[k].session_key in session_keys]
            if front:
                rest = [k for k in self.queue if k not in set(front)]
                self.queue = front + rest

    def index_step(self, budget=0.3):
        """Index queued files for up to `budget` seconds. True if work remains."""
        deadline = self.clock() + budget if budget is not None else None
        while True:
            with self.lock:
                if not self.queue:
                    return False
                agent = self.agents[self.queue[0]]
                more = self._consume(agent)
                if not more:
                    agent.indexed = True
                    self.queue.pop(0)
                    self._sessions[agent.session_key].dirty = True
            if deadline is not None and self.clock() >= deadline:
                with self.lock:
                    return bool(self.queue)

    def poll(self, now=None, hot_only=True):
        """Read new bytes from indexed files: hot ones every call, all if not hot_only."""
        now = self.clock() if now is None else now
        watched = self.watched_sessions()
        with self.lock:
            candidates = [a for a in self.agents.values() if a.indexed]
        for agent in candidates:
            if hot_only and not self._is_hot(agent, now, watched):
                continue
            with self.lock:
                while self._consume(agent):
                    pass

    def _is_hot(self, agent, now, watched):
        return (agent.session_key in watched or agent.status in ("running", "idle")
                or now - agent.mtime < self.config.hot_window)

    def _consume(self, agent):
        """One tail read: feed complete lines, publish appends. True if more to read."""
        result = agent.tail.poll()
        session = self._sessions[agent.session_key]
        if result.missing:
            if not agent.missing:
                agent.missing = True
                session.dirty = True
            return False
        if agent.missing:
            agent.missing = False
            session.dirty = True
        agent.mtime = result.mtime
        if result.reset:
            self._reset(agent, session)
        if result.changed:
            session.dirty = True
        first_new = None
        new_events = []
        for line in result.lines:
            parsed = agent.state.feed_line(line.raw, line.offset, line.line_no)
            if parsed is None:
                continue
            self._absorb(agent, session, parsed)
            if parsed.events:
                new_events.extend(parsed.events)
        _ws1_set_partial_tail(agent.state, agent.tail.partial_tail)
        if new_events:
            agent.stats_dirty = True
            session.dirty = True
            if agent.indexed:
                first_new = new_events[0]
                self._publish_append(agent, new_events, first_new)
        return result.more

    def _reset(self, agent, session):
        agent.new_state()
        agent.stats_dirty = True
        session.dirty = True
        self.publisher.reset_key("append:" + agent.key)
        self.publisher.submit("reset:" + agent.key, "agent.reset", {"agentKey": agent.key},
                              session_key=session.key)

    def _absorb(self, agent, session, parsed):
        for event in parsed.events:
            ts = _ws1_timestamp(event)
            if ts:
                if agent.is_root and session.first_ts is None:
                    session.first_ts = ts
                if session.last_ts is None or ts > session.last_ts:
                    session.last_ts = ts

    def _publish_append(self, agent, events, _first):
        dicts = [_ws1_event_dict(e) for e in events]
        payload = sse.cap_inline({"agentKey": agent.key, "fromSeq": dicts[0]["seq"],
                                  "toSeq": dicts[-1]["seq"], "events": dicts})
        self.publisher.submit("append:" + agent.key, "events.append", payload,
                              session_key=agent.session_key, merge=sse.merge_append)

    def refresh(self, now=None, force=False):
        """Recompute statuses, summaries and trees; publish what changed."""
        now = self.clock() if now is None else now
        graph_watch = self.watched_graph_sessions()
        with self.lock:
            self._graph_watch, self._summaries_changed, refreshed = graph_watch, set(), set()
            previous, self._continued_from = self._continued_from, {}
            for session in self._sessions.values():
                successor = _session_ref(session.source,
                                         self._session_meta(session).get("continuedIn"))
                if successor:
                    self._continued_from[successor] = session.key
            for key in set(previous.items()) ^ set(self._continued_from.items()):
                if key[0] in self._sessions:
                    self._sessions[key[0]].dirty = True
            for session in self._sessions.values():
                # Without a file change only time moves statuses, and finished is terminal.
                settled = all(a.status == "finished" for a in session.agents.values())
                if settled and not (session.dirty or force):
                    continue
                statuses_changed = self._update_statuses(session, now)
                if not (session.dirty or statuses_changed or force):
                    continue
                session.dirty = False
                self._refresh_session(session)
                refreshed.add(session.key)
            self._refresh_chain_graphs(refreshed)

    def _update_statuses(self, session, now):
        all_signals = []
        for agent in session.agents.values():
            all_signals.extend(agent.state.status_signals)
        changed = False
        for agent in session.agents.values():
            signals = signals_for_agent(agent.agent_id, agent.state.status_signals,
                                        all_signals, agent.meta)
            status = decide_status(signals, agent.mtime, now, agent.is_root, self.status_config)
            if status != agent.status:
                agent.status = status
                changed = True
        return changed

    def _refresh_session(self, session):
        session.tree_obj, tree = self._build_tree(session)
        if tree != session.tree:
            session.tree = tree
            self.publisher.submit("tree:" + session.key, "tree.update",
                                  {"sessionKey": session.key, "tree": tree},
                                  session_key=session.key)
        summary = self._summary(session)
        if summary != session.summary:
            session.summary = summary
            self._summaries_changed.add(session.key)
            self.publisher.submit("upsert:" + session.key, "session.upsert", summary)
        dirty = [a for a in session.agents.values() if a.stats_dirty]
        for agent in dirty:
            agent.stats_dirty = False
            if self._session_indexed(session):
                self.publisher.submit("stats:%s:agent" % agent.key, "stats.update",
                                      {"key": agent.key, "scope": "agent",
                                       "stats": self._agent_stats(agent)},
                                      session_key=session.key, min_interval=sse.STATS_MIN_INTERVAL)
        if dirty and self._session_indexed(session) and session.root_key in session.agents:
            self.publisher.submit("stats:%s:subtree" % session.root_key, "stats.update",
                                  {"key": session.root_key, "scope": "subtree",
                                   "stats": self._subtree_stats(session, session.root_key)},
                                  session_key=session.key, min_interval=sse.STATS_MIN_INTERVAL)
        if session.key in self._graph_watch:
            self._publish_graph(session)

    def _refresh_chain_graphs(self, refreshed):
        """A chain neighbour's summary change also changes the graphs that show it (G-FR-13)."""
        if not self._summaries_changed:
            return
        for key in sorted(self._graph_watch - refreshed):
            session = self._sessions.get(key)
            if session is not None and session.graph_chain & self._summaries_changed:
                self._publish_graph(session)

    def _publish_graph(self, session):
        graph = self._current_graph(session)
        if session.graph_rev == session.graph_published:
            return
        session.graph_published = session.graph_rev
        payload = sse.cap_graph({"sessionKey": session.key, "rev": session.graph_rev,
                                 "graph": graph})
        self.publisher.submit("graph:" + session.key, sse.GRAPH_EVENT, payload,
                              session_key=session.key, min_interval=sse.GRAPH_MIN_INTERVAL)

    def _current_graph(self, session):
        """Build the session graph; a change bumps `rev`. Returns the graph with its rev."""
        built = graph_mod.build_graph(session.key, session.tree, self._summary_map(),
                                      self._usage_by_agent(session))
        if built != session.graph:
            session.graph = built
            session.graph_rev += 1
            session.graph_chain = frozenset(n["sessionKey"] for n in built["nodes"]
                                            if n["kind"] == "session")
        return _with_rev(session.graph, session.graph_rev)

    def _summary_map(self):
        return {k: s.summary for k, s in self._sessions.items() if s.summary is not None}

    def _usage_by_agent(self, session):
        usage = {}
        for key, agent in session.agents.items():
            stats = agent.state.stats()
            usage[key] = {"tools": stats.tools, "skills": stats.skills, "errors": stats.errors,
                          "pending": agent.state.pending_by_tool()}
        return usage

    # =========================================================== derivation

    def _session_indexed(self, session):
        return all(a.indexed for a in session.agents.values())

    def _build_tree(self, session):
        nodes, links = [], []
        project_agents = [a for s in self._sessions.values()
                          if s.source == session.source and s.project == session.project
                          for a in s.agents.values()]
        for agent in project_agents:
            nodes.append(_ws1_node_input(agent))
            links.extend(agent.state.links)
        tree = build_tree(nodes, links)
        return tree, _ws1_session_tree(tree, session)

    def _session_meta(self, session):
        root = session.agents.get(session.root_key)
        return dict(root.state.session_meta) if root else {}

    def _summary(self, session):
        meta = self._session_meta(session)
        quick = session.quick
        indexed = self._session_indexed(session)
        statuses = [a.status for a in session.agents.values()]
        root = session.agents.get(session.root_key)
        status = session_status(statuses, root.status if root else None)
        stats = self._subtree_stats(session, session.root_key) if indexed and root else None
        first_prompt = meta.get("firstPrompt") or quick.get("firstPrompt")
        title = meta.get("title") or quick.get("title")
        continued_in = _session_ref(session.source, meta.get("continuedIn"))
        continued_from = self._continued_from.get(session.key)
        return {
            "key": session.key, "source": session.source, "sessionId": session.session_id,
            "project": session.project,
            "cwd": meta.get("cwd") or quick.get("cwd"),
            "gitBranch": meta.get("gitBranch") or quick.get("gitBranch"),
            "title": title, "firstPrompt": first_prompt,
            "startedAt": session.first_ts or quick.get("startedAt") or session.last_ts,
            "lastActivityAt": max(filter(None, (session.last_ts, quick.get("lastActivityAt"))),
                                  default=None),
            "status": status, "indexed": indexed,
            "agentCount": len(session.agents),
            "runningAgents": statuses.count("running"),
            "tokens": stats["tokens"] if stats else None,
            "errors": sum((stats.get("errors") or {}).values()) if stats else None,
            "continuedFrom": continued_from,
            "continuedIn": continued_in,
            "prUrl": meta.get("prUrl"),
            "rootAgentKey": session.root_key,
            "version": _str_or_none(meta.get("version")),
            "mtime": _iso(max((a.mtime for a in session.agents.values()), default=0.0)),
        }

    def _quick_summary(self, agent):
        """Cheap summary from the first and last 64 KiB of a root file (SPEC 7.2)."""
        try:
            with open(agent.path, "rb") as fh:
                head = fh.read(HEAD_TAIL_BYTES)
                size = os.fstat(fh.fileno()).st_size
                fh.seek(max(0, size - HEAD_TAIL_BYTES))
                tail = fh.read(HEAD_TAIL_BYTES)
        except OSError:
            return {}
        scratch = AgentState(agent.key, PARSERS[agent.sf.source](agent.key), file=agent.path)
        first_ts = None
        for line_no, raw in enumerate(head.split(b"\n")[:-1], 1):
            parsed = scratch.feed_line(raw, 0, line_no)
            for event in (parsed.events if parsed else ()):
                first_ts = first_ts or _ws1_timestamp(event)
        meta = scratch.session_meta
        last_ts = None
        for raw in reversed(tail.split(b"\n")[1:]):
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("timestamp"), str):
                last_ts = value["timestamp"]
                break
        return {"title": meta.get("title"), "cwd": meta.get("cwd"), "gitBranch": meta.get("gitBranch"),
                "firstPrompt": meta.get("firstPrompt"), "startedAt": first_ts,
                "lastActivityAt": last_ts}

    def _agent_stats(self, agent):
        tree = self._sessions[agent.session_key].tree_obj
        stats = agent.state.stats()
        return _ws1_stats_dict(agent_stats(tree, agent.key, stats) if tree else stats)

    def _subtree_stats(self, session, key):
        tree = session.tree_obj
        if tree is None or key not in tree.nodes:
            return None
        keys = tree.subtree_keys(key)
        by_key = {k: self.agents[k].state.stats() for k in keys if k in self.agents}
        return _ws1_stats_dict(subtree_stats(tree, key, by_key))

    # ============================================================== reading

    def health(self):
        with self.lock:
            done = sum(1 for a in self.agents.values() if a.indexed)
            return {"ok": True, "version": VERSION,
                    "roots": {"claude": self.config.claude_root, "omp": self.config.omp_root},
                    "indexing": {"queued": len(self.queue), "done": done}}

    def projects(self):
        with self.lock:
            groups = {}
            for session in self._sessions.values():
                if session.summary is None:
                    continue
                entry = groups.setdefault((session.source, session.project),
                                          {"source": session.source, "project": session.project,
                                           "cwd": session.summary.get("cwd"), "sessionCount": 0})
                entry["sessionCount"] += 1
                entry["cwd"] = entry["cwd"] or session.summary.get("cwd")
            return [groups[k] for k in sorted(groups)]

    def sessions(self):
        with self.lock:
            return [dict(s.summary) for s in self._sessions.values() if s.summary is not None]

    def session(self, key):
        with self.lock:
            session = self._sessions.get(key)
            return dict(session.summary) if session and session.summary else None

    def tree(self, key):
        with self.lock:
            session = self._sessions.get(key)
            return session.tree if session else None

    def session_stats(self, key):
        with self.lock:
            session = self._sessions.get(key)
            if session is None or not self._session_indexed(session):
                return None
            return self._subtree_stats(session, session.root_key)

    def _agent(self, agent_key) -> Tuple[Optional["_Agent"], Optional["_Session"]]:
        agent = self.agents.get(agent_key)
        if agent is None:
            return None, None
        return agent, self._sessions[agent.session_key]

    def agent(self, agent_key):
        with self.lock:
            agent, session = self._agent(agent_key)
            if agent is None or session is None:
                node = ((self._sessions.get(agent_key.rsplit(":", 1)[0]) or _Session("", "", ""))
                        .tree or {}).get("nodes", {}).get(agent_key)
                return dict(node, file=None, toolCalls=0, skills=[]) if node else None
            node = dict((session.tree or {}).get("nodes", {}).get(agent_key) or {})
            node["file"] = agent.path
            node["toolCalls"] = _ws1_tool_call_count(agent.state)
            node["skills"] = _ws1_skills(agent.state)
            return node

    def agent_stats(self, agent_key, scope):
        with self.lock:
            agent, session = self._agent(agent_key)
            if agent is None:
                return None
            if scope == "agent":
                return self._agent_stats(agent)
            return self._subtree_stats(session, agent_key)

    def graph(self, key):
        """Graph JSON of feature doc 8.2 with `rev`; built from the current state."""
        with self.lock:
            session = self._sessions.get(key)
            if session is None or session.summary is None:
                return None
            return self._current_graph(session)

    def events(self, agent_key, before, after, limit, kinds, include_meta, tool_name=None):
        with self.lock:
            agent, _ = self._agent(agent_key)
            if agent is None:
                return None
            state = agent.state

            def fetch(before_seq, after_seq, count):
                found = state.events(before_seq, after_seq, count, kinds, include_meta,
                                     tool_name=tool_name)
                return [_ws1_event_dict(e) for e in found]

            return api_mod.events_page(agent_key, fetch, before, after, limit,
                                       _ws1_event_count(state))

    def event_full(self, agent_key, seq):
        with self.lock:
            agent, _ = self._agent(agent_key)
            if agent is None:
                return None
            try:
                ref = agent.state.event_ref(seq)
            except (IndexError, KeyError, ValueError):
                return None
            if ref is None:
                return None
            state = agent.state
            path = agent.path
        try:
            with open(path, "rb") as fh:
                fh.seek(ref.offset)
                raw = fh.read(ref.length)
            record = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        with self.lock:
            if agent.state is not state:  # reset while the line was being read
                return None
            body = state.full_content(seq, record)
        return dict(body) if body is not None else None


# ==================================================================== helpers

def _iso(epoch):
    if not epoch:
        return None
    return sse.iso_now(epoch)


def _with_rev(graph, rev):
    """A copy of `graph` with `rev` right after `sessionKey`, as in graph.json."""
    out = {"sessionKey": graph["sessionKey"], "rev": rev}
    out.update((k, v) for k, v in graph.items() if k != "sessionKey")
    return out


def _str_or_none(value):
    return None if value is None else str(value)


def _session_ref(source, value):
    if not value:
        return None
    return value if value.startswith(source + ":") else "%s:%s" % (source, value)


# ------------------------------------------------- WS1 value-shape adapters

def _to_dict(value) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return value.to_dict()


def _ws1_timestamp(event):
    ts = getattr(event, "timestamp", None)
    return format_ts(ts) if isinstance(ts, datetime) else ts


def _ws1_event_dict(event):
    data = dict(_to_dict(event))
    data.pop("ref", None)
    return data


def _ws1_event_count(state):
    return state.event_count


def _ws1_stats_dict(stats):
    return dict(_to_dict(stats))


def _ws1_set_partial_tail(state, value):
    state.parse_stats.partial_tail = value


def _ws1_node_input(agent):
    return agent.state.node_input(project=agent.sf.project, status=agent.status or "running",
                                  meta=agent.meta, forked_skill=agent.forked,
                                  missing=agent.missing)


def _ws1_session_tree(tree, session):
    data = tree.session_tree(session.key)
    if not data:
        return {"sessionKey": session.key, "rootKey": session.root_key, "nodes": {}}
    data = dict(data)
    data.setdefault("sessionKey", session.key)
    return data


def _ws1_tool_call_count(state):
    calls = getattr(state, "tool_calls", None)
    return len(calls) if calls is not None else 0


def _ws1_skills(state):
    return [_to_dict(s) for s in getattr(state, "skills", []) or []]
