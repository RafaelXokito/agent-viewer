"""Relation graph of one session for the graph canvas (docs/FEATURE-graph-canvas.md 8).

`build_graph` is a pure function over plain dicts: no I/O, no imports from the
store, server or SSE modules, and deterministic for the same inputs whatever
their dict insertion order. `rev` is added by the store, not here.
"""

RELATION_TOOLS = frozenset(("Agent", "Task", "task", "SendMessage", "Skill"))
TOOLS_PER_AGENT = 50
CHAIN_HOPS = 5
MAX_NODES = 2000
DEFAULT_LIMITS = {"toolsPerAgent": TOOLS_PER_AGENT, "chainHops": CHAIN_HOPS, "maxNodes": MAX_NODES}
EDGE_KINDS = ("spawn", "resume", "continued_in", "uses_tool", "uses_skill")
ERROR_KEYS = ("toolErrors", "apiErrors", "aborted")
SUMMARY_FIELDS = ("title", "status", "startedAt", "lastActivityAt", "agentCount")
TREE_FIELDS = ("agentId", "agentType", "description", "name", "status", "linkedBy",
               "forkedSkill", "spawnedAt", "lastActivityAt")
FLAG_FIELDS = ("missing", "isFork", "stoppedByUser")


def build_graph(session_key, tree, summaries, usage_by_agent, limits=None):
    """Graph JSON of feature doc 8.2 without `rev`.

    `tree` is the SPEC 9.4 dict, `summaries` maps session keys to SPEC 9.2
    dicts, `usage_by_agent` maps agent keys to
    `{"tools", "skills", "errors", "pending"}` from agent-scope stats.
    """
    limits = dict(DEFAULT_LIMITS, **(limits or {}))
    tree = tree or {}
    tree_nodes = tree.get("nodes") or {}
    summaries = summaries or {}
    usage_by_agent = usage_by_agent or {}
    root_key = tree.get("rootKey") or session_key + ":main"
    order = agent_order(tree_nodes, root_key)
    agents = [agent_node(session_key, tree_nodes[k], root_key, usage_by_agent.get(k))
              for k in order]
    sessions = chain_nodes(session_key, summaries, limits["chainHops"])
    usage = [usage_nodes(k, usage_by_agent.get(k), limits["toolsPerAgent"]) for k in order]
    agent_by_id = {a["id"]: a for a in agents}
    for key, (_, omitted, omitted_calls) in zip(order, usage):
        agent_by_id[key]["toolsOmitted"] = omitted
        agent_by_id[key]["toolsOmittedCalls"] = omitted_calls
    extras = [node for nodes, _, _ in usage for node in nodes]
    extras, truncated = cap_nodes(extras, len(agents) + len(sessions), limits["maxNodes"],
                                  agent_by_id)
    nodes = agents + sessions + extras
    root_present = root_key in agent_by_id
    edges = order_edges(spawn_edges(agents, agent_by_id, tree_nodes) + resume_edges(order, tree_nodes, agent_by_id)
                        + chain_edges(sessions, root_key if root_present else None)
                        + usage_edges(extras), nodes)
    summary = summaries.get(session_key) or {}
    return {"sessionKey": session_key, "rootKey": root_key,
            "indexed": bool(summary.get("indexed")), "truncated": truncated,
            "limits": {k: limits[k] for k in DEFAULT_LIMITS},
            "nodes": nodes, "edges": edges}


# ------------------------------------------------------------------ nodes

def agent_order(tree_nodes, root_key):
    """Depth-first from the root in children order, then unreachable keys sorted (flattenTree)."""
    order, seen = [], set()

    def visit(start):
        stack = [start]
        while stack:
            key = stack.pop()
            if key in seen or key not in tree_nodes:
                continue
            seen.add(key)
            order.append(key)
            children = tree_nodes[key].get("children") or []
            stack.extend(reversed([c for c in children if c in tree_nodes]))

    visit(root_key)
    for key in sorted(k for k in tree_nodes if k not in seen):
        visit(key)
    return order


def agent_node(session_key, node, root_key, usage):
    key = node.get("key")
    tools = (usage or {}).get("tools") or {}
    errors = (usage or {}).get("errors") or {}
    out = {"id": key, "kind": "agent", "key": key, "agentId": node.get("agentId"),
           "sessionKey": session_key, "parentId": node.get("parentKey"),
           "depth": node.get("depth") or 0, "isRoot": key == root_key}
    for name in TREE_FIELDS:
        out[name] = node.get(name)
    out["warnings"] = list(node.get("warnings") or [])
    for name in FLAG_FIELDS:
        out[name] = bool(node.get(name))
    out["models"] = list(node.get("models") or [])
    out["tokensTotal"] = node.get("tokensTotal") or 0
    out["eventCount"] = node.get("eventCount") or 0
    out["childCount"] = len(node.get("children") or [])
    out["resumeCount"] = len(node.get("resumes") or [])
    out["toolCalls"] = sum(_calls(entry) for entry in tools.values())
    out["relationToolCalls"] = sum(_calls(tools[name]) for name in tools if name in RELATION_TOOLS)
    out["errors"] = {name: errors.get(name) or 0 for name in ERROR_KEYS}
    out["toolsOmitted"] = 0
    out["toolsOmittedCalls"] = 0
    return _ordered(out)


# The field order of the example, so a serialized graph reads like graph.json.
AGENT_FIELD_ORDER = ("id", "kind", "key", "agentId", "sessionKey", "parentId", "depth", "isRoot",
                     "agentType", "description", "name", "status", "linkedBy", "warnings",
                     "missing", "isFork", "stoppedByUser", "forkedSkill", "models", "tokensTotal",
                     "eventCount", "spawnedAt", "lastActivityAt", "childCount", "resumeCount",
                     "toolCalls", "relationToolCalls", "errors", "toolsOmitted",
                     "toolsOmittedCalls")


def _ordered(agent):
    return {name: agent[name] for name in AGENT_FIELD_ORDER}


def _calls(entry):
    return (entry or {}).get("calls") or 0


def usage_nodes(agent_key, usage, tools_per_agent):
    """Tool then skill nodes of one agent, and the tools left out by the per-agent cap."""
    usage = usage or {}
    tools = usage.get("tools") or {}
    pending = usage.get("pending") or {}
    ranked = sorted((name for name in tools if name not in RELATION_TOOLS),
                    key=lambda name: (-_calls(tools[name]), name))
    kept, rest = ranked[:tools_per_agent], ranked[tools_per_agent:]
    nodes = [tool_node(agent_key, name, tools[name], pending.get(name) or 0) for name in kept]
    skills = usage.get("skills") or {}
    for name in sorted(skills, key=lambda n: (-((skills[n] or {}).get("count") or 0), n)):
        nodes.append(skill_node(agent_key, name, skills[name] or {}))
    return nodes, len(rest), sum(_calls(tools[name]) for name in rest)


def tool_node(agent_key, name, entry, pending):
    entry = entry or {}
    return {"id": "tool|%s|%s" % (agent_key, name), "kind": "tool", "ownerId": agent_key,
            "name": name, "calls": entry.get("calls") or 0, "errors": entry.get("errors") or 0,
            "pending": pending, "totalDurationMs": entry.get("totalDurationMs") or 0}


def skill_node(agent_key, name, entry):
    return {"id": "skill|%s|%s" % (agent_key, name), "kind": "skill", "ownerId": agent_key,
            "name": name, "count": entry.get("count") or 0, "via": dict(entry.get("via") or {})}


def cap_nodes(extras, fixed, max_nodes, agent_by_id):
    """Drop tool and skill nodes from the end until the total fits; agents and sessions stay."""
    room = max(max_nodes - fixed, 0)
    if len(extras) <= room:
        return extras, False
    for node in extras[room:]:
        if node["kind"] == "tool":
            owner = agent_by_id[node["ownerId"]]
            owner["toolsOmitted"] += 1
            owner["toolsOmittedCalls"] += node["calls"]
    return extras[:room], True


def chain_nodes(session_key, summaries, hops):
    """Session nodes of the continuation chain: predecessors, then successors, by hops."""
    seen = {session_key}
    current = summaries.get(session_key) or {}
    nodes = []
    for field, relation in (("continuedFrom", "predecessor"), ("continuedIn", "successor")):
        summary = current
        for hop in range(1, hops + 1):
            key = summary.get(field)
            if not key or key in seen:
                break
            seen.add(key)
            summary = summaries.get(key)
            nodes.append(session_node(key, relation, hop, summary))
            if summary is None:
                break
    return nodes


def session_node(key, relation, hops, summary):
    node = {"id": "session|" + key, "kind": "session", "sessionKey": key,
            "relation": relation, "hops": hops}
    for name in SUMMARY_FIELDS:
        node[name] = summary.get(name) if summary else None
    node["missing"] = summary is None
    return node


# ------------------------------------------------------------------ edges

def _edge(kind, source, target, **extra):
    edge = {"id": "%s|%s|%s" % (kind, source, target), "kind": kind, "from": source, "to": target}
    edge.update(extra)
    return edge


def spawn_edges(agents, agent_by_id, tree_nodes):
    """One per agent whose parent is a node of this graph."""
    return [_edge("spawn", a["parentId"], a["id"], linkedBy=a["linkedBy"],
                  toolCallId=tree_nodes[a["id"]].get("spawnToolCallId"), timestamp=a["spawnedAt"])
            for a in agents if a["parentId"] is not None and a["parentId"] in agent_by_id]


def resume_edges(order, tree_nodes, agent_by_id):
    """Tree `resumes` grouped per (resuming agent, resumed agent)."""
    edges = []
    position = {key: index for index, key in enumerate(order)}
    for target in order:
        groups = {}
        for resume in tree_nodes[target].get("resumes") or []:
            source = (resume or {}).get("byAgentKey")
            if source in agent_by_id:
                groups.setdefault(source, []).append(resume)
        for source in sorted(groups, key=position.get):
            items = sorted(groups[source], key=lambda r: (r.get("timestamp") is None,
                                                          r.get("timestamp") or ""))
            stamps = [r["timestamp"] for r in items if r.get("timestamp")]
            edges.append(_edge("resume", source, target, count=len(items),
                               toolCallIds=[r["toolCallId"] for r in items if r.get("toolCallId")],
                               lastAt=max(stamps) if stamps else None))
    return edges


def chain_edges(sessions, root_id):
    """continued_in edges from each session to the next later one, through the root card."""
    if root_id is None:
        return []
    preds = [n["id"] for n in sessions if n["relation"] == "predecessor"]
    succs = [n["id"] for n in sessions if n["relation"] == "successor"]
    line = list(reversed(preds)) + [root_id] + succs
    return [_edge("continued_in", line[i], line[i + 1]) for i in range(len(line) - 1)]


def usage_edges(extras):
    edges = []
    for node in extras:
        if node["kind"] == "tool":
            edges.append(_edge("uses_tool", node["ownerId"], node["id"], count=node["calls"],
                               errors=node["errors"]))
        else:
            edges.append(_edge("uses_skill", node["ownerId"], node["id"], count=node["count"]))
    return edges


def order_edges(edges, nodes):
    """By kind, then by the position of the `to` node, then of the `from` node."""
    position = {node["id"]: index for index, node in enumerate(nodes)}
    rank = {kind: index for index, kind in enumerate(EDGE_KINDS)}
    return sorted(edges, key=lambda e: (rank[e["kind"]], position.get(e["to"], len(position)),
                                        position.get(e["from"], len(position))))
