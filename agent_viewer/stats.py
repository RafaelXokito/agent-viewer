"""Stats aggregation (SPEC 5.8): merge agent stats into subtree stats."""
from dataclasses import replace

from .model import Stats, empty_errors, empty_tokens

TOKEN_KEYS = ("input", "output", "cacheRead", "cacheCreation", "reasoning")
MODEL_KEYS = ("messages", "input", "output", "cacheRead", "cacheCreation")
TOOL_KEYS = ("calls", "errors", "totalDurationMs")


def merge(stats):
    """Sum a list of Stats into a new `subtree` Stats; inputs are not modified."""
    tokens = empty_tokens()
    errors = empty_errors()
    by_model, tools, skills, slash, subagent_types = {}, {}, {}, {}, {}
    agents = {"total": 0, "maxDepth": 0, "running": 0}
    parse = {"lines": 0, "skipped": 0, "unknownTypes": {}}
    cost = active = started = last = None
    turns = 0
    for s in stats:
        for key in TOKEN_KEYS:
            tokens[key] += s.tokens.get(key, 0)
        for key in errors:
            errors[key] += s.errors.get(key, 0)
        _sum_nested(by_model, s.by_model, MODEL_KEYS)
        _sum_nested(tools, s.tools, TOOL_KEYS)
        for name, entry in s.skills.items():
            target = skills.setdefault(name, {"count": 0, "via": {}})
            target["count"] += entry["count"]
            for via, count in entry["via"].items():
                target["via"][via] = target["via"].get(via, 0) + count
        _sum_flat(slash, s.slash_commands)
        _sum_flat(subagent_types, s.subagent_types)
        agents["total"] += s.agents.get("total", 0)
        agents["running"] += s.agents.get("running", 0)
        agents["maxDepth"] = max(agents["maxDepth"], s.agents.get("maxDepth", 0))
        parse["lines"] += s.parse["lines"]
        parse["skipped"] += s.parse["skipped"]
        _sum_flat(parse["unknownTypes"], s.parse["unknownTypes"])
        if s.cost_usd is not None:
            cost = (cost or 0.0) + s.cost_usd
        if s.active_duration_ms is not None:
            active = (active or 0) + s.active_duration_ms
        if s.started_at is not None and (started is None or s.started_at < started):
            started = s.started_at
        if s.last_activity_at is not None and (last is None or s.last_activity_at > last):
            last = s.last_activity_at
        turns += s.turns
    tokens["total"] = tokens["input"] + tokens["output"] + tokens["cacheRead"] + tokens["cacheCreation"]
    return Stats(scope="subtree", tokens=tokens, cost_usd=cost, by_model=by_model, tools=tools, skills=skills,
                 slash_commands=slash, subagent_types=subagent_types, agents=agents, active_duration_ms=active,
                 errors=errors, turns=turns, parse=parse, started_at=started, last_activity_at=last)


def agent_stats(tree, key, stats):
    """Agent-scope Stats with the `agents` block filled from the tree node (its depth and status)."""
    node = tree.nodes.get(key)
    agents = {"total": 1, "maxDepth": node.depth if node else 0,
              "running": 1 if node is not None and node.status == "running" else 0}
    return _with_agents(stats, "agent", agents)


def subtree_stats(tree, root_key, stats_by_key):
    """Subtree Stats of `root_key`: merged stats of every node reachable from it, missing nodes included."""
    keys = tree.subtree_keys(root_key)
    merged = merge([stats_by_key[k] for k in keys if k in stats_by_key])
    agents = {"total": len(keys), "maxDepth": max((tree.nodes[k].depth for k in keys), default=0),
              "running": sum(1 for k in keys if tree.nodes[k].status == "running")}
    return _with_agents(merged, "subtree", agents)


def _with_agents(stats, scope, agents):
    return replace(stats, scope=scope, agents=agents)


def _sum_nested(target, source, keys):
    for name, entry in source.items():
        bucket = target.setdefault(name, {k: 0 for k in keys})
        for k in keys:
            bucket[k] += entry.get(k, 0)


def _sum_flat(target, source):
    for name, count in source.items():
        target[name] = target.get(name, 0) + count
