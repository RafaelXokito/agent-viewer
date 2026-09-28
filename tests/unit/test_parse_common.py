"""WS1: safe line decoding, ParseStats, previews and timestamps."""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
import loader  # noqa: E402,F401  (puts the repo root on sys.path)

from agent_viewer.model import format_ts, parse_ts  # noqa: E402
from agent_viewer.parse_common import (  # noqa: E402
    MAX_SKIPPED_SAMPLES, PREVIEW_CHARS, ParseStats, decode_line, preview_text)


class DecodeLineTest(unittest.TestCase):
    def setUp(self):
        self.stats = ParseStats(file="/x/f.jsonl")

    def test_valid_object_is_returned_and_counted(self):
        record = decode_line(b'{"type":"user","a":1}', 1, 0, self.stats)
        self.assertEqual(record, {"type": "user", "a": 1})
        self.assertEqual((self.stats.lines, self.stats.parsed, self.stats.skipped), (1, 1, 0))

    def test_trailing_newline_and_carriage_return_are_stripped(self):
        self.assertEqual(decode_line(b'{"type":"x"}\r\n', 1, 0, self.stats), {"type": "x"})

    def test_bad_json_is_skipped_with_sample(self):
        self.assertIsNone(decode_line(b"this is not json", 2, 120, self.stats))
        self.assertEqual(self.stats.skipped, 1)
        self.assertEqual(self.stats.skipped_samples,
                         [{"file": "/x/f.jsonl", "lineNo": 2, "offset": 120, "reason": "not_json"}])

    def test_non_object_json_is_skipped(self):
        self.assertIsNone(decode_line(b"[1,2,3]", 3, 0, self.stats))
        self.assertEqual(self.stats.skipped_samples[0]["reason"], "not_object")

    def test_bad_utf8_is_skipped(self):
        self.assertIsNone(decode_line(b"\xff\xfe", 4, 0, self.stats))
        self.assertEqual(self.stats.skipped_samples[0]["reason"], "bad_utf8")

    def test_object_without_string_type_fails_required_field_check(self):
        self.assertIsNone(decode_line(b'{"type": 3}', 1, 0, self.stats))
        self.assertIsNone(decode_line(b'{"message": {}}', 2, 0, self.stats))
        self.assertEqual([s["reason"] for s in self.stats.skipped_samples], ["missing_type"] * 2)

    def test_blank_line_is_ignored_entirely(self):
        self.assertIsNone(decode_line(b"   ", 1, 0, self.stats))
        self.assertEqual((self.stats.lines, self.stats.skipped), (0, 0))

    def test_deeply_nested_json_is_skipped_not_raised(self):
        self.assertIsNone(decode_line(b"[" * 100000 + b"]" * 100000, 1, 0, self.stats))
        self.assertEqual(self.stats.skipped, 1)

    def test_skipped_samples_are_capped_but_counting_continues(self):
        for i in range(12):
            decode_line(b"nope", i + 1, i * 5, self.stats)
        self.assertEqual(self.stats.skipped, 12)
        self.assertEqual(len(self.stats.skipped_samples), MAX_SKIPPED_SAMPLES)
        self.assertEqual(self.stats.skipped_samples[-1]["lineNo"], 5)

    def test_samples_never_keep_line_content(self):
        decode_line(b"secret-token-123", 1, 0, self.stats)
        self.assertNotIn("secret", repr(self.stats.to_dict()))


class ParseStatsTest(unittest.TestCase):
    def test_to_dict_has_spec_fields(self):
        stats = ParseStats()
        stats.count_unknown("brand-new-type")
        stats.partial_tail = True
        self.assertEqual(stats.to_dict(), {
            "lines": 0, "parsed": 0, "skipped": 0, "skippedSamples": [],
            "unknownTypes": {"brand-new-type": 1}, "usageConflicts": 0, "partialTail": True})

    def test_merged_sums_counts_and_caps_samples(self):
        a, b = ParseStats(file="a"), ParseStats(file="b")
        for i in range(4):
            decode_line(b"x", i, 0, a)
            decode_line(b"y", i, 0, b)
        a.count_unknown("t")
        b.count_unknown("t")
        b.partial_tail = True
        merged = ParseStats.merged([a, b])
        self.assertEqual((merged.lines, merged.skipped), (8, 8))
        self.assertEqual(len(merged.skipped_samples), MAX_SKIPPED_SAMPLES)
        self.assertEqual(merged.unknown_types, {"t": 2})
        self.assertTrue(merged.partial_tail)
        self.assertEqual(a.lines, 4, "inputs are not mutated")


class PreviewTest(unittest.TestCase):
    def test_short_text_is_not_truncated(self):
        self.assertEqual(preview_text("abc"), ("abc", False))

    def test_long_text_is_cut_at_limit(self):
        text, truncated = preview_text("x" * (PREVIEW_CHARS + 5))
        self.assertEqual(len(text), PREVIEW_CHARS)
        self.assertTrue(truncated)

    def test_none_gives_empty(self):
        self.assertEqual(preview_text(None), ("", False))


class TimestampTest(unittest.TestCase):
    def test_parse_and_format_round_trip_with_milliseconds(self):
        dt = parse_ts("2026-09-25T10:00:01.100Z")
        self.assertEqual(dt, datetime(2026, 9, 25, 10, 0, 1, 100000, tzinfo=timezone.utc))
        self.assertEqual(format_ts(dt), "2026-09-25T10:00:01.100Z")

    def test_offsets_and_missing_fraction_are_accepted(self):
        self.assertEqual(format_ts(parse_ts("2026-09-25T12:00:00+02:00")), "2026-09-25T10:00:00.000Z")
        self.assertEqual(format_ts(parse_ts("2026-09-25T10:00:00.123456789Z")), "2026-09-25T10:00:00.123Z")

    def test_invalid_values_give_none(self):
        for value in (None, "", "yesterday", "2026-13-45T99:00:00Z", 12, [], {}):
            self.assertIsNone(parse_ts(value), value)
        self.assertIsNone(format_ts(None))


if __name__ == "__main__":
    unittest.main()
