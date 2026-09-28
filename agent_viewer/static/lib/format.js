// Pure formatting helpers: numbers, durations, times, truncation.
// No DOM access, so every function here is unit-tested with `node --test`.

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const ELLIPSIS = '…';
const PLACEHOLDER = '-';

function isNumber(n) {
  return typeof n === 'number' && Number.isFinite(n);
}

/** Integer with thousands separators, for example 979520 -> "979,520". */
export function formatInt(n) {
  if (!isNumber(n)) return PLACEHOLDER;
  return Math.round(n).toLocaleString('en-US');
}

const COMPACT_UNITS = [
  [1e3, 'k'],
  [1e6, 'M'],
  [1e9, 'G'],
];

/** Short number for dense columns, for example 979520 -> "979.5k". */
export function formatCompact(n) {
  if (!isNumber(n)) return PLACEHOLDER;
  const abs = Math.abs(n);
  if (abs < 1000) return String(Math.round(n));
  for (let i = 0; i < COMPACT_UNITS.length; i += 1) {
    const [size, unit] = COMPACT_UNITS[i];
    const scaled = n / size;
    const text = Math.abs(scaled) >= 100 ? scaled.toFixed(0) : scaled.toFixed(1);
    const isLastUnit = i === COMPACT_UNITS.length - 1;
    if (Math.abs(Number(text)) < 1000 || isLastUnit) {
      return text.replace(/\.0$/, '') + unit;
    }
  }
  return String(n);
}

function pad2(n) {
  return String(n).padStart(2, '0');
}

/** Human duration, for example 777 -> "777ms", 192000 -> "3m 12s". */
export function formatDuration(ms) {
  if (!isNumber(ms) || ms < 0) return PLACEHOLDER;
  if (ms < SECOND) return `${Math.round(ms)}ms`;
  if (ms < MINUTE) return `${(ms / SECOND).toFixed(1).replace(/\.0$/, '')}s`;
  const totalSeconds = Math.floor(ms / SECOND);
  if (ms < HOUR) return `${Math.floor(totalSeconds / 60)}m ${pad2(totalSeconds % 60)}s`;
  const totalMinutes = Math.floor(ms / MINUTE);
  if (ms < DAY) return `${Math.floor(totalMinutes / 60)}h ${pad2(totalMinutes % 60)}m`;
  const totalHours = Math.floor(ms / HOUR);
  return `${Math.floor(totalHours / 24)}d ${totalHours % 24}h`;
}

/** Milliseconds since the epoch for an ISO timestamp, or NaN. */
export function toMs(ts) {
  if (ts == null || ts === '') return NaN;
  return Date.parse(ts);
}

/** Milliseconds between two ISO timestamps, or null when either is missing. */
export function durationBetween(startTs, endTs) {
  const start = toMs(startTs);
  const end = toMs(endTs);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return Math.max(0, end - start);
}

/** Relative time, for example "5m ago". Future times read as "just now". */
export function formatRelative(ts, nowMs) {
  const then = toMs(ts);
  if (Number.isNaN(then)) return PLACEHOLDER;
  const diff = nowMs - then;
  if (diff < 5 * SECOND) return 'just now';
  if (diff < MINUTE) return `${Math.floor(diff / SECOND)}s ago`;
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)}m ago`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)}h ago`;
  return `${Math.floor(diff / DAY)}d ago`;
}

/** Local wall-clock time "HH:MM:SS". */
export function formatClock(ts) {
  const ms = toMs(ts);
  if (Number.isNaN(ms)) return PLACEHOLDER;
  const d = new Date(ms);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/** Local date and time "YYYY-MM-DD HH:MM". */
export function formatDateTime(ts) {
  const ms = toMs(ts);
  if (Number.isNaN(ms)) return PLACEHOLDER;
  const d = new Date(ms);
  const date = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  return `${date} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** Truncate to at most `max` characters, ending with an ellipsis when cut. */
export function truncate(text, max) {
  if (text == null) return '';
  const s = String(text);
  if (s.length <= max) return s;
  if (max <= 1) return ELLIPSIS;
  return s.slice(0, max - 1) + ELLIPSIS;
}

/** First line of a text, trimmed. */
export function firstLine(text) {
  if (text == null) return '';
  const s = String(text).trim();
  const nl = s.indexOf('\n');
  return nl === -1 ? s : s.slice(0, nl).trim();
}

/** First `n` characters of an id, for dense display. */
export function shortId(id, n = 8) {
  if (id == null) return '';
  return String(id).slice(0, n);
}

/** Project label: the last two path parts of cwd, else the fallback. */
export function projectLabel(cwd, fallback) {
  if (cwd) {
    const parts = String(cwd).split('/').filter(Boolean);
    if (parts.length > 0) return parts.slice(-2).join('/');
  }
  return fallback == null ? PLACEHOLDER : String(fallback);
}

/** Byte size, for example 2048 -> "2.0 KB". */
export function formatBytes(n) {
  if (!isNumber(n) || n < 0) return PLACEHOLDER;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** US dollar amount as recorded by the source, or the placeholder. */
export function formatUsd(n) {
  if (!isNumber(n)) return PLACEHOLDER;
  return `$${n.toFixed(n < 1 ? 4 : 2)}`;
}
