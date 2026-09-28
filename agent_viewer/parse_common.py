"""Safe line decoding, ParseStats and small value coercions shared by the parsers (SPEC 5.9, 6.3)."""
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

PREVIEW_CHARS = 2000
FINAL_STOP_REASONS = frozenset(("end_turn", "stop_sequence", "stop"))
MAX_SKIPPED_SAMPLES = 5


@dataclass
class ParseStats:
    """Per-file parse counters. `file` labels skipped samples; `partial_tail` is set by the reader."""
    lines: int = 0
    parsed: int = 0
    skipped: int = 0
    skipped_samples: List[dict] = field(default_factory=list)
    unknown_types: Dict[str, int] = field(default_factory=dict)
    usage_conflicts: int = 0
    partial_tail: bool = False
    file: Optional[str] = None

    def skip(self, reason, line_no, offset, file=None):
        """Count a skipped line; keep at most MAX_SKIPPED_SAMPLES samples, never the line content."""
        self.skipped += 1
        if len(self.skipped_samples) < MAX_SKIPPED_SAMPLES:
            self.skipped_samples.append(
                {"file": file if file is not None else self.file, "lineNo": line_no, "offset": offset, "reason": reason})

    def count_unknown(self, record_type):
        self.unknown_types[record_type] = self.unknown_types.get(record_type, 0) + 1

    def to_dict(self):
        return {"lines": self.lines, "parsed": self.parsed, "skipped": self.skipped,
                "skippedSamples": [dict(s) for s in self.skipped_samples],
                "unknownTypes": dict(self.unknown_types), "usageConflicts": self.usage_conflicts,
                "partialTail": self.partial_tail}

    @classmethod
    def merged(cls, stats_list):
        """Sum several ParseStats into a new one (session totals); inputs are not modified."""
        out = cls()
        for s in stats_list:
            out.lines += s.lines
            out.parsed += s.parsed
            out.skipped += s.skipped
            out.usage_conflicts += s.usage_conflicts
            out.partial_tail = out.partial_tail or s.partial_tail
            for name, count in s.unknown_types.items():
                out.unknown_types[name] = out.unknown_types.get(name, 0) + count
            room = MAX_SKIPPED_SAMPLES - len(out.skipped_samples)
            out.skipped_samples.extend(dict(x) for x in s.skipped_samples[:max(room, 0)])
        return out


@dataclass
class ParseResult:
    """Everything one record contributes (SPEC 14.5.1), plus a few counters the stats need.

    `unknown_type` is set when the record type is not in the parser's known
    set; `error` is set when the parser failed on this record (the caller counts
    it as skipped). The trailing counters feed Stats directly.
    """
    events: list = field(default_factory=list)
    tool_call_updates: list = field(default_factory=list)
    usages: list = field(default_factory=list)
    skills: list = field(default_factory=list)
    links: list = field(default_factory=list)
    session_meta: dict = field(default_factory=dict)
    status_signals: list = field(default_factory=list)
    unknown_type: Optional[str] = None
    error: Optional[str] = None
    slash_commands: list = field(default_factory=list)
    subagent_types: list = field(default_factory=list)
    active_duration_ms: Optional[int] = None
    api_errors: int = 0
    aborted: int = 0


def decode_line(raw, line_no, offset, stats):
    """Decode one complete JSONL line into a dict with a string `type`, or None.

    Blank lines are ignored and not counted. Invalid UTF-8, invalid JSON, a
    non-object value or a missing `type` are counted as skipped with a reason.
    """
    raw = raw.rstrip(b"\r\n")
    if not raw.strip():
        return None
    stats.lines += 1
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        stats.skip("bad_utf8", line_no, offset)
        return None
    try:
        record = json.loads(text)
    except (ValueError, RecursionError):
        stats.skip("not_json", line_no, offset)
        return None
    if not isinstance(record, dict):
        stats.skip("not_object", line_no, offset)
        return None
    if not isinstance(record.get("type"), str):
        stats.skip("missing_type", line_no, offset)
        return None
    stats.parsed += 1
    return record


def preview_text(text):
    """Return `(preview, truncated)` bounded by PREVIEW_CHARS."""
    if not text:
        return "", False
    if len(text) > PREVIEW_CHARS:
        return text[:PREVIEW_CHARS], True
    return text, False


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def as_str(value):
    return value if isinstance(value, str) and value else None


def as_int(value):
    """Integer token counts: ints and floats are accepted, booleans and others give 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) < 1e18:
        return int(value)
    return 0


def as_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def dumps_preview(value):
    """JSON text of a tool input for previews; never raises."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (ValueError, TypeError, RecursionError):
        return ""


def image_summary(block):
    """Replace image data by `{type, mediaType, bytes}` (SPEC 9.5)."""
    source = as_dict(block.get("source"))
    data = source.get("data", block.get("data"))
    size = len(data) * 3 // 4 - (data.endswith("==") + data.endswith("=")) if isinstance(data, str) else 0
    return {"type": "image", "mediaType": as_str(source.get("media_type")) or as_str(block.get("mimeType")), "bytes": size}


def content_text(content):
    """Flatten a tool result / message content (string or block list) into text."""
    if isinstance(content, str):
        return content
    parts = []
    for block in as_list(content):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def content_blocks(content):
    """Sanitized block list for full content: text kept, images summarised, others reduced to their type."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for block in as_list(content):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            out.append({"type": "text", "text": block.get("text") if isinstance(block.get("text"), str) else ""})
        elif kind == "image":
            out.append(image_summary(block))
        else:
            out.append({"type": kind if isinstance(kind, str) else "unknown"})
    return out
