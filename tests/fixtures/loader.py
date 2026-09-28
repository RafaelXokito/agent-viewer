"""Fixture loader for agent-viewer tests (SPEC 13.2).

`copy_fixtures()` copies the committed `claude/` and `omp/` fixture trees to a
fresh temporary directory, replaces `__FIXTURE_ROOT__` with that directory and
inserts the invalid UTF-8 line with `make_binary_line.py`. Tests never touch
the committed fixtures.

`index_roots()` is a small test-only stand-in for the WS2 store: it discovers
files, feeds every complete line to an `AgentState`, decides statuses and
builds the tree. It is used by the WS1 suites and by manual real-data checks.
"""
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field

FIXTURES_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(FIXTURES_DIR))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if FIXTURES_DIR not in sys.path:
    sys.path.insert(0, FIXTURES_DIR)

from make_binary_line import insert_binary_line  # noqa: E402

from agent_viewer.accumulate import AgentState  # noqa: E402
from agent_viewer.claude_parser import ClaudeParser  # noqa: E402
from agent_viewer.discovery import discover, omp_session_id_from_stem, read_sidecar  # noqa: E402
from agent_viewer.omp_parser import OmpParser  # noqa: E402
from agent_viewer.status import StatusConfig, decide_status, signals_for_agent  # noqa: E402
from agent_viewer.tree import build_tree  # noqa: E402

PLACEHOLDER = "__FIXTURE_ROOT__"
SOURCE_DIRS = ("claude", "omp")


def copy_fixtures():
    """Return the path of a fresh, writable copy of the fixture roots."""
    root = tempfile.mkdtemp(prefix="agent-viewer-fixtures-")
    for name in SOURCE_DIRS:
        shutil.copytree(os.path.join(FIXTURES_DIR, name), os.path.join(root, name))
    for dirpath, _dirs, files in os.walk(os.path.join(root, "omp")):
        for name in files:
            if name.endswith(".jsonl"):
                _replace_placeholder(os.path.join(dirpath, name), root)
    insert_binary_line(root)
    return root


def remove_fixtures(root):
    shutil.rmtree(root, ignore_errors=True)


def _replace_placeholder(path, root):
    with open(path, "rb") as handle:
        data = handle.read()
    if PLACEHOLDER.encode() in data:
        with open(path, "wb") as handle:
            handle.write(data.replace(PLACEHOLDER.encode(), root.encode()))


def feed_file(state, path):
    """Feed every complete line of `path` to `state`; hold back a partial tail."""
    with open(path, "rb") as handle:
        data = handle.read()
    offset = 0
    line_no = 0
    while True:
        end = data.find(b"\n", offset)
        if end < 0:
            break
        line_no += 1
        state.feed_line(data[offset:end], offset, line_no)
        offset = end + 1
    state.parse_stats.partial_tail = offset < len(data)
    return state


@dataclass
class Index:
    files: list
    states: dict = field(default_factory=dict)
    nodes: list = field(default_factory=list)
    links: list = field(default_factory=list)
    tree: object = None


def _session_id(source_file):
    if source_file.source == "omp":
        return omp_session_id_from_stem(source_file.session_hint)
    return source_file.session_hint


def index_roots(claude_root=None, omp_root=None, now=None, config=None, only=None):
    """Index both roots the way the WS2 store would, returning an `Index`."""
    now = time.time() if now is None else now
    config = config or StatusConfig()
    files = discover(claude_root, omp_root)
    if only is not None:
        files = [f for f in files if only(f)]
    sidecars = {}
    transcripts = []
    for sf in files:
        key = (sf.source, sf.project, sf.session_hint, sf.agent_hint)
        if sf.kind in ("meta", "forked_skill"):
            sidecars[key + (sf.kind,)] = read_sidecar(sf.path)
        else:
            transcripts.append(sf)
    index = Index(files=files)
    for sf in transcripts:
        sid = _session_id(sf)
        agent_key = "%s:%s:%s" % (sf.source, sid, sf.agent_hint)
        parser = ClaudeParser(agent_key) if sf.source == "claude" else OmpParser(agent_key)
        state = AgentState(agent_key, parser, file=sf.path)
        feed_file(state, sf.path)
        index.states[agent_key] = (sf, state)
    session_signals = {}
    for sf, state in index.states.values():
        session_signals.setdefault((sf.source, _session_id(sf)), []).extend(state.status_signals)
    for agent_key, (sf, state) in sorted(index.states.items()):
        base = (sf.source, sf.project, sf.session_hint, sf.agent_hint)
        meta = sidecars.get(base + ("meta",))
        forked = sidecars.get(base + ("forked_skill",))
        signals = signals_for_agent(
            sf.agent_hint, state.status_signals,
            session_signals[(sf.source, _session_id(sf))], meta)
        status = decide_status(signals, os.stat(sf.path).st_mtime, now,
                               sf.agent_hint == "main", config)
        index.nodes.append(state.node_input(
            project=sf.project, status=status, meta=meta, forked_skill=forked))
        index.links.extend(state.links)
    index.tree = build_tree(index.nodes, index.links)
    return index
