"""CLI: python3 -m agent_viewer [--port 8765] [--claude-root ...] [--omp-root ...] (SPEC 10)."""
import argparse
import os
import re
import signal
import sys
import threading
import webbrowser

from . import api as api_mod
from . import server as server_mod
from . import sse

DEFAULT_PORT = 8765
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text):
    """'30m', '10s', '2h', '1d' or plain seconds to seconds."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", text or "")
    if not match:
        raise argparse.ArgumentTypeError("invalid duration %r (use e.g. 30m, 10s, 2h)" % text)
    return float(match.group(1)) * _UNITS[match.group(2)]


def build_parser():
    parser = argparse.ArgumentParser(prog="agent_viewer",
                                     description="Local read-only viewer for AI agent transcripts.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="0 picks a free port")
    parser.add_argument("--claude-root", default="~/.claude/projects")
    parser.add_argument("--omp-root", default="~/.omp/agent/sessions")
    parser.add_argument("--idle-window", type=parse_duration, default=parse_duration("30m"))
    parser.add_argument("--stale-after", type=parse_duration, default=parse_duration("10m"))
    parser.add_argument("--open", action="store_true", help="open the browser")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    from .store import Store, StoreConfig
    from .watcher import Watcher

    config = StoreConfig(claude_root=os.path.expanduser(args.claude_root),
                         omp_root=os.path.expanduser(args.omp_root),
                         idle_window=args.idle_window, stale_after=args.stale_after)
    hub = sse.Hub()
    coalescer = sse.Coalescer(hub)
    store = Store(config, coalescer, watched_sessions=hub.watched_sessions,
                  watched_graph_sessions=hub.watched_graph_sessions)
    watcher = Watcher(store)
    httpd = server_mod.make_server(api_mod.Api(store), hub, args.port, STATIC_DIR,
                                   on_stream_sessions=store.prioritize)
    url = "http://127.0.0.1:%d" % httpd.server_address[1]
    print("listening on %s" % url, flush=True)

    stopping = threading.Event()

    def stop(*_):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    coalescer.start()
    watcher.start()
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    finally:
        watcher.stop()
        coalescer.stop()
        hub.close()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
