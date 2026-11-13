"""Controlled node registry.

Workflows can only contain node types registered here. Each type declares a
pydantic configuration model (validated on publish and again, after template
rendering, at run time), its output schema, its outgoing handles and whether
it is safe to repeat (``idempotent``). Handlers are ordinary Python functions
in this codebase - user-supplied code is never executed.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session


class NodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyConfig(NodeConfig):
    pass


@dataclass
class Wait:
    """Returned by a handler to suspend the execution durably."""

    kind: str  # delay | external | approval | agent | subworkflow
    resume_at: datetime | None = None  # delay: when to resume; external/approval: optional timeout
    data: dict = field(default_factory=dict)


@dataclass
class NodeResult:
    output: dict = field(default_factory=dict)
    handles: list[str] | None = None  # outgoing handles to follow; None = ["out"]
    wait: Wait | None = None


@dataclass
class NodeContext:
    db: Session
    organization_id: uuid.UUID
    execution: Any  # WorkflowExecution
    node: dict
    scope: dict
    secrets: set[str] = field(default_factory=set)  # values to scrub from stored step data
    in_loop: bool = False

    @property
    def node_id(self) -> str:
        return self.node["id"]


Handler = Callable[[NodeContext, Any], NodeResult]


@dataclass
class NodeSpec:
    type: str
    category: str  # trigger | action | logic | ai
    label: str
    description: str
    config_model: type[NodeConfig]
    handler: Handler
    handles: list[str] = field(default_factory=lambda: ["out"])
    dynamic_handles: dict | None = None  # {"field": "cases", "key": None, "extra": ["default"]}
    output_schema: dict = field(default_factory=dict)
    idempotent: bool = True  # safe to re-run automatically after a transient failure / crash
    providers: list[str] = field(default_factory=list)  # integration providers that can back this node
    high_impact: bool = False
    icon: str = "circle"

    @property
    def is_trigger(self) -> bool:
        return self.category == "trigger"

    def handles_for(self, config: dict) -> list[str]:
        handles = list(self.handles)
        if self.dynamic_handles:
            items = config.get(self.dynamic_handles["field"]) or []
            key = self.dynamic_handles.get("key")
            names = [str(i.get(key) if key and isinstance(i, dict) else i) for i in items if i not in (None, "")]
            handles = names + list(self.dynamic_handles.get("extra", []))
        if not self.is_trigger and self.category in ("action", "ai") and "error" not in handles:
            handles.append("error")
        return handles

    def describe(self) -> dict:
        schema = self.config_model.model_json_schema()
        return {
            "type": self.type,
            "category": self.category,
            "label": self.label,
            "description": self.description,
            "icon": self.icon,
            "config_schema": schema,
            "input_schema": schema,  # handlers receive the rendered configuration as their input
            "output_schema": {"type": "object", "properties": self.output_schema},
            "handles": self.handles,
            "dynamic_handles": self.dynamic_handles,
            "supports_error_handle": not self.is_trigger and self.category in ("action", "ai"),
            "idempotent": self.idempotent,
            "providers": self.providers,
            "high_impact": self.high_impact,
        }


_REGISTRY: dict[str, NodeSpec] = {}
_loaded = False
_load_lock = threading.RLock()


def register(spec: NodeSpec) -> NodeSpec:
    if spec.type in _REGISTRY:
        raise RuntimeError(f"Duplicate node type {spec.type}")
    _REGISTRY[spec.type] = spec
    return spec


def node(type: str, category: str, label: str, description: str, config: type[NodeConfig] = EmptyConfig, **kw):
    """Decorator registering a handler as a node type."""

    def deco(fn: Handler) -> Handler:
        register(NodeSpec(type=type, category=category, label=label, description=description, config_model=config, handler=fn, **kw))
        return fn

    return deco


def load_nodes() -> None:
    """Import the node library exactly once. Guarded by a lock: the API threadpool and
    the worker thread can both ask for the registry at the same moment after start-up,
    and nobody may observe it half-populated."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        from app.engine.nodes import actions, ai, data, flow, logic, triggers  # noqa: F401

        _loaded = True


def get_spec(type: str) -> NodeSpec | None:
    load_nodes()
    return _REGISTRY.get(type)


def all_specs() -> list[NodeSpec]:
    load_nodes()
    return list(_REGISTRY.values())
