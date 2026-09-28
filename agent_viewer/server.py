"""HTTP server: host check, method check, security headers, static files, SSE (SPEC 9, 11)."""
import json
import os
import threading
from typing import Callable, Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from . import api as api_mod
from . import sse

HOST = "127.0.0.1"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
BASE_HEADERS = (("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"))
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8"}
JSON_TYPE = "application/json; charset=utf-8"
EXCLUDED_STATIC_DIRS = ("tests",)


class StaticFiles:
    """Allowlist of frontend files keyed by relative name, rebuilt on a miss.

    Request paths are only ever looked up as dictionary keys, so `..`, absolute
    paths and encoded separators cannot reach the filesystem.
    """

    def __init__(self, root):
        self.root = os.path.realpath(root) if root else None
        self._lock = threading.Lock()
        self._files = {}
        self._scan()

    def _scan(self):
        files = {}
        if self.root and os.path.isdir(self.root):
            for dirpath, dirnames, filenames in os.walk(self.root):
                rel_dir = os.path.relpath(dirpath, self.root)
                if rel_dir == ".":
                    dirnames[:] = [d for d in dirnames if d not in EXCLUDED_STATIC_DIRS]
                for name in filenames:
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in CONTENT_TYPES:
                        continue
                    full = os.path.join(dirpath, name)
                    if not os.path.realpath(full).startswith(self.root + os.sep):
                        continue
                    rel = name if rel_dir == "." else "%s/%s" % (rel_dir.replace(os.sep, "/"), name)
                    files[rel] = (full, CONTENT_TYPES[ext])
        with self._lock:
            self._files = files

    def lookup(self, name):
        with self._lock:
            hit = self._files.get(name)
        if hit is None:
            self._scan()
            with self._lock:
                hit = self._files.get(name)
        return hit


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-viewer"
    sys_version = ""

    # Wired up by make_server on a per-server subclass; never None while serving.
    api: api_mod.Api
    hub: sse.Hub
    static: "StaticFiles"
    on_stream_sessions: Optional[Callable[[set], None]] = None

    def log_message(self, fmt, *args):  # keys and counts only, never content
        pass

    def __getattr__(self, name):
        # Any method other than GET and HEAD reaches here through do_<METHOD>.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    # ---------------------------------------------------------------- helpers

    def _host_ok(self):
        port = self.server.server_port  # type: ignore[attr-defined]  # set by HTTPServer.server_bind
        host = self.headers.get("Host", "")
        return host in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def _send(self, status, body, content_type, extra=(), api=True):
        self.send_response(status)
        for name, value in BASE_HEADERS:
            self.send_header(name, value)
        if api:
            self.send_header("Cache-Control", "no-store")
        for name, value in extra:
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status, obj, extra=()):
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._send(status, body, JSON_TYPE, extra)

    def _send_error(self, code, message, extra=()):
        err = api_mod.ApiError(code, message)
        self._send_json(err.status, err.body(), extra)

    def _method_not_allowed(self):
        if not self._host_ok():
            return self._send_error("forbidden_host", "host not allowed")
        self._send_error("method_not_allowed", "only GET and HEAD are allowed",
                         extra=(("Allow", "GET, HEAD"),))

    # ---------------------------------------------------------------- methods

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._send_error("forbidden_host", "host not allowed")
        path, _, query = self.path.partition("?")
        if path == "/api/stream":
            return self._stream(query)
        if path == "/api" or path.startswith("/api/"):
            status, body = self.api.handle(path, query)
            return self._send_json(status, body)
        return self._static(path)

    def _static(self, path):
        if path == "/":
            name = "index.html"
        elif path.startswith("/static/"):
            name = unquote(path[len("/static/"):])
        else:
            name = None
        hit = self.static.lookup(name) if name else None
        if hit is None:
            return self._send_error("not_found", "no such file")
        full, content_type = hit
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send_error("not_found", "no such file")
        self._send(200, body, content_type, api=False)

    def _stream(self, query):
        try:
            sessions = api_mod.parse_stream_sessions(query)
            graph = api_mod.parse_stream_graph(query)
        except api_mod.ApiError as err:
            return self._send_json(err.status, err.body())
        self.send_response(200)
        for name, value in BASE_HEADERS:
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        client = self.hub.subscribe(sessions, graph=graph)
        if self.on_stream_sessions and sessions:
            self.on_stream_sessions(sessions)
        try:
            self._write(self.hub.hello())
            self._pump(client)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(client)

    def _pump(self, client):
        while True:
            message = client.next(sse.HEARTBEAT_S)
            if message is sse.CLOSE:
                return
            if message is sse.OVERFLOW:
                self._write(sse.frame("resync", {}))
                return
            self._write(sse.HEARTBEAT if message is None else message)

    def _write(self, data):
        self.wfile.write(data)
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(api, hub, port, static_dir, on_stream_sessions=None):
    """Bind to 127.0.0.1 only; `port` 0 picks a free port."""
    handler = type("BoundHandler", (Handler,), {
        "api": api, "hub": hub, "static": StaticFiles(static_dir),
        "on_stream_sessions": staticmethod(on_stream_sessions) if on_stream_sessions else None,
    })
    return Server((HOST, port), handler)
