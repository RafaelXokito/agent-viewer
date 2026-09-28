#!/usr/bin/env python3
"""Contract mock server for WS3 (SPEC 14.4, 14.5.2).

Serves contract/examples/ on the real routes, through the real router, host
check, security headers and static file handling, plus a scripted SSE feed
replayed in a loop from examples/sse_script.jsonl.

    python3 contract/mock_server.py --port 8766
"""
import argparse
import copy
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from agent_viewer import api as api_mod  # noqa: E402
from agent_viewer import server as server_mod  # noqa: E402
from agent_viewer import sse  # noqa: E402

EXAMPLES = os.path.join(HERE, "examples")
STATIC = os.path.join(REPO, "agent_viewer", "static")


def load(name):
    with open(os.path.join(EXAMPLES, name), encoding="utf-8") as fh:
        return json.load(fh)


class ExampleStore:
    """Implements the store protocol of agent_viewer.api from the example files."""

    def __init__(self):
        self.examples = {name[:-5]: load(name) for name in os.listdir(EXAMPLES)
                         if name.endswith(".json")}
        self._sessions = {s["key"]: s for s in self.examples["sessions"]["items"]}
        detail = self.examples["session_detail"]
        self._trees = {detail["key"]: self.examples["tree"]}
        self._agents = {}
        for tree in self._trees.values():
            for key, node in tree["nodes"].items():
                agent = dict(node, file="/example/%s.jsonl" % node["agentId"],
                             toolCalls=0, skills=[])
                self._agents[key] = agent
        example_agent = self.examples["agent"]
        self._agents[example_agent["key"]] = example_agent
        self._events = {self.examples["events_page"]["agentKey"]:
                        self.examples["events_page"]["items"]}

    # test helpers
    def set_indexed(self, key, indexed):
        self._sessions[key] = dict(self._sessions[key], indexed=indexed)

    def add_agent_alias(self, key):
        self._agents[key] = dict(self.examples["agent"], key=key,
                                 agentId=key.split(":", 2)[2])

    # store protocol
    def health(self):
        return copy.deepcopy(self.examples["health"])

    def projects(self):
        return copy.deepcopy(self.examples["projects"]["items"])

    def sessions(self):
        return [dict(s) for s in self._sessions.values()]

    def session(self, key):
        found = self._sessions.get(key)
        return dict(found) if found else None

    def tree(self, key):
        if key in self._trees:
            return copy.deepcopy(self._trees[key])
        summary = self._sessions.get(key)
        if summary is None:
            return None
        root = summary["rootAgentKey"]
        node = {"key": root, "agentId": "main", "sessionId": summary["sessionId"],
                "parentKey": None, "depth": 0, "agentType": "main", "description": None,
                "name": None, "linkedBy": "root", "spawnToolCallId": None,
                "spawnedAt": summary["startedAt"], "lastActivityAt": summary["lastActivityAt"],
                "status": summary["status"], "stoppedByUser": False, "isFork": False,
                "forkedSkill": None, "resumes": [], "models": [], "eventCount": 0,
                "missing": False, "children": [], "warnings": [],
                "tokensTotal": (summary.get("tokens") or {}).get("total", 0)}
        return {"sessionKey": key, "rootKey": root, "nodes": {root: node}}

    def session_stats(self, key):
        if key not in self._sessions:
            return None
        return copy.deepcopy(self.examples["stats_subtree"])

    def agent(self, agent_key):
        if agent_key in self._agents:
            return copy.deepcopy(self._agents[agent_key])
        session_key = agent_key.rsplit(":", 1)[0]
        tree = self.tree(session_key)
        node = tree and tree["nodes"].get(agent_key)
        if not node:
            return None
        return dict(node, file="/example/main.jsonl", toolCalls=0, skills=[])

    def agent_stats(self, agent_key, scope):
        if self.agent(agent_key) is None:
            return None
        return copy.deepcopy(self.examples["stats_agent" if scope == "agent" else "stats_subtree"])

    def events(self, agent_key, before, after, limit, kinds, include_meta, tool_name=None):
        if self.agent(agent_key) is None:
            return None
        every = self._events.get(agent_key, [])
        items = [e for e in every
                 if (kinds is None or e["kind"] in kinds)
                 and (include_meta or e["kind"] != "meta" or (kinds and "meta" in kinds))
                 and (tool_name is None or e.get("toolName") == tool_name)]

        def fetch(before_seq, after_seq, count):
            if after_seq is not None:
                return [e for e in items if e["seq"] > after_seq][:count]
            chosen = [e for e in items if before_seq is None or e["seq"] < before_seq]
            return chosen[-count:]

        return api_mod.events_page(agent_key, fetch, before, after, limit, len(every))

    def event_full(self, agent_key, seq):
        example = self.examples["event_full"]
        for event in self._events.get(agent_key, []):
            if event["seq"] == seq:
                if seq == example["seq"]:
                    return copy.deepcopy(example)
                return {"seq": seq, "kind": event["kind"], "toolCallId": event["toolCallId"],
                        "isError": event["isError"], "content": event["preview"],
                        "input": None, "blocks": [{"type": "text", "text": event["preview"]}]}
        return None

    def graph(self, key):
        """graph.json for its session, with `indexed` following set_indexed; None otherwise."""
        example = self.examples["graph"]
        if key != example["sessionKey"] or key not in self._sessions:
            return None
        return dict(copy.deepcopy(example), indexed=bool(self._sessions[key].get("indexed")))

    def prioritize(self, keys):
        pass


def load_script():
    with open(os.path.join(EXAMPLES, "sse_script.jsonl"), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def session_of(event, data):
    """Session key that scopes an agent-level SSE event."""
    if event in ("tree.update", "graph.update"):
        return data["sessionKey"]
    key = data.get("agentKey") or data.get("key") or ""
    return ":".join(key.split(":")[:2])


def replay(hub, stop, loop=True):
    script = load_script()
    while not stop.is_set():
        for step in script:
            if stop.wait(step["delayMs"] / 1000.0):
                return
            hub.publish(step["event"], step["data"], session_key=session_of(step["event"], step["data"]))
        if not loop:
            return


def main(argv=None):
    parser = argparse.ArgumentParser(description="agent-viewer contract mock server")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--static", default=STATIC, help="frontend directory to serve")
    parser.add_argument("--no-script", action="store_true", help="do not replay the SSE script")
    args = parser.parse_args(argv)

    hub = sse.Hub()
    api = api_mod.Api(ExampleStore())
    httpd = server_mod.make_server(api, hub, args.port, args.static)
    stop = threading.Event()
    if not args.no_script:
        threading.Thread(target=replay, args=(hub, stop), daemon=True).start()
    print("listening on http://127.0.0.1:%d" % httpd.server_address[1], flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        hub.close()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    time.tzset() if hasattr(time, "tzset") else None
    sys.exit(main())
