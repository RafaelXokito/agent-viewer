"""WS2: SSE framing, fan-out scoping, bounded queues and coalescing (part of 13.3 test_store scope)."""
import json
import unittest

from agent_viewer import sse


def parse(message):
    fields = {}
    for line in message.decode("utf-8").strip("\n").split("\n"):
        name, _, value = line.partition(": ")
        fields[name] = value
    fields["data"] = json.loads(fields["data"])
    return fields


class FrameTest(unittest.TestCase):
    def test_frame_layout(self):
        raw = sse.frame("tree.update", {"a": "x\ny"}, 7)
        self.assertTrue(raw.endswith(b"\n\n"))
        self.assertEqual(raw.count(b"\n"), 4, "data must stay on one line")
        got = parse(raw)
        self.assertEqual((got["id"], got["event"], got["data"]), ("7", "tree.update", {"a": "x\ny"}))

    def test_hello_carries_retry_and_version(self):
        hub = sse.Hub(clock=lambda: 0.0)
        got = parse(hub.hello())
        self.assertEqual(got["retry"], "2000")
        self.assertEqual(got["data"], {"serverTime": "1970-01-01T00:00:00.000Z", "version": "1"})


class HubTest(unittest.TestCase):
    def test_list_events_reach_everyone_and_agent_events_are_scoped(self):
        hub = sse.Hub()
        plain = hub.subscribe()
        scoped = hub.subscribe({"claude:s"})
        hub.publish("session.upsert", {"key": "claude:s"})
        hub.publish("events.append", {"agentKey": "claude:s:main"}, session_key="claude:s")
        hub.publish("events.append", {"agentKey": "claude:t:main"}, session_key="claude:t")
        self.assertEqual(plain.queue.qsize(), 1)
        self.assertEqual(scoped.queue.qsize(), 2)
        ids = [int(parse(scoped.next(0))["id"]) for _ in range(2)]
        self.assertLess(ids[0], ids[1])

    def test_overflow_marks_client_and_stops_queueing(self):
        hub = sse.Hub(maxsize=3)
        client = hub.subscribe()
        for i in range(10):
            hub.publish("session.upsert", {"i": i})
        messages = [client.next(0) for _ in range(4)]
        self.assertIs(messages[-1], sse.OVERFLOW)
        self.assertTrue(client.overflowed)
        self.assertIsNone(client.next(0))

    def test_close_wakes_clients(self):
        hub = sse.Hub()
        client = hub.subscribe()
        hub.close()
        self.assertIs(client.next(0), sse.CLOSE)
        late = hub.subscribe()
        self.assertIs(late.next(0), sse.CLOSE)

    def test_watched_sessions_is_the_union_of_live_subscriptions(self):
        hub = sse.Hub()
        first = hub.subscribe({"claude:a"})
        hub.subscribe({"claude:a", "omp:b"})
        hub.subscribe()
        self.assertEqual(hub.watched_sessions(), {"claude:a", "omp:b"})
        hub.unsubscribe(first)
        self.assertEqual(hub.watched_sessions(), {"claude:a", "omp:b"})

    def test_heartbeat_is_a_comment(self):
        self.assertEqual(sse.HEARTBEAT, b": hb\n\n")


class GraphEventTest(unittest.TestCase):
    """graph.update is opt-in per client (docs/FEATURE-graph-canvas.md 8.6)."""

    def test_only_graph_clients_of_the_session_receive_graph_update(self):
        hub = sse.Hub()
        graph = hub.subscribe({"claude:s"}, graph=True)
        tree_only = hub.subscribe({"claude:s"})
        other = hub.subscribe({"claude:t"}, graph=True)
        hub.publish("graph.update", {"sessionKey": "claude:s", "rev": 1}, session_key="claude:s")
        self.assertEqual(parse(graph.next(0))["event"], "graph.update")
        self.assertIsNone(tree_only.next(0))
        self.assertIsNone(other.next(0))

    def test_graph_flag_does_not_change_other_events(self):
        hub = sse.Hub()
        graph = hub.subscribe({"claude:s"}, graph=True)
        hub.publish("session.upsert", {"key": "claude:x"})
        hub.publish("tree.update", {"sessionKey": "claude:s"}, session_key="claude:s")
        hub.publish("tree.update", {"sessionKey": "claude:t"}, session_key="claude:t")
        self.assertEqual([parse(graph.next(0))["event"] for _ in range(2)],
                         ["session.upsert", "tree.update"])
        self.assertIsNone(graph.next(0))

    def test_graph_flag_without_sessions_receives_list_events_only(self):
        hub = sse.Hub()
        client = hub.subscribe(graph=True)
        hub.publish("graph.update", {"sessionKey": "claude:s"}, session_key="claude:s")
        hub.publish("session.upsert", {"key": "claude:s"})
        self.assertEqual(parse(client.next(0))["event"], "session.upsert")
        self.assertIsNone(client.next(0))

    def test_watched_graph_sessions(self):
        hub = sse.Hub()
        first = hub.subscribe({"claude:a"}, graph=True)
        hub.subscribe({"claude:b"})
        hub.subscribe({"claude:a", "omp:c"}, graph=True)
        self.assertEqual(hub.watched_graph_sessions(), {"claude:a", "omp:c"})
        self.assertEqual(hub.watched_sessions(), {"claude:a", "claude:b", "omp:c"})
        hub.unsubscribe(first)
        self.assertEqual(hub.watched_graph_sessions(), {"claude:a", "omp:c"})

    def test_cap_graph_keeps_small_graphs_inline(self):
        payload = {"sessionKey": "claude:s", "rev": 3, "graph": {"nodes": [], "edges": []}}
        self.assertEqual(sse.cap_graph(payload), payload)

    def test_cap_graph_overflows_large_graphs(self):
        big = {"nodes": [{"id": "x" * 1024}] * (sse.MAX_INLINE_GRAPH_BYTES // 1024 + 1)}
        payload = {"sessionKey": "claude:s", "rev": 4, "graph": big}
        self.assertEqual(sse.cap_graph(payload),
                         {"sessionKey": "claude:s", "rev": 4, "graph": None, "overflow": True})

    def test_constants(self):
        self.assertEqual(sse.GRAPH_MIN_INTERVAL, 1.0)
        self.assertEqual(sse.MAX_INLINE_GRAPH_BYTES, 512 * 1024)


class CoalescerTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.hub = sse.Hub(clock=lambda: self.now)
        self.client = self.hub.subscribe({"claude:s"})
        self.co = sse.Coalescer(self.hub, clock=lambda: self.now, delay=0.25)

    def drain(self):
        out = []
        while True:
            message = self.client.next(0)
            if message is None:
                return out
            out.append(parse(message))

    def append(self, first, last):
        events = [{"seq": s} for s in range(first, last + 1)]
        self.co.submit("append:claude:s:main", "events.append",
                       {"agentKey": "claude:s:main", "fromSeq": first, "toSeq": last,
                        "events": events},
                       session_key="claude:s", merge=sse.merge_append)

    def test_holds_until_delay_then_sends_one_merged_message(self):
        self.append(0, 1)
        self.now += 0.1
        self.append(2, 2)
        self.co.flush()
        self.assertEqual(self.drain(), [])
        self.now += 0.2
        self.co.flush()
        got = self.drain()
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["data"]["fromSeq"], got[0]["data"]["toSeq"]), (0, 2))
        self.assertEqual([e["seq"] for e in got[0]["data"]["events"]], [0, 1, 2])

    def test_more_than_100_events_are_sent_as_a_range(self):
        self.append(0, 60)
        self.append(61, 130)
        self.co.flush(force=True)
        data = self.drain()[0]["data"]
        self.assertEqual((data["fromSeq"], data["toSeq"], data["events"]), (0, 130, []))

    def test_last_value_wins_without_merge(self):
        for n in range(3):
            self.co.submit("upsert:claude:x", "session.upsert", {"key": "claude:x", "n": n})
        self.co.flush(force=True)
        got = self.drain()
        self.assertEqual([m["data"]["n"] for m in got], [2])

    def test_stats_rate_limited_to_one_per_interval(self):
        def stats(n):
            self.co.submit("stats:k", "stats.update", {"key": "k", "n": n}, session_key="claude:s",
                           min_interval=2.0)
        stats(1)
        self.now += 0.3
        self.co.flush()
        self.assertEqual(len(self.drain()), 1)
        stats(2)
        self.now += 0.3
        self.co.flush()
        self.assertEqual(self.drain(), [], "second update inside 2 s must wait")
        stats(3)
        self.now += 1.4
        self.co.flush()
        self.assertEqual(self.drain(), [], "still inside 2 s of the first send")
        self.now += 0.4
        self.co.flush()
        got = self.drain()
        self.assertEqual([m["data"]["n"] for m in got], [3])

    def test_reset_key_drops_pending(self):
        self.append(0, 0)
        self.co.reset_key("append:claude:s:main")
        self.co.flush(force=True)
        self.assertEqual(self.drain(), [])


if __name__ == "__main__":
    unittest.main()
