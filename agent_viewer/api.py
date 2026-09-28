"""HTTP API routing and JSON handlers (SPEC section 9).

Pure with respect to I/O: `Api.handle` maps a path and query string to a
(status, body) pair using a store object. The store protocol it relies on:

    health() -> dict                    projects() -> list[dict]
    sessions() -> list[dict]            session(key) -> dict | None
    tree(key) -> dict | None            session_stats(key) -> dict | None
    agent(agent_key) -> dict | None     agent_stats(agent_key, scope) -> dict | None
    events(agent_key, before, after, limit, kinds, include_meta, tool_name=None) -> dict | None
    event_full(agent_key, seq) -> dict | None
    graph(key) -> dict | None           (Graph JSON with `rev`, docs/FEATURE-graph-canvas.md 8.2)

This module imports nothing from WS1 so the contract mock server can reuse it.
"""
import base64
import binascii
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote

SOURCES = ("claude", "omp")
STATUSES = ("running", "idle", "stale", "finished")
ACTIVE_STATUSES = frozenset(("running", "idle", "stale"))
RECENT = "recent"
EVENT_KINDS = frozenset(("prompt", "text", "thinking", "tool_call", "tool_result", "skill",
                         "notification", "system", "compaction", "error", "meta"))
SCOPES = ("agent", "subtree")
SESSIONS_LIMIT = (50, 200)
EVENTS_LIMIT = (200, 1000)
TOOL_NAME_MAX = 200
RECENT_WINDOW = 24 * 3600.0
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ApiError(Exception):
    STATUS = {"bad_request": 400, "forbidden_host": 403, "not_found": 404,
              "method_not_allowed": 405, "not_ready": 409}

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = self.STATUS[code]

    def body(self):
        return {"error": {"code": self.code, "message": self.message}}


def bad_request(message):
    return ApiError("bad_request", message)


def not_found(message):
    return ApiError("not_found", message)


# ---------------------------------------------------------------- parameters

def parse_time(text, end_of_day=False):
    """ISO date or datetime to an aware UTC datetime.

    A bare date means the start of that day, or with `end_of_day` the start of
    the next day, so `until=2026-09-25` includes all of the 25th.
    """
    value = text.strip()
    try:
        if len(value) == 10:
            day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return day + timedelta(days=1) if end_of_day else day
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise bad_request("invalid date or datetime: %r" % text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ts(value):
    """Timestamp string from a session to a datetime, missing values sort oldest."""
    if not value:
        return EPOCH
    try:
        return parse_time(value)
    except ApiError:
        return EPOCH


def parse_limit(text, default, maximum):
    if text is None or text == "":
        return default
    try:
        value = int(text)
    except ValueError:
        raise bad_request("limit must be an integer")
    if value < 1:
        raise bad_request("limit must be at least 1")
    return min(value, maximum)


def parse_seq(text, name):
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise bad_request("%s must be an integer" % name)
    if value < 0:
        raise bad_request("%s must not be negative" % name)
    return value


def parse_bool(text, name):
    if text is None or text == "":
        return False
    lowered = text.lower()
    if lowered in ("true", "1"):
        return True
    if lowered in ("false", "0"):
        return False
    raise bad_request("%s must be true or false" % name)


def parse_list(text):
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_source(source):
    if source not in SOURCES:
        raise bad_request("unknown source %r" % source)
    return source


def parse_stream_sessions(query):
    """Session keys from repeated `session=<source>:<sessionId>` parameters."""
    keys = set()
    for value in parse_qs(query, keep_blank_values=True).get("session", []):
        source, _, session_id = value.partition(":")
        if not session_id:
            raise bad_request("session must be <source>:<sessionId>")
        parse_source(source)
        keys.add(value)
    return keys


def parse_stream_graph(query):
    """The opt-in `graph=1` flag of /api/stream (graph.update events)."""
    values = parse_qs(query, keep_blank_values=True).get("graph", [])
    return parse_bool(values[-1] if values else None, "graph")


# ------------------------------------------------------------ session lists

def sort_sessions(items):
    """lastActivityAt descending, ties by key ascending."""
    by_key = sorted(items, key=lambda s: s["key"])
    return sorted(by_key, key=lambda s: _ts(s.get("lastActivityAt")), reverse=True)


def _status_predicate(text, now, recent_window):
    wanted = parse_list(text)
    for status in wanted:
        if status not in STATUSES and status != RECENT:
            raise bad_request("unknown status %r" % status)
    cutoff = datetime.fromtimestamp(now, timezone.utc) - timedelta(seconds=recent_window)

    def matches(item):
        status = item.get("status")
        if status in wanted:
            return True
        if RECENT not in wanted:
            return False
        # "recent" is every active session plus those finished within the window (SPEC G1).
        return status in ACTIVE_STATUSES or (status == "finished" and _ts(item.get("lastActivityAt")) >= cutoff)
    return matches


def _text_predicate(text):
    needle = text.lower()

    def matches(item):
        fields = (item.get("title"), item.get("firstPrompt"), item.get("sessionId"))
        return any(needle in field.lower() for field in fields if field)
    return matches


def _predicates(params, now, recent_window):
    preds = []
    if params.get("source"):
        source = parse_source(params["source"])
        preds.append(lambda s: s.get("source") == source)
    if params.get("project"):
        project = params["project"]
        preds.append(lambda s: s.get("project") == project)
    if params.get("branch"):
        branch = params["branch"].lower()
        preds.append(lambda s: branch in (s.get("gitBranch") or "").lower())
    if params.get("since"):
        since = parse_time(params["since"])
        preds.append(lambda s: _ts(s.get("lastActivityAt")) >= since)
    if params.get("until"):
        until = parse_time(params["until"], end_of_day=True)
        preds.append(lambda s: _ts(s.get("lastActivityAt")) < until)
    if params.get("status"):
        preds.append(_status_predicate(params["status"], now, recent_window))
    if params.get("q"):
        preds.append(_text_predicate(params["q"]))
    return preds


def filter_sessions(items, params, now, recent_window=RECENT_WINDOW):
    """Apply the /api/sessions filters of section 9.1 and sort the result."""
    preds = _predicates(params, now, recent_window)
    return sort_sessions([s for s in items if all(p(s) for p in preds)])


def encode_cursor(item):
    raw = json.dumps([item.get("lastActivityAt"), item["key"]]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor):
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeEncodeError):
        raise bad_request("invalid cursor")
    if (not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], str)
            or not (value[0] is None or isinstance(value[0], str))):
        raise bad_request("invalid cursor")
    return _ts(value[0]), value[1]


def paginate(items, limit, cursor):
    """Keyset pagination over sorted items; the cursor survives inserts."""
    start = 0
    if cursor:
        last_ts, last_key = decode_cursor(cursor)
        start = len(items)
        for index, item in enumerate(items):
            ts = _ts(item.get("lastActivityAt"))
            if ts < last_ts or (ts == last_ts and item["key"] > last_key):
                start = index
                break
    page = items[start:start + limit]
    more = start + limit < len(items)
    return page, (encode_cursor(page[-1]) if more and page else None)


def events_page(agent_key, fetch, before, after, limit, total):
    """Build the /events response of section 9.1.

    `fetch(before, after, limit)` returns the matching event dicts in seq
    order: the last `limit` before `before`, the first `limit` after `after`,
    or the last `limit` overall when both are None.
    """
    items = fetch(before, after, limit)
    if items:
        from_seq, to_seq = items[0]["seq"], items[-1]["seq"]
        has_before = bool(fetch(from_seq, None, 1))
        has_after = bool(fetch(None, to_seq, 1))
    else:
        from_seq = to_seq = None
        has_before = after is not None and bool(fetch(after + 1, None, 1))
        has_after = before is not None and before > 0 and bool(fetch(None, before - 1, 1))
    return {"agentKey": agent_key, "items": items, "fromSeq": from_seq, "toSeq": to_seq,
            "total": total, "hasBefore": has_before, "hasAfter": has_after}


# ------------------------------------------------------------------- router

def split_path(path):
    """Split on `/` before percent-decoding, so `%2F` stays inside a segment."""
    return [unquote(part) for part in path.split("/") if part != ""]


class Api:
    def __init__(self, store, clock=time.time, recent_window=RECENT_WINDOW):
        self.store = store
        self.clock = clock
        self.recent_window = recent_window

    def handle(self, path, query):
        """Return (status, body) for a GET of `path` with raw `query` string."""
        try:
            params = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
            return 200, self._route(split_path(path), params)
        except ApiError as err:
            return err.status, err.body()

    def _route(self, parts, params):
        if parts[:1] != ["api"]:
            raise not_found("no such endpoint")
        rest = parts[1:]
        if rest == ["health"]:
            return self.store.health()
        if rest == ["projects"]:
            return {"items": self.store.projects()}
        if rest == ["sessions"]:
            return self._sessions(params)
        if len(rest) in (3, 4) and rest[0] == "sessions":
            return self._session_route(rest[1:], params)
        if 4 <= len(rest) <= 6 and rest[0] == "agents":
            return self._agent_route(rest[1:], params)
        raise not_found("no such endpoint")

    def _sessions(self, params):
        items = filter_sessions(self.store.sessions(), params, self.clock(), self.recent_window)
        limit = parse_limit(params.get("limit"), *SESSIONS_LIMIT)
        page, next_cursor = paginate(items, limit, params.get("cursor"))
        return {"items": page, "nextCursor": next_cursor, "total": len(items)}

    def _require_session(self, source, session_id):
        key = "%s:%s" % (parse_source(source), session_id)
        summary = self.store.session(key)
        if summary is None:
            raise not_found("session %s not found" % key)
        return key, summary

    def _session_route(self, parts, params):
        key, summary = self._require_session(parts[0], parts[1])
        if len(parts) == 2:
            detail = dict(summary)
            detail["tree"] = self.store.tree(key)
            detail["stats"] = self.store.session_stats(key) if summary.get("indexed") else None
            return detail
        if parts[2] == "tree":
            return self.store.tree(key)
        if parts[2] == "graph":
            return self._found(self.store.graph(key), "graph", key)
        raise not_found("no such endpoint")

    def _agent_route(self, parts, params):
        session_key, summary = self._require_session(parts[0], parts[1])
        agent_key = "%s:%s" % (session_key, parts[2])
        tail = parts[3:]
        if tail == []:
            return self._found(self.store.agent(agent_key), "agent", agent_key)
        if tail == ["events"]:
            return self._events(agent_key, params)
        if len(tail) == 2 and tail[0] == "events":
            seq = parse_seq(tail[1], "seq")
            return self._found(self.store.event_full(agent_key, seq), "event",
                               "%s#%d" % (agent_key, seq))
        if tail == ["stats"]:
            scope = params.get("scope") or "agent"
            if scope not in SCOPES:
                raise bad_request("scope must be agent or subtree")
            if not summary.get("indexed"):
                raise ApiError("not_ready", "session %s is still being indexed" % session_key)
            return self._found(self.store.agent_stats(agent_key, scope), "agent", agent_key)
        raise not_found("no such endpoint")

    def _events(self, agent_key, params):
        before, after = params.get("before"), params.get("after")
        if before and after:
            raise bad_request("before and after are exclusive")
        before = parse_seq(before, "before") if before else None
        after = parse_seq(after, "after") if after else None
        limit = parse_limit(params.get("limit"), *EVENTS_LIMIT)
        kinds = None
        if params.get("kinds"):
            kinds = set(parse_list(params["kinds"]))
            unknown = sorted(kinds - EVENT_KINDS)
            if unknown:
                raise bad_request("unknown kinds: %s" % ", ".join(unknown))
        include_meta = parse_bool(params.get("includeMeta"), "includeMeta")
        tool_name = params.get("toolName") or None
        if tool_name is not None and len(tool_name) > TOOL_NAME_MAX:
            raise bad_request("toolName is too long")
        page = self.store.events(agent_key, before, after, limit, kinds, include_meta,
                                 tool_name=tool_name)
        return self._found(page, "agent", agent_key)

    @staticmethod
    def _found(value, what, key):
        if value is None:
            raise not_found("%s %s not found" % (what, key))
        return value
