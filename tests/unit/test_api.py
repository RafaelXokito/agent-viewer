"""WS2: every endpoint and error code, pagination bounds, host check, headers, 405."""
import http.client
import importlib.util
import json
import os
import sys
import threading
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contract_shape import REPO, load_example, shape_errors  # noqa: E402

from agent_viewer import api as api_mod  # noqa: E402
from agent_viewer import server as server_mod  # noqa: E402
from agent_viewer import sse  # noqa: E402


def load_mock_module():
    path = os.path.join(REPO, "contract", "mock_server.py")
    spec = importlib.util.spec_from_file_location("agent_viewer_mock_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOCK = load_mock_module()
NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def session(key, last, status="finished", source=None, project="p", branch=None,
            title=None, first=None):
    src, sid = key.split(":", 1)
    return {"key": key, "source": source or src, "sessionId": sid, "project": project,
            "gitBranch": branch, "title": title, "firstPrompt": first,
            "lastActivityAt": last, "status": status}


class ParseTimeTest(unittest.TestCase):
    def test_parses_z_datetime(self):
        got = api_mod.parse_time("2026-09-25T10:00:01.500Z")
        self.assertEqual(got, datetime(2026, 9, 25, 10, 0, 1, 500000, tzinfo=timezone.utc))

    def test_date_only_since_is_start_of_day(self):
        got = api_mod.parse_time("2026-09-25")
        self.assertEqual(got, datetime(2026, 9, 25, tzinfo=timezone.utc))

    def test_date_only_until_is_end_of_day_exclusive(self):
        got = api_mod.parse_time("2026-09-25", end_of_day=True)
        self.assertEqual(got, datetime(2026, 9, 26, tzinfo=timezone.utc))

    def test_invalid_raises_bad_request(self):
        with self.assertRaises(api_mod.ApiError) as ctx:
            api_mod.parse_time("yesterday")
        self.assertEqual(ctx.exception.status, 400)


class FilterSessionsTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            session("claude:a", "2026-09-25T11:00:00.000Z", "running", project="p1",
                    branch="feature/DEMO-1", title="Alpha work"),
            session("claude:b", "2026-09-20T11:00:00.000Z", "finished", project="p2",
                    first="Fix the Beta bug"),
            session("omp:c", "2026-09-25T09:00:00.000Z", "finished", project="p1"),
            session("claude:d", "2026-09-25T08:00:00.000Z", "stale", project="p1"),
        ]

    def keys(self, params):
        got = api_mod.filter_sessions(self.items, params, NOW, recent_window=86400)
        return [s["key"] for s in got]

    def test_no_filter_returns_all_sorted_by_last_activity_desc(self):
        self.assertEqual(self.keys({}), ["claude:a", "omp:c", "claude:d", "claude:b"])

    def test_ties_sort_by_key(self):
        items = [session("claude:z", "2026-09-25T11:00:00.000Z"),
                 session("claude:y", "2026-09-25T11:00:00.000Z")]
        got = api_mod.filter_sessions(items, {}, NOW, recent_window=86400)
        self.assertEqual([s["key"] for s in got], ["claude:y", "claude:z"])

    def test_source_filter(self):
        self.assertEqual(self.keys({"source": "omp"}), ["omp:c"])

    def test_project_is_exact(self):
        self.assertEqual(self.keys({"project": "p2"}), ["claude:b"])
        self.assertEqual(self.keys({"project": "p"}), [])

    def test_branch_is_case_insensitive_substring(self):
        self.assertEqual(self.keys({"branch": "demo"}), ["claude:a"])

    def test_since_and_until(self):
        self.assertEqual(self.keys({"since": "2026-09-25T09:00:00Z"}), ["claude:a", "omp:c"])
        self.assertEqual(self.keys({"until": "2026-09-20"}), ["claude:b"])

    def test_status_list(self):
        self.assertEqual(self.keys({"status": "running,stale"}), ["claude:a", "claude:d"])

    def test_status_recent_means_finished_within_window(self):
        self.assertEqual(self.keys({"status": "recent"}), ["omp:c"])
        self.assertEqual(self.keys({"status": "running,recent"}), ["claude:a", "omp:c"])

    def test_unknown_status_is_bad_request(self):
        with self.assertRaises(api_mod.ApiError):
            self.keys({"status": "sleeping"})

    def test_unknown_source_is_bad_request(self):
        with self.assertRaises(api_mod.ApiError):
            self.keys({"source": "cursor"})

    def test_q_matches_title_first_prompt_and_session_id(self):
        self.assertEqual(self.keys({"q": "alpha"}), ["claude:a"])
        self.assertEqual(self.keys({"q": "beta BUG"}), ["claude:b"])
        self.assertEqual(self.keys({"q": "c"}), ["omp:c"])


class PaginateTest(unittest.TestCase):
    def setUp(self):
        self.items = [session("claude:s%03d" % i, "2026-09-25T10:00:00.000Z") for i in range(7)]

    def test_cursor_walks_all_items_without_overlap(self):
        seen, cursor = [], None
        while True:
            page, cursor = api_mod.paginate(self.items, 3, cursor)
            seen += [s["key"] for s in page]
            if cursor is None:
                break
        self.assertEqual(seen, [s["key"] for s in self.items])

    def test_last_page_has_no_cursor(self):
        page, cursor = api_mod.paginate(self.items, 10, None)
        self.assertEqual(len(page), 7)
        self.assertIsNone(cursor)

    def test_garbage_cursor_is_bad_request(self):
        with self.assertRaises(api_mod.ApiError) as ctx:
            api_mod.paginate(self.items, 3, "not-a-cursor")
        self.assertEqual(ctx.exception.code, "bad_request")

    def test_limit_parsing_bounds(self):
        self.assertEqual(api_mod.parse_limit(None, 50, 200), 50)
        self.assertEqual(api_mod.parse_limit("500", 50, 200), 200)
        for bad in ("0", "-1", "ten"):
            with self.assertRaises(api_mod.ApiError):
                api_mod.parse_limit(bad, 50, 200)


class ApiRoutingTest(unittest.TestCase):
    """Every endpoint of section 9.1 and every error code, over the example store."""

    def setUp(self):
        self.store = MOCK.ExampleStore()
        self.api = api_mod.Api(self.store, clock=lambda: NOW)

    def get(self, path, query=""):
        return self.api.handle(path, query)

    def assert_matches(self, example, body):
        self.assertEqual(shape_errors(load_example(example), body), [])

    def assert_error(self, result, status, code):
        got_status, body = result
        self.assertEqual(got_status, status, body)
        self.assertEqual(body["error"]["code"], code)
        self.assertIsInstance(body["error"]["message"], str)

    def test_health(self):
        status, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assert_matches("health.json", body)

    def test_projects(self):
        status, body = self.get("/api/projects")
        self.assertEqual(status, 200)
        self.assert_matches("projects.json", body)

    def test_sessions(self):
        status, body = self.get("/api/sessions")
        self.assertEqual(status, 200)
        self.assert_matches("sessions.json", body)
        self.assertEqual(body["total"], 3)

    def test_sessions_pagination_and_total(self):
        status, body = self.get("/api/sessions", "limit=2")
        self.assertEqual((status, len(body["items"]), body["total"]), (200, 2, 3))
        status, rest = self.get("/api/sessions", "limit=2&cursor=" + body["nextCursor"])
        self.assertEqual(len(rest["items"]), 1)
        self.assertIsNone(rest["nextCursor"])

    def test_sessions_bad_limit(self):
        self.assert_error(self.get("/api/sessions", "limit=abc"), 400, "bad_request")

    def test_session_detail(self):
        status, body = self.get("/api/sessions/claude/s-main")
        self.assertEqual(status, 200)
        self.assert_matches("session_detail.json", body)

    def test_session_not_found(self):
        self.assert_error(self.get("/api/sessions/claude/nope"), 404, "not_found")

    def test_bad_source(self):
        self.assert_error(self.get("/api/sessions/cursor/s-main"), 400, "bad_request")

    def test_tree(self):
        status, body = self.get("/api/sessions/claude/s-main/tree")
        self.assertEqual(status, 200)
        self.assert_matches("tree.json", body)

    def test_agent(self):
        status, body = self.get("/api/agents/claude/s-main/a2222222222222222")
        self.assertEqual(status, 200)
        self.assert_matches("agent.json", body)

    def test_agent_not_found(self):
        self.assert_error(self.get("/api/agents/claude/s-main/zzz"), 404, "not_found")

    def test_events_last_page(self):
        status, body = self.get("/api/agents/claude/s-main/a2222222222222222/events")
        self.assertEqual(status, 200)
        self.assert_matches("events_page.json", body)

    def test_events_after_and_limit(self):
        status, body = self.get("/api/agents/claude/s-main/a2222222222222222/events",
                                "after=0&limit=2")
        self.assertEqual([e["seq"] for e in body["items"]], [1, 2])
        self.assertTrue(body["hasBefore"])
        self.assertTrue(body["hasAfter"])

    def test_events_kinds_filter(self):
        status, body = self.get("/api/agents/claude/s-main/a2222222222222222/events",
                                "kinds=tool_call,tool_result")
        self.assertEqual([e["kind"] for e in body["items"]], ["tool_call", "tool_result"])

    def test_events_bad_params(self):
        base = "/api/agents/claude/s-main/a2222222222222222/events"
        for query in ("before=1&after=0", "before=x", "limit=1001x", "limit=0",
                      "kinds=bogus", "includeMeta=maybe"):
            self.assert_error(self.get(base, query), 400, "bad_request")

    def test_events_limit_capped(self):
        status, body = self.get("/api/agents/claude/s-main/a2222222222222222/events",
                                "limit=5000")
        self.assertEqual(status, 200)

    def test_event_full(self):
        status, body = self.get("/api/agents/claude/s-main/a2222222222222222/events/2")
        self.assertEqual(status, 200)
        self.assert_matches("event_full.json", body)

    def test_event_full_bad_and_missing_seq(self):
        base = "/api/agents/claude/s-main/a2222222222222222/events/"
        self.assert_error(self.get(base + "x"), 400, "bad_request")
        self.assert_error(self.get(base + "999"), 404, "not_found")

    def test_stats_scopes(self):
        base = "/api/agents/claude/s-main/a2222222222222222/stats"
        status, body = self.get(base, "scope=agent")
        self.assertEqual(status, 200)
        self.assert_matches("stats_agent.json", body)
        status, body = self.get(base)
        self.assertEqual(body["scope"], "agent")
        status, body = self.get(base, "scope=subtree")
        self.assert_matches("stats_subtree.json", body)
        self.assert_error(self.get(base, "scope=world"), 400, "bad_request")

    def test_stats_not_ready(self):
        self.store.set_indexed("claude:s-main", False)
        self.assert_error(self.get("/api/agents/claude/s-main/main/stats"), 409, "not_ready")
        status, body = self.get("/api/sessions/claude/s-main")
        self.assertEqual(status, 200)
        self.assertIsNone(body["stats"])

    def test_unknown_route(self):
        self.assert_error(self.get("/api/nothing"), 404, "not_found")
        self.assert_error(self.get("/api/sessions/claude/s-main/extra/deep"), 404, "not_found")

    def test_percent_encoded_segments(self):
        # An Oh My Pi agentId with "/" arrives as %2F in one segment (section 5.1).
        self.store.add_agent_alias("omp:o-root:Parent/Child")
        status, body = self.get("/api/agents/omp/o-root/Parent%2FChild")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["key"], "omp:o-root:Parent/Child")

    def test_stream_params(self):
        self.assertEqual(api_mod.parse_stream_sessions("session=claude:a&session=omp:b"),
                         {"claude:a", "omp:b"})
        self.assertEqual(api_mod.parse_stream_sessions(""), set())
        with self.assertRaises(api_mod.ApiError):
            api_mod.parse_stream_sessions("session=nocolon")

    def test_errors_example_shape(self):
        status, body = self.get("/api/sessions/claude/abc")
        self.assertEqual(shape_errors(load_example("error_not_found.json"), body), [])

    # ---------------------------------------------------- graph (feature doc 8)

    def test_graph(self):
        status, body = self.get("/api/sessions/claude/s-main/graph")
        self.assertEqual(status, 200)
        self.assert_matches("graph.json", body)
        self.assertEqual(body, load_example("graph.json"))

    def test_graph_errors(self):
        self.assert_error(self.get("/api/sessions/claude/nope/graph"), 404, "not_found")
        self.assert_error(self.get("/api/sessions/cursor/s-main/graph"), 400, "bad_request")

    def test_graph_while_indexing_is_200(self):
        self.store.set_indexed("claude:s-main", False)
        status, body = self.get("/api/sessions/claude/s-main/graph", "anything=1")
        self.assertEqual(status, 200)
        self.assertFalse(body["indexed"])

    def test_graph_stream_param(self):
        for query, wanted in (("", False), ("session=claude:a", False), ("graph=1", True),
                              ("graph=true", True), ("graph=0", False), ("graph=false", False),
                              ("session=claude:a&graph=1", True)):
            self.assertEqual(api_mod.parse_stream_graph(query), wanted, query)
        for query in ("graph=2", "graph=yes"):
            with self.assertRaises(api_mod.ApiError):
                api_mod.parse_stream_graph(query)

    # ------------------------------------------------ toolName (feature doc 8.8)

    TOOL_BASE = "/api/agents/claude/s-main/a2222222222222222/events"

    def test_events_tool_name_example(self):
        status, body = self.get(self.TOOL_BASE, "toolName=Bash")
        self.assertEqual(status, 200)
        self.assertEqual(body, load_example("events_page_tool_filter.json"))

    def test_events_tool_name_and_kinds(self):
        _, body = self.get(self.TOOL_BASE, "toolName=Bash&kinds=tool_result")
        self.assertEqual([e["seq"] for e in body["items"]], [2])

    def test_events_tool_name_paging(self):
        _, first = self.get(self.TOOL_BASE, "toolName=Bash&limit=1")
        self.assertEqual(([e["seq"] for e in first["items"]], first["hasBefore"], first["hasAfter"]),
                         ([2], True, False))
        _, older = self.get(self.TOOL_BASE, "toolName=Bash&limit=1&before=2")
        self.assertEqual(([e["seq"] for e in older["items"]], older["hasBefore"], older["hasAfter"]),
                         ([1], False, True))
        _, newer = self.get(self.TOOL_BASE, "toolName=Bash&limit=1&after=1")
        self.assertEqual([e["seq"] for e in newer["items"]], [2])
        self.assertFalse(newer["hasAfter"])

    def test_events_tool_name_unknown_and_empty(self):
        _, body = self.get(self.TOOL_BASE, "toolName=Nope")
        self.assertEqual((body["items"], body["fromSeq"], body["toSeq"]), ([], None, None))
        self.assertEqual(body["total"], 4)
        _, body = self.get(self.TOOL_BASE, "toolName=")
        self.assertEqual([e["seq"] for e in body["items"]], [0, 1, 2, 3])

    def test_events_tool_name_too_long(self):
        status, body = self.get(self.TOOL_BASE, "toolName=" + "x" * 201)
        self.assertEqual((status, body["error"]["code"]), (400, "bad_request"))
        self.assertEqual(body["error"]["message"], "toolName is too long")
        status, _ = self.get(self.TOOL_BASE, "toolName=" + "x" * 200)
        self.assertEqual(status, 200)


class HttpServerTest(unittest.TestCase):
    """Host check, method check, security headers, static allowlist, SSE hello."""

    @classmethod
    def setUpClass(cls):
        cls.static = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_static_fixture")
        os.makedirs(os.path.join(cls.static, "lib"), exist_ok=True)
        os.makedirs(os.path.join(cls.static, "tests"), exist_ok=True)
        for name, text in (("index.html", "<!doctype html><title>x</title>"),
                           ("app.js", "export {};"), ("lib/format.js", "export {};"),
                           ("tests/x.test.js", "secret test")):
            with open(os.path.join(cls.static, name), "w") as fh:
                fh.write(text)
        cls.hub = sse.Hub()
        cls.api = api_mod.Api(MOCK.ExampleStore(), clock=lambda: NOW)
        cls.httpd = server_mod.make_server(cls.api, cls.hub, 0, cls.static)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for root, dirs, files in os.walk(cls.static, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(cls.static)

    def request(self, method, path, host=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        all_headers = {"Host": host or "127.0.0.1:%d" % self.port}
        all_headers.update(headers or {})
        conn.request(method, path, headers=all_headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_binds_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_api_headers(self):
        resp, body = self.request("GET", "/api/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(resp.getheader("Content-Security-Policy"), server_mod.CSP)
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        self.assertTrue(json.loads(body)["ok"])

    def test_localhost_host_is_accepted(self):
        resp, _ = self.request("GET", "/api/health", host="localhost:%d" % self.port)
        self.assertEqual(resp.status, 200)

    def test_foreign_host_is_forbidden(self):
        for host in ("evil.example:%d" % self.port, "127.0.0.1:1", "127.0.0.1"):
            resp, body = self.request("GET", "/api/health", host=host)
            self.assertEqual(resp.status, 403, host)
            self.assertEqual(json.loads(body)["error"]["code"], "forbidden_host")
            self.assertEqual(resp.getheader("Content-Security-Policy"), server_mod.CSP)

    def test_non_get_methods_are_405(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "BREW"):
            resp, body = self.request(method, "/api/sessions")
            self.assertEqual(resp.status, 405, method)
            self.assertEqual(json.loads(body)["error"]["code"], "method_not_allowed")
            self.assertEqual(resp.getheader("Allow"), "GET, HEAD")

    def test_head_has_headers_and_no_body(self):
        resp, body = self.request("HEAD", "/api/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(resp.getheader("Content-Type"), "application/json; charset=utf-8")

    def test_api_error_status_propagates(self):
        resp, body = self.request("GET", "/api/sessions/claude/nope")
        self.assertEqual(resp.status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_index_and_static(self):
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertIn(b"<title>", body)
        self.assertIsNone(resp.getheader("Cache-Control"))
        resp, _ = self.request("GET", "/static/lib/format.js")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/javascript; charset=utf-8")

    def test_static_outside_allowlist_is_404(self):
        for path in ("/static/../SPEC.md", "/static/%2e%2e/SPEC.md", "/static//etc/passwd",
                     "/static/tests/x.test.js", "/static/nope.js", "/SPEC.md",
                     "/static/..%2F..%2FSPEC.md"):
            resp, _ = self.request("GET", path)
            self.assertEqual(resp.status, 404, path)

    def test_static_picks_up_new_files(self):
        with open(os.path.join(self.static, "late.css"), "w") as fh:
            fh.write("body{}")
        resp, _ = self.request("GET", "/static/late.css")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/css; charset=utf-8")

    def test_stream_hello_and_published_event(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/stream?session=claude:s-main",
                     headers={"Host": "127.0.0.1:%d" % self.port})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        hello = read_sse_message(resp)
        self.assertEqual(hello["event"], "hello")
        self.assertEqual(hello["retry"], "2000")
        self.assertEqual(hello["data"]["version"], "1")
        self.hub.publish("events.append", {"agentKey": "claude:s-main:main"},
                         session_key="claude:s-main")
        self.hub.publish("events.append", {"agentKey": "claude:other:main"},
                         session_key="claude:other")
        self.hub.publish("session.upsert", {"key": "claude:x"})
        first = read_sse_message(resp)
        second = read_sse_message(resp)
        self.assertEqual(first["data"]["agentKey"], "claude:s-main:main")
        self.assertEqual(second["event"], "session.upsert")
        self.assertGreater(int(second["id"]), int(first["id"]))
        conn.close()

    def test_stream_bad_session_param(self):
        resp, body = self.request("GET", "/api/stream?session=bad")
        self.assertEqual(resp.status, 400)

    def test_stream_bad_graph_param(self):
        resp, body = self.request("GET", "/api/stream?session=claude:s-main&graph=maybe")
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "bad_request")

    def test_graph_head(self):
        resp, body = self.request("HEAD", "/api/sessions/claude/s-main/graph")
        self.assertEqual((resp.status, body), (200, b""))
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_stream_graph_flag_reaches_the_hub(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/stream?session=claude:s-main&graph=1",
                     headers={"Host": "127.0.0.1:%d" % self.port})
        resp = conn.getresponse()
        self.assertEqual(read_sse_message(resp)["event"], "hello")
        self.hub.publish("graph.update", {"sessionKey": "claude:s-main", "rev": 9},
                         session_key="claude:s-main")
        message = read_sse_message(resp)
        self.assertEqual((message["event"], message["data"]["rev"]), ("graph.update", 9))
        conn.close()


try:
    sys.path.insert(0, os.path.join(REPO, "tests", "fixtures"))
    import loader  # WS1 fixture loader, imports the WS1 modules
    from agent_viewer.store import Store, StoreConfig
except ImportError:  # WS1 not importable yet
    loader = None


@unittest.skipIf(loader is None, "WS1 modules not importable")
class RealStoreContractTest(unittest.TestCase):
    """A2.2: the real store answers every endpoint in the shape of contract/examples/."""

    @classmethod
    def setUpClass(cls):
        cls.root = loader.copy_fixtures()
        store = Store(StoreConfig(claude_root=os.path.join(cls.root, "claude"),
                                  omp_root=os.path.join(cls.root, "omp")))
        store.scan()
        while store.index_step(None):
            pass
        store.refresh()
        cls.api = api_mod.Api(store)

    @classmethod
    def tearDownClass(cls):
        loader.remove_fixtures(cls.root)

    def check(self, example, path, query=""):
        status, body = self.api.handle(path, query)
        expected_status = 404 if example.startswith("error") else 200
        self.assertEqual(status, expected_status, body)
        self.assertEqual(shape_errors(load_example(example), body), [], path)
        return body

    def test_every_endpoint_matches_its_example(self):
        a2 = "/api/agents/claude/s-main/a2222222222222222"
        self.check("health.json", "/api/health")
        self.check("projects.json", "/api/projects")
        self.check("sessions.json", "/api/sessions")
        self.check("session_detail.json", "/api/sessions/claude/s-main")
        self.check("tree.json", "/api/sessions/claude/s-main/tree")
        self.check("agent.json", a2)
        self.check("events_page.json", a2 + "/events")
        self.check("event_full.json", a2 + "/events/2")
        self.check("stats_agent.json", a2 + "/stats", "scope=agent")
        self.check("stats_subtree.json", "/api/agents/claude/s-main/main/stats", "scope=subtree")
        self.check("error_not_found.json", "/api/sessions/claude/abc")

    def test_example_tree_values_equal_real_tree(self):
        # Status and lastActivityAt depend on the fixture copy's mtimes and the clock.
        volatile = ("status", "lastActivityAt")
        _, real = self.api.handle("/api/sessions/claude/s-main/tree", "")
        example = load_example("tree.json")
        self.assertEqual(set(real["nodes"]), set(example["nodes"]))
        for key, node in example["nodes"].items():
            for field, value in node.items():
                if field not in volatile:
                    self.assertEqual(real["nodes"][key][field], value, "%s.%s" % (key, field))

    def test_omp_endpoints_match_examples_too(self):
        self.check("session_detail.json", "/api/sessions/omp/o-root")
        self.check("agent.json", "/api/agents/omp/o-root/Checker")
        self.check("events_page.json", "/api/agents/omp/o-root/main/events", "includeMeta=true")
        self.check("stats_subtree.json", "/api/agents/omp/o-root/main/stats", "scope=subtree")

    def test_graph_matches_its_example(self):
        body = self.check("graph.json", "/api/sessions/claude/s-main/graph")
        self.assertIsInstance(body["rev"], int)
        self.check("graph.json", "/api/sessions/omp/o-root/graph")

    def test_graph_while_indexing_is_200(self):
        store = Store(StoreConfig(claude_root=os.path.join(self.root, "claude"),
                                  omp_root=os.path.join(self.root, "omp")))
        store.scan()
        store.refresh()
        status, body = api_mod.Api(store).handle("/api/sessions/claude/s-main/graph", "")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["indexed"])

    def test_events_tool_name_equals_its_example(self):
        status, body = self.api.handle("/api/agents/claude/s-main/a2222222222222222/events",
                                       "toolName=Bash")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, load_example("events_page_tool_filter.json"))

    def test_events_pagination_walks_backwards(self):
        base = "/api/agents/claude/s-main/main/events"
        status, page = self.api.handle(base, "limit=5&includeMeta=true")
        seen = [e["seq"] for e in page["items"]]
        while page["hasBefore"]:
            status, page = self.api.handle(base, "limit=5&includeMeta=true&before=%d" % page["fromSeq"])
            seen = [e["seq"] for e in page["items"]] + seen
        self.assertEqual(seen, list(range(page["total"])))


def read_sse_message(resp):
    """Read one SSE message (skipping comments) from an http.client response."""
    fields = {}
    while True:
        line = resp.fp.readline().decode("utf-8")
        if line == "":
            raise EOFError("stream closed")
        line = line.rstrip("\n")
        if line == "":
            if fields:
                if "data" in fields:
                    fields["data"] = json.loads(fields["data"])
                return fields
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        fields[name] = value[1:] if value.startswith(" ") else value


if __name__ == "__main__":
    unittest.main()
