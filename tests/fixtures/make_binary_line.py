"""Insert one invalid UTF-8 line into the broken Claude fixture.

The committed `claude/-tmp-broken/s-broken.jsonl` cannot hold the raw bytes
readably, so the loader runs this on its temporary copy: it inserts the line
`\\xff\\xfe` after line 3. Usage: python3 make_binary_line.py <fixture-copy-root>
"""
import os
import sys

BROKEN_FILE = os.path.join("claude", "-tmp-broken", "s-broken.jsonl")
BINARY_LINE = b"\xff\xfe"
INSERT_AFTER_LINE = 3


def insert_binary_line(root):
    path = os.path.join(root, BROKEN_FILE)
    with open(path, "rb") as handle:
        data = handle.read()
    lines = data.split(b"\n")
    lines.insert(INSERT_AFTER_LINE, BINARY_LINE)
    with open(path, "wb") as handle:
        handle.write(b"\n".join(lines))
    return path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: make_binary_line.py <fixture-copy-root>")
    insert_binary_line(sys.argv[1])
