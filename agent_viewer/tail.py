"""Byte-offset incremental reader for append-only JSONL files (SPEC 7.1).

Files are only ever opened with mode "rb". A trailing line without a newline
is held back in a buffer until it is completed.
"""
import os
from dataclasses import dataclass, field
from typing import List, Optional

MAX_READ_CHUNK = 8 * 1024 * 1024
FIRST_LINE_MAX = 64 * 1024


@dataclass(frozen=True)
class Line:
    raw: bytes        # without the trailing newline
    offset: int       # byte offset of the line start
    length: int       # len(raw)
    line_no: int      # 1-based


@dataclass
class TailResult:
    lines: List[Line] = field(default_factory=list)
    reset: bool = False           # file shrank or was replaced; reindexed from 0
    missing: bool = False         # file is gone; state kept
    changed: bool = False         # size or mtime moved since the last poll
    more: bool = False            # chunk limit hit, call poll again
    first_line: Optional[bytes] = None   # re-read on equal size but newer mtime
    size: int = 0
    mtime: float = 0.0


class FileTail:
    def __init__(self, path, max_read=MAX_READ_CHUNK):
        self.path = path
        self.max_read = max_read
        self.reset()

    def reset(self):
        self.read_offset = 0      # bytes read from the file
        self._buffer = b""        # trailing partial line
        self.line_no = 0
        self.inode = None
        self.size = 0
        self.mtime = 0.0

    @property
    def offset(self):
        """Bytes consumed into complete lines."""
        return self.read_offset - len(self._buffer)

    @property
    def partial_tail(self):
        return bool(self._buffer)

    @property
    def pending_bytes(self):
        return self._buffer

    def poll(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return TailResult(missing=True, size=self.size, mtime=self.mtime)
        result = TailResult(size=st.st_size, mtime=st.st_mtime)
        replaced = self.inode is not None and st.st_ino != self.inode
        if replaced or st.st_size < self.read_offset:
            self.reset()
            result.reset = True
        result.changed = (result.reset or st.st_size != self.size
                          or st.st_mtime != self.mtime)
        self.inode = st.st_ino
        grew = st.st_size > self.read_offset
        if grew:
            self._read(st.st_size, result)
        elif result.changed and self.read_offset > 0:
            result.first_line = self._first_line()
        self.size, self.mtime = st.st_size, st.st_mtime
        return result

    def _read(self, size, result):
        want = min(self.max_read, size - self.read_offset)
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.read_offset)
                chunk = fh.read(want)
        except OSError:
            result.missing = True
            return
        start = self.offset
        self.read_offset += len(chunk)
        data = self._buffer + chunk
        parts = data.split(b"\n")
        self._buffer = parts.pop()
        for raw in parts:
            self.line_no += 1
            if raw:
                result.lines.append(Line(raw, start, len(raw), self.line_no))
            start += len(raw) + 1
        result.more = self.read_offset < size

    def _first_line(self):
        try:
            with open(self.path, "rb") as fh:
                head = fh.read(FIRST_LINE_MAX)
        except OSError:
            return None
        return head.split(b"\n", 1)[0]
