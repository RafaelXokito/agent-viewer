"""WS2: FileTail - partial lines across reads, growth, shrink reset, inode change, chunk limit."""
import os
import shutil
import tempfile
import time
import unittest

from agent_viewer.tail import FileTail


class FileTailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="av-tail-")
        self.path = os.path.join(self.dir, "t.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def append(self, data):
        with open(self.path, "ab") as fh:
            fh.write(data)

    def raws(self, result):
        return [line.raw for line in result.lines]

    def test_reads_complete_lines_with_offsets_and_numbers(self):
        self.append(b'{"a":1}\n{"b":2}\n')
        result = FileTail(self.path).poll()
        self.assertEqual(self.raws(result), [b'{"a":1}', b'{"b":2}'])
        self.assertEqual([l.offset for l in result.lines], [0, 8])
        self.assertEqual([l.line_no for l in result.lines], [1, 2])
        self.assertFalse(result.reset)

    def test_partial_line_is_held_back_until_completed(self):
        tail = FileTail(self.path)
        self.append(b'{"a":1}\n{"b":')
        first = tail.poll()
        self.assertEqual(self.raws(first), [b'{"a":1}'])
        self.assertTrue(tail.partial_tail)
        self.assertEqual(tail.pending_bytes, b'{"b":')
        self.append(b'2}\n')
        second = tail.poll()
        self.assertEqual(self.raws(second), [b'{"b":2}'])
        self.assertEqual(second.lines[0].offset, 8)
        self.assertEqual(second.lines[0].line_no, 2)
        self.assertFalse(tail.partial_tail)

    def test_offsets_seek_to_line_start(self):
        self.append(b'x\n' * 3 + b'{"long":"' + b"y" * 100 + b'"}\n')
        tail = FileTail(self.path)
        for line in tail.poll().lines:
            with open(self.path, "rb") as fh:
                fh.seek(line.offset)
                self.assertEqual(fh.read(line.length), line.raw)

    def test_no_growth_returns_nothing(self):
        self.append(b'{"a":1}\n')
        tail = FileTail(self.path)
        tail.poll()
        result = tail.poll()
        self.assertEqual(result.lines, [])
        self.assertFalse(result.changed)

    def test_growth_returns_only_new_lines(self):
        tail = FileTail(self.path)
        self.append(b"1\n")
        tail.poll()
        self.append(b"2\n3\n")
        result = tail.poll()
        self.assertEqual(self.raws(result), [b"2", b"3"])
        self.assertTrue(result.changed)

    def test_blank_lines_are_skipped_but_numbered(self):
        self.append(b"1\n\n3\n")
        result = FileTail(self.path).poll()
        self.assertEqual(self.raws(result), [b"1", b"3"])
        self.assertEqual([l.line_no for l in result.lines], [1, 3])

    def test_shrink_resets_and_reindexes_from_zero(self):
        tail = FileTail(self.path)
        self.append(b"aaaa\nbbbb\n")
        tail.poll()
        with open(self.path, "r+b") as fh:  # test-only truncation of a temp file
            fh.truncate(0)
        self.append(b"c\n")
        result = tail.poll()
        self.assertTrue(result.reset)
        self.assertEqual(self.raws(result), [b"c"])
        self.assertEqual(result.lines[0].offset, 0)
        self.assertEqual(result.lines[0].line_no, 1)

    def test_inode_change_resets(self):
        tail = FileTail(self.path)
        self.append(b"old-1\nold-2\n")
        tail.poll()
        other = os.path.join(self.dir, "other")
        with open(other, "wb") as fh:
            fh.write(b"new-1\nnew-2\nnew-3\n")
        os.replace(other, self.path)
        result = tail.poll()
        self.assertTrue(result.reset)
        self.assertEqual(self.raws(result), [b"new-1", b"new-2", b"new-3"])

    def test_chunk_limit_splits_reads(self):
        self.append(b"".join(b"%04d\n" % i for i in range(10)))  # 50 bytes
        tail = FileTail(self.path, max_read=12)
        first = tail.poll()
        self.assertEqual(self.raws(first), [b"0000", b"0001"])
        self.assertTrue(first.more)
        seen = self.raws(first)
        while True:
            result = tail.poll()
            seen += self.raws(result)
            if not result.more:
                break
        self.assertEqual(seen, [b"%04d" % i for i in range(10)])

    def test_line_longer_than_chunk_accumulates(self):
        self.append(b"z" * 30 + b"\n")
        tail = FileTail(self.path, max_read=8)
        seen = []
        for _ in range(10):
            seen += self.raws(tail.poll())
        self.assertEqual(seen, [b"z" * 30])

    def test_missing_file_keeps_state(self):
        tail = FileTail(self.path)
        self.append(b"1\n")
        tail.poll()
        os.remove(self.path)
        result = tail.poll()
        self.assertTrue(result.missing)
        self.assertFalse(result.reset)
        self.assertEqual(tail.offset, 2)

    def test_file_not_yet_existing_is_missing(self):
        result = FileTail(self.path).poll()
        self.assertTrue(result.missing)
        self.assertEqual(result.lines, [])

    def test_same_size_newer_mtime_rereads_first_line(self):
        self.append(b'{"type":"title","title":"A"}\n{"x":1}\n')
        tail = FileTail(self.path)
        tail.poll()
        with open(self.path, "r+b") as fh:  # simulate the Oh My Pi in-place title rewrite
            fh.write(b'{"type":"title","title":"B"}')
        future = time.time() + 5
        os.utime(self.path, (future, future))
        result = tail.poll()
        self.assertEqual(result.lines, [])
        self.assertEqual(result.first_line, b'{"type":"title","title":"B"}')
        self.assertTrue(result.changed)

    def test_reset_method_rewinds(self):
        self.append(b"1\n2\n")
        tail = FileTail(self.path)
        tail.poll()
        tail.reset()
        self.assertEqual(self.raws(tail.poll()), [b"1", b"2"])


if __name__ == "__main__":
    unittest.main()
