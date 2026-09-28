"""Scan, poll and index scheduling (SPEC 7.1, 7.2).

One background thread drives the store: a scan every SCAN_INTERVAL, a poll of
hot files every POLL_INTERVAL (all files at the scan interval), chunked
indexing of the queue in the time left over, and a refresh that publishes
changes. Using one thread means a FileTail is never read concurrently.
"""
import threading
import time

SCAN_INTERVAL = 5.0
POLL_INTERVAL = 1.0
INDEX_BUDGET = 0.25
TICK = 0.1


class Watcher:
    def __init__(self, store, scan_interval=SCAN_INTERVAL, poll_interval=POLL_INTERVAL,
                 index_budget=INDEX_BUDGET, clock=time.time):
        self.store = store
        self.scan_interval = scan_interval
        self.poll_interval = poll_interval
        self.index_budget = index_budget
        self.clock = clock
        self.next_scan = 0.0
        self.next_poll = 0.0
        self._stop = threading.Event()
        self._thread = None
        self.errors = 0

    def run_once(self, now=None):
        """One scheduling step; returns True when indexing work remains."""
        now = self.clock() if now is None else now
        full_poll = now >= self.next_scan
        if full_poll:
            self.store.scan()
            self.next_scan = now + self.scan_interval
        if full_poll or now >= self.next_poll:
            self.store.poll(now, hot_only=not full_poll)
            self.next_poll = now + self.poll_interval
            self.store.refresh(now)
        more = self.store.index_step(self.index_budget)
        if more:
            self.store.refresh(now)
        elif full_poll:
            self.store.refresh(now)
        return more

    def _run(self):
        # The first pass indexes everything once before settling into the loop,
        # refreshing after each budget slice so the list fills progressively.
        while not self._stop.is_set():
            try:
                more = self.run_once()
            except Exception:  # noqa: BLE001 - the watcher must never die
                self.errors += 1
                more = False
            if not more:
                self._stop.wait(TICK)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="watcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
