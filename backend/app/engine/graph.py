"""Workflow graph model and static validation.

Definition format (persisted in ``workflow_versions.definition``)::

    {"nodes": [{"id", "type", "name", "config", "settings", "position": {"x", "y"}}],
     "edges": [{"source", "source_handle", "target"}]}
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

from pydantic import ValidationError

from app.engine.expressions import has_template
from app.engine.registry import get_spec

NODE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
MAX_NODES = 200
NO_LOOP_TYPES = {"logic.delay", "logic.wait", "logic.approval"}


class Graph:
    def __init__(self, definition: dict):
        self.nodes: dict[str, dict] = {n["id"]: n for n in definition.get("nodes", [])}
        self.order: list[str] = [n["id"] for n in definition.get("nodes", [])]
        self.edges: list[dict] = []
        self.out: dict[str, list[dict]] = defaultdict(list)
        self.inc: dict[str, list[dict]] = defaultdict(list)
        for i, e in enumerate(definition.get("edges", [])):
            edge = {"i": str(i), "source": e["source"], "target": e["target"], "handle": e.get("source_handle") or "out"}
            self.edges.append(edge)
            self.out[edge["source"]].append(edge)
            self.inc[edge["target"]].append(edge)
        # loop bodies: nodes reachable from a loop's "each" handle; owner = innermost enclosing loop
        self.bodies: dict[str, set[str]] = {}
        for nid, n in self.nodes.items():
            if n.get("type") == "logic.loop":
                self.bodies[nid] = self._reach([e["target"] for e in self.out[nid] if e["handle"] == "each"])
        self.owner: dict[str, str | None] = {}
        for nid in self.nodes:
            enclosing = [l for l, body in self.bodies.items() if nid in body]
            self.owner[nid] = min(enclosing, key=lambda l: len(self.bodies[l])) if enclosing else None

    def _reach(self, starts: list[str]) -> set[str]:
        seen: set[str] = set()
        dq = deque(s for s in starts if s in self.nodes)
        while dq:
            cur = dq.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            dq.extend(e["target"] for e in self.out[cur] if e["target"] in self.nodes)
        return seen

    def is_trigger(self, nid: str) -> bool:
        spec = get_spec(self.nodes[nid].get("type", ""))
        return bool(spec and spec.is_trigger)

    def triggers(self) -> list[str]:
        return [nid for nid in self.order if self.is_trigger(nid)]

    def has_cycle(self) -> bool:
        indeg = {nid: 0 for nid in self.nodes}
        for e in self.edges:
            if e["target"] in indeg and e["source"] in indeg:
                indeg[e["target"]] += 1
        dq = deque(n for n, d in indeg.items() if d == 0)
        seen = 0
        while dq:
            cur = dq.popleft()
            seen += 1
            for e in self.out[cur]:
                if e["target"] in indeg:
                    indeg[e["target"]] -= 1
                    if indeg[e["target"]] == 0:
                        dq.append(e["target"])
        return seen != len(self.nodes)


def _raw_at(config: Any, loc: tuple) -> Any:
    cur = config
    for part in loc:
        try:
            cur = cur[part]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def validate_node_config(node: dict) -> list[str]:
    """Validate a node's static configuration. Fields holding templates are checked at run time."""
    spec = get_spec(node.get("type", ""))
    if not spec:
        return [f"Unknown node type '{node.get('type')}'"]
    config = node.get("config") or {}
    try:
        spec.config_model.model_validate(config)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            raw = _raw_at(config, err["loc"])
            if has_template(raw):
                continue
            # a template nested below the failing location (e.g. inside a union member)
            parent = _raw_at(config, err["loc"][:-1]) if err["loc"] else None
            if has_template(parent):
                continue
            problems.append(f"{'.'.join(str(p) for p in err['loc']) or 'config'}: {err['msg']}")
        return problems
    return []


def validate_definition(definition: dict) -> list[dict]:
    """Return a list of issues ``{"level": "error"|"warning", "node_id", "message"}``."""
    issues: list[dict] = []

    def add(level: str, message: str, node_id: str | None = None) -> None:
        issues.append({"level": level, "node_id": node_id, "message": message})

    nodes = definition.get("nodes")
    edges = definition.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        add("error", "Definition must contain 'nodes' and 'edges' lists")
        return issues
    if not nodes:
        add("error", "Workflow has no nodes")
        return issues
    if len(nodes) > MAX_NODES:
        add("error", f"Workflow exceeds the maximum of {MAX_NODES} nodes")
        return issues
    seen: set[str] = set()
    for n in nodes:
        nid = n.get("id") if isinstance(n, dict) else None
        if not isinstance(nid, str) or not NODE_ID.match(nid):
            add("error", f"Invalid node id {nid!r}")
            return issues
        if nid in seen:
            add("error", f"Duplicate node id '{nid}'", nid)
            return issues
        seen.add(nid)
    for e in edges:
        if not isinstance(e, dict) or e.get("source") not in seen or e.get("target") not in seen:
            add("error", "Connection references a node that does not exist")
            return issues

    g = Graph(definition)
    for nid, n in g.nodes.items():
        spec = get_spec(n.get("type", ""))
        if not spec:
            add("error", f"Unknown node type '{n.get('type')}'", nid)
            continue
        for p in validate_node_config(n):
            add("error", p, nid)
        allowed = set(spec.handles_for(n.get("config") or {}))
        for e in g.out[nid]:
            if e["handle"] not in allowed:
                add("error", f"Output '{e['handle']}' does not exist on this node", nid)
        if spec.is_trigger and g.inc[nid]:
            add("error", "Trigger nodes cannot have incoming connections", nid)
        if not spec.is_trigger and not g.inc[nid]:
            add("warning", "Node is not connected to any input and will never run", nid)
        if e_self := [e for e in g.out[nid] if e["target"] == nid]:
            add("error", "A node cannot connect to itself", nid)
            del e_self
    if not g.triggers():
        add("error", "Workflow needs at least one trigger")
    if g.has_cycle():
        add("error", "Workflow contains a cycle. Use the Loop node to repeat steps.")
        return issues
    for loop_id, body in g.bodies.items():
        done_reach = g._reach([e["target"] for e in g.out[loop_id] if e["handle"] == "done"])
        for nid in body:
            if nid in done_reach:
                add("error", "Node is reachable from both the 'each' and 'done' outputs of a loop", nid)
            if g.nodes[nid].get("type") in NO_LOOP_TYPES:
                add("error", "Delay, Wait and Approval nodes cannot be used inside a loop", nid)
            for e in g.inc[nid]:
                if e["source"] != loop_id and e["source"] not in body:
                    add("error", "Nodes inside a loop cannot receive connections from outside the loop", nid)
    return issues


def has_errors(issues: list[dict]) -> bool:
    return any(i["level"] == "error" for i in issues)
