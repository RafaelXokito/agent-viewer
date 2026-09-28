"""Find transcript files and sidecars under the Claude and Oh My Pi roots (SPEC 4, 7.1, 11).

The only WS1 module that touches the filesystem: it lists directories and
reads the small JSON sidecars. Every path is resolved with `realpath` and must
stay under its root; anything else is skipped and optionally reported.
"""
import json
import os
from dataclasses import dataclass

MAX_SIDECAR_BYTES = 1 << 20
SUBAGENT_PREFIX = "agent-"
TRANSCRIPT_SUFFIX = ".jsonl"
META_SUFFIX = ".meta.json"
FORKED_SKILL_SUFFIX = ".forked-skill.json"


@dataclass(frozen=True)
class SourceFile:
    source: str        # "claude" | "omp"
    kind: str          # "main" | "subagent" | "meta" | "forked_skill" | "omp_root" | "omp_child"
    path: str          # absolute, realpath under its root
    project: str       # slug or project directory name
    session_hint: str  # Claude: owning sessionId from the path; Omp: root file stem
    agent_hint: str    # "main", Claude agentId, or Omp relative stem path


def discover(claude_root, omp_root, skipped=None):
    """All transcript files and sidecars under both roots, sorted by (source, path).

    `skipped`, when a list, receives `(path, reason)` for entries that were
    ignored for safety (a symlink resolving outside its root).
    """
    found = []
    if claude_root:
        found.extend(_discover_claude(claude_root, skipped))
    if omp_root:
        found.extend(_discover_omp(omp_root, skipped))
    return sorted(found, key=lambda f: (f.source, f.path))


def read_sidecar(path):
    """Parse a small JSON object file; None on any error, never raises."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_SIDECAR_BYTES + 1)
        if len(data) > MAX_SIDECAR_BYTES:
            return None
        value = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def omp_session_id_from_stem(stem):
    """Oh My Pi root files are named `<ISO-timestamp>_<sessionId>`; return the id part (the stem if no `_`)."""
    _, sep, rest = stem.partition("_")
    return rest if sep and rest else stem


def _discover_claude(root, skipped):
    real_root = _real_dir(root)
    if real_root is None:
        return []
    out = []
    for project in _entries(real_root):
        if not project.is_dir(follow_symlinks=False):
            continue
        project_dir = project.path
        for entry in _entries(project_dir):
            if entry.name.endswith(TRANSCRIPT_SUFFIX) and _is_file(entry):
                path = _inside(entry.path, real_root, skipped)
                if path:
                    out.append(SourceFile("claude", "main", path, project.name, entry.name[:-len(TRANSCRIPT_SUFFIX)], "main"))
            elif entry.is_dir(follow_symlinks=False):
                out.extend(_claude_subagents(entry, project.name, real_root, skipped))
    return out


def _claude_subagents(session_dir, project, real_root, skipped):
    out = []
    for entry in _entries(os.path.join(session_dir.path, "subagents")):
        name = entry.name
        if not name.startswith(SUBAGENT_PREFIX) or not _is_file(entry):
            continue
        for suffix, kind in ((TRANSCRIPT_SUFFIX, "subagent"), (META_SUFFIX, "meta"), (FORKED_SKILL_SUFFIX, "forked_skill")):
            if name.endswith(suffix):
                agent_id = name[len(SUBAGENT_PREFIX):-len(suffix)]
                path = _inside(entry.path, real_root, skipped) if agent_id else None
                if path:
                    out.append(SourceFile("claude", kind, path, project, session_dir.name, agent_id))
                break
    return out


def _discover_omp(root, skipped):
    real_root = _real_dir(root)
    if real_root is None:
        return []
    out = []
    for project in _entries(real_root):
        if not project.is_dir(follow_symlinks=False):
            continue
        for entry in _entries(project.path):
            if entry.name.endswith(TRANSCRIPT_SUFFIX) and _is_file(entry):
                path = _inside(entry.path, real_root, skipped)
                if path:
                    out.append(SourceFile("omp", "omp_root", path, project.name, entry.name[:-len(TRANSCRIPT_SUFFIX)], "main"))
            elif entry.is_dir(follow_symlinks=False):
                out.extend(_omp_children(entry.path, project.name, entry.name, "", real_root, skipped))
    return out


def _omp_children(directory, project, stem, prefix, real_root, skipped):
    out = []
    for entry in _entries(directory):
        if entry.is_dir(follow_symlinks=False):
            out.extend(_omp_children(entry.path, project, stem, prefix + entry.name + "/", real_root, skipped))
        elif entry.name.endswith(TRANSCRIPT_SUFFIX) and _is_file(entry):
            path = _inside(entry.path, real_root, skipped)
            if path:
                out.append(SourceFile("omp", "omp_child", path, project, stem,
                                      prefix + entry.name[:-len(TRANSCRIPT_SUFFIX)]))
    return out


def _real_dir(root):
    real = os.path.realpath(os.path.expanduser(root))
    return real if os.path.isdir(real) else None


def _entries(directory):
    try:
        with os.scandir(directory) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return []


def _is_file(entry):
    try:
        return entry.is_file()
    except OSError:
        return False


def _inside(path, real_root, skipped):
    real = os.path.realpath(path)
    if real == real_root or not real.startswith(real_root + os.sep):
        if skipped is not None:
            skipped.append((path, "outside_root"))
        return None
    return real
