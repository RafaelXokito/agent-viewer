"""Tree reconstruction (SPEC 6): a pure, deterministic `build_tree(nodes, links)`.

Nodes and links are sorted before use, so any input order yields the same
tree. Claude parents come from the `.meta.json` sidecar first and transcript
spawn links second (6.1); Oh My Pi parents come from `session.parentSession`,
then the folder layout (6.2). Missing parents get placeholder nodes, cycles
are broken, and every anomaly is recorded as a node warning.
"""
import os

from .model import Agent, Resume, Tree, agent_key, sort_ts, split_key

MAIN = "main"


class _Node:
    __slots__ = ("input", "key", "source", "session", "agent_id", "project", "parent", "linked_by", "warnings",
                 "agent_type", "description", "name", "spawn_tool_call_id", "spawned_at", "spawn_seen_in",
                 "resumes", "missing")

    def __init__(self, source, session, agent_id, node_input=None, project=""):
        self.input = node_input
        self.source, self.session, self.agent_id = source, session, agent_id
        self.key = agent_key(source, session, agent_id)
        self.project = node_input.project if node_input is not None else project
        self.parent = None
        self.linked_by = "root" if agent_id == MAIN else "orphan"
        self.warnings = []
        self.agent_type = MAIN if agent_id == MAIN else None
        self.description = self.name = self.spawn_tool_call_id = self.spawn_seen_in = None
        self.spawned_at = node_input.started_at if node_input is not None else None
        self.resumes = []
        self.missing = node_input.missing if node_input is not None else True


def build_tree(nodes, links):
    """Build the agent forest of every session present in `nodes` (SPEC 6)."""
    builder = _Builder(nodes, links)
    builder.link_claude()
    builder.link_omp()
    builder.add_placeholders()
    builder.apply_resumes()
    builder.break_cycles()
    return builder.result()


class _Builder:
    def __init__(self, nodes, links):
        self.work = {}
        for n in sorted(nodes, key=lambda n: (str(n.key), n.missing, str(n.file))):
            if not all(isinstance(v, str) and v for v in (n.source, n.session_id, n.agent_id)):
                continue
            if n.key not in self.work:
                self.work[n.key] = _Node(n.source, n.session_id, n.agent_id, n)
        self.projects = {}
        for node in self.work.values():
            self.projects.setdefault((node.source, node.session), node.project)
        for node in list(self.work.values()):
            self._ensure_main(node.source, node.session)
        self.by_agent = {}
        for key in sorted(self.work):
            node = self.work[key]
            self.by_agent.setdefault((node.source, node.agent_id), []).append(key)
        valid = [l for l in links if split_key(l.from_key) and isinstance(l.to_agent_id, str) and l.to_agent_id]
        valid.sort(key=lambda l: l.sort_key())
        self.spawns, self.resumes = {}, []
        for link in valid:
            source = split_key(link.from_key)[0]
            if link.kind == "spawn":
                self.spawns.setdefault((source, link.to_agent_id), []).append(link)
            elif link.kind == "resume":
                self.resumes.append(link)
        self.placeholders = {}

    # -- helpers ----------------------------------------------------------

    def _ensure_main(self, source, session):
        key = agent_key(source, session, MAIN)
        if key not in self.work:
            self.work[key] = _Node(source, session, MAIN, project=self.projects.get((source, session), ""))
            self.work[key].warnings.append("missing")
        return key

    def _main_of(self, node):
        return agent_key(node.source, node.session, MAIN)

    def _find(self, source, session, agent_id, project):
        """Node key of `agent_id`: in `session` first, then anywhere in the same project."""
        key = agent_key(source, session, agent_id)
        if key in self.work:
            return key
        for candidate in self.by_agent.get((source, agent_id), []):
            if self.work[candidate].project == project:
                return candidate
        return None

    def _parent_or_placeholder(self, key):
        if key not in self.work:
            self.placeholders[key] = True
        return key

    def _same_project(self, link, project):
        source, session, _agent = split_key(link.from_key)
        known = self.projects.get((source, session))
        return known is None or known == project

    # -- Claude (6.1) -----------------------------------------------------

    def link_claude(self):
        for key in sorted(self.work):
            node = self.work[key]
            if node.source == "claude" and node.agent_id != MAIN and node.input is not None:
                self._link_claude_node(node)

    def _link_claude_node(self, node):
        meta = node.input.meta if isinstance(node.input.meta, dict) else None
        candidates = [l for l in self.spawns.get(("claude", node.agent_id), []) if self._same_project(l, node.project)]
        own = [l for l in candidates if split_key(l.from_key)[1] == node.session]
        primary = own[0] if own else (candidates[0] if candidates else None)
        main = self._main_of(node)
        parent_id = _str(meta.get("parentAgentId")) if meta else None
        if meta is not None:
            node.linked_by = "meta"
            if parent_id:
                found = self._find("claude", node.session, parent_id, node.project)
                node.parent = found or self._parent_or_placeholder(agent_key("claude", node.session, parent_id))
            else:
                node.parent = main
            if primary is not None and primary.from_key != node.parent:
                if self._is_other_main(primary.from_key, node.session):
                    node.spawn_seen_in = primary.from_key
                else:
                    node.warnings.append("parent_mismatch")
        elif primary is not None:
            node.linked_by = "transcript"
            if self._is_other_main(primary.from_key, node.session):
                node.parent = main
                node.spawn_seen_in = primary.from_key
            else:
                node.parent = self._parent_or_placeholder(primary.from_key)
        else:
            node.parent = main
            node.warnings.append("orphan")
        tool_use_id = _str(meta.get("toolUseId")) if meta else None
        evidence_link = next((l for l in candidates if tool_use_id and l.tool_call_id == tool_use_id), primary)
        evidence = evidence_link.evidence if evidence_link is not None else {}
        node.agent_type = (_str(meta.get("agentType")) if meta else None) or _str(evidence.get("agentType"))
        node.description = (_str(meta.get("description")) if meta else None) or _str(evidence.get("description"))
        node.name = _str(meta.get("name")) if meta else None
        node.spawn_tool_call_id = tool_use_id or (evidence_link.tool_call_id if evidence_link is not None else None)
        if evidence_link is not None and evidence_link.timestamp is not None:
            node.spawned_at = evidence_link.timestamp

    @staticmethod
    def _is_other_main(from_key, session):
        _source, from_session, from_agent = split_key(from_key)
        return from_session != session and from_agent == MAIN

    # -- Oh My Pi (6.2) ---------------------------------------------------

    def link_omp(self):
        files = {}
        for key in sorted(self.work):
            node = self.work[key]
            if node.source == "omp" and node.input is not None and node.input.file:
                files.setdefault(os.path.normpath(node.input.file), key)
        for key in sorted(self.work):
            node = self.work[key]
            if node.source == "omp" and node.agent_id != MAIN and node.input is not None:
                self._link_omp_node(node, files)
            elif node.source not in ("claude", "omp") and node.agent_id != MAIN:
                node.parent = self._main_of(node)
                node.warnings.append("orphan")

    def _link_omp_node(self, node, files):
        parent_session = node.input.parent_session
        if isinstance(parent_session, str) and parent_session:
            found = files.get(os.path.normpath(parent_session))
            if found and found != node.key:
                node.parent, node.linked_by = found, "parentSession"
        if node.parent is None and node.input.file:
            found = files.get(os.path.dirname(os.path.normpath(node.input.file)) + ".jsonl")
            if found and found != node.key:
                node.parent, node.linked_by = found, "folder"
        if node.parent is None:
            node.parent, node.linked_by = self._main_of(node), "orphan"
            node.warnings.append("orphan")
        stem = node.agent_id.rsplit("/", 1)[-1]
        candidates = [l for l in self.spawns.get(("omp", stem), []) if split_key(l.from_key)[1] == node.session]
        link = next((l for l in candidates if l.from_key == node.parent), candidates[0] if candidates else None)
        if link is not None:
            node.agent_type = _str(link.evidence.get("agentType"))
            node.description = _str(link.evidence.get("description"))
            node.spawn_tool_call_id = link.tool_call_id
        node.name = stem

    # -- shared steps -----------------------------------------------------

    def add_placeholders(self):
        """Create `missing` nodes for parents that have no file (6.1 step 7), parented to their session main."""
        for key in sorted(self.placeholders):
            if key in self.work:
                continue
            source, session, agent_id = split_key(key)
            node = _Node(source, session, agent_id, project=self.projects.get((source, session), ""))
            node.warnings.extend(["missing"] if agent_id == MAIN else ["missing", "orphan"])
            node.parent = self._ensure_main(source, session) if agent_id != MAIN else None
            self.work[key] = node

    def apply_resumes(self):
        """Resume links append to the target's `resumes`; they never reparent (6.1 step 5)."""
        seen = set()
        for link in self.resumes:
            source, session, _agent = split_key(link.from_key)
            target = self._find(source, session, link.to_agent_id, self.projects.get((source, session)))
            if target is None:
                continue
            marker = (target, link.tool_call_id or (link.from_key, sort_ts(link.timestamp)))
            if marker in seen:
                continue
            seen.add(marker)
            self.work[target].resumes.append(Resume(link.from_key, link.tool_call_id, link.timestamp))

    def break_cycles(self):
        """Walk up from each node; the node whose parent edge closes a loop is reparented to its main (6.1 step 8)."""
        for key in sorted(self.work):
            visited, current = {key}, key
            while True:
                parent = self.work[current].parent
                if parent is None:
                    break
                if parent in visited or parent not in self.work:
                    node = self.work[current]
                    node.parent = self._ensure_main(node.source, node.session)
                    node.warnings.append("cycle" if parent in visited else "orphan")
                    break
                visited.add(parent)
                current = parent

    def _depths(self):
        depths = {}
        for key in sorted(self.work):
            chain, current = [], key
            while current is not None and current not in depths:
                chain.append(current)
                current = self.work[current].parent
            base = depths[current] if current is not None else -1
            for offset, item in enumerate(reversed(chain), start=1):
                depths[item] = base + offset
        return depths

    def result(self):
        depths = self._depths()
        children = {}
        for key in sorted(self.work):
            parent = self.work[key].parent
            if parent is not None:
                children.setdefault(parent, []).append(key)
        agents = {}
        for key in sorted(self.work):
            node = self.work[key]
            ordered = sorted(children.get(key, []), key=lambda k: (sort_ts(self.work[k].spawned_at), k))
            agents[key] = self._agent(node, depths[key], ordered)
        return Tree(nodes=agents)

    def _agent(self, node, depth, children):
        n = node.input
        meta = n.meta if n is not None and isinstance(n.meta, dict) else {}
        forked = n.forked_skill if n is not None and isinstance(n.forked_skill, dict) else {}
        warnings = list(node.warnings)
        spawn_depth = meta.get("spawnDepth")
        if isinstance(spawn_depth, int) and not isinstance(spawn_depth, bool) and spawn_depth != depth:
            warnings.append("depth_mismatch")
        if node.missing and "missing" not in warnings:
            warnings.append("missing")
        return Agent(
            key=node.key, agent_id=node.agent_id, session_id=node.session, source=node.source,
            parent_key=node.parent, depth=depth, agent_type=node.agent_type, description=node.description,
            name=node.name, linked_by=node.linked_by, spawn_tool_call_id=node.spawn_tool_call_id,
            spawned_at=node.spawned_at, last_activity_at=n.last_activity_at if n is not None else None,
            status=n.status if n is not None else "finished",
            stopped_by_user=meta.get("stoppedByUser") is True, is_fork=meta.get("isFork") is True,
            forked_skill=_str(forked.get("skillName")),
            resumes=tuple(sorted(node.resumes, key=lambda r: (sort_ts(r.timestamp), r.by_agent_key))),
            models=tuple(n.models) if n is not None else (), file=n.file if n is not None else "",
            event_count=n.event_count if n is not None else 0, missing=node.missing, children=tuple(children),
            warnings=tuple(warnings), tokens_total=n.tokens_total if n is not None else 0,
            spawn_seen_in=node.spawn_seen_in, project=node.project)


def _str(value):
    return value if isinstance(value, str) and value else None
