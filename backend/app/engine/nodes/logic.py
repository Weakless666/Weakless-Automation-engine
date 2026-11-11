"""Logic nodes: branching, waiting, looping and data shaping."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import Field

from app.core.errors import NodeError
from app.db import utcnow
from app.engine.expressions import OPERATORS, evaluate_condition
from app.engine.registry import NodeConfig, NodeContext, NodeResult, Wait, node


class Condition(NodeConfig):
    left: Any = None
    operator: Literal[OPERATORS] = "equals"  # type: ignore[valid-type]
    right: Any = None


class ConditionGroup(NodeConfig):
    combinator: Literal["and", "or"] = "and"
    conditions: list[Condition] = Field(default_factory=list, min_length=1)


def _check(group: ConditionGroup) -> bool:
    try:
        return evaluate_condition(group.model_dump())
    except re.error as exc:
        raise NodeError(f"Invalid regular expression: {exc}", code="invalid_config") from exc
    except ValueError as exc:
        raise NodeError(str(exc), code="invalid_config") from exc


class IfConfig(NodeConfig):
    condition: ConditionGroup


@node("logic.if", "logic", "If / Else", "Follow 'true' or 'false' depending on a condition.", IfConfig,
      handles=["true", "false"], output_schema={"result": {"type": "boolean"}}, icon="git-branch")
def if_node(ctx: NodeContext, cfg: IfConfig) -> NodeResult:
    result = _check(cfg.condition)
    return NodeResult(output={"result": result}, handles=["true" if result else "false"])


class SwitchConfig(NodeConfig):
    value: Any = Field(None, description="Value to compare, e.g. {{ nodes.classify.label }}")
    cases: list[str] = Field(default_factory=list, min_length=1, description="One output per case")


@node("logic.switch", "logic", "Switch", "Follow the output whose case equals the value, otherwise 'default'.", SwitchConfig,
      dynamic_handles={"field": "cases", "key": None, "extra": ["default"]}, output_schema={"matched": {"type": "string"}}, icon="split")
def switch_node(ctx: NodeContext, cfg: SwitchConfig) -> NodeResult:
    value = "" if cfg.value is None else str(cfg.value).strip().lower()
    for case in cfg.cases:
        if case.strip().lower() == value:
            return NodeResult(output={"matched": case, "value": cfg.value}, handles=[case])
    return NodeResult(output={"matched": "default", "value": cfg.value}, handles=["default"])


class FilterConfig(NodeConfig):
    condition: ConditionGroup


@node("logic.filter", "logic", "Filter", "Continue only when the condition holds; otherwise follow 'rejected'.", FilterConfig,
      handles=["pass", "rejected"], output_schema={"passed": {"type": "boolean"}}, icon="filter")
def filter_node(ctx: NodeContext, cfg: FilterConfig) -> NodeResult:
    passed = _check(cfg.condition)
    return NodeResult(output={"passed": passed}, handles=["pass" if passed else "rejected"])


class Route(NodeConfig):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\- ]+$")
    condition: ConditionGroup


class RouterConfig(NodeConfig):
    routes: list[Route] = Field(default_factory=list, min_length=1)


@node("logic.router", "logic", "Router", "Follow every route whose condition holds (or 'default' when none does).", RouterConfig,
      dynamic_handles={"field": "routes", "key": "name", "extra": ["default"]}, output_schema={"matched": {"type": "array"}}, icon="route")
def router_node(ctx: NodeContext, cfg: RouterConfig) -> NodeResult:
    matched = [r.name for r in cfg.routes if _check(r.condition)]
    return NodeResult(output={"matched": matched}, handles=matched or ["default"])


class DelayConfig(NodeConfig):
    seconds: float = Field(0, ge=0, le=60 * 60 * 24 * 90, description="How long to pause")
    until: str | None = Field(None, description="Alternatively, an ISO 8601 time to resume at")


@node("logic.delay", "logic", "Delay", "Pause the workflow for a duration or until a time. Survives restarts.", DelayConfig,
      output_schema={"resumed_at": {"type": "string"}}, icon="timer")
def delay_node(ctx: NodeContext, cfg: DelayConfig) -> NodeResult:
    if ctx.in_loop:
        raise NodeError("Delay cannot run inside a loop", code="invalid_config")
    if cfg.until:
        try:
            resume_at = datetime.fromisoformat(cfg.until.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NodeError("'until' is not a valid ISO 8601 time", code="invalid_config") from exc
        if resume_at.tzinfo is None:
            resume_at = resume_at.replace(tzinfo=timezone.utc)
    else:
        resume_at = utcnow() + timedelta(seconds=cfg.seconds)
    if resume_at <= utcnow():
        return NodeResult(output={"resumed_at": utcnow().isoformat()})
    return NodeResult(output={"resume_at": resume_at.isoformat()}, wait=Wait(kind="delay", resume_at=resume_at))


class WaitConfig(NodeConfig):
    timeout_seconds: float | None = Field(None, ge=1, le=60 * 60 * 24 * 90, description="Follow 'timeout' if nothing arrives in time")


@node("logic.wait", "logic", "Wait", "Pause until an external signal resumes the execution ({{ execution.resume_url }}).", WaitConfig,
      handles=["out", "timeout"], output_schema={"timed_out": {"type": "boolean"}, "data": {"type": "object"}}, icon="pause")
def wait_node(ctx: NodeContext, cfg: WaitConfig) -> NodeResult:
    if ctx.in_loop:
        raise NodeError("Wait cannot run inside a loop", code="invalid_config")
    resume_at = utcnow() + timedelta(seconds=cfg.timeout_seconds) if cfg.timeout_seconds else None
    return NodeResult(output={"waiting_for": "external_signal"}, wait=Wait(kind="external", resume_at=resume_at))


class ApprovalConfig(NodeConfig):
    title: str = Field(..., min_length=1, max_length=300)
    summary: str = Field("", description="Details shown to the approver")
    timeout_hours: float | None = Field(72, ge=0.01, le=24 * 90, description="Treated as rejected when it expires")


@node("logic.approval", "logic", "Human Approval", "Pause until a person approves or rejects.", ApprovalConfig,
      handles=["approved", "rejected"], output_schema={"approved": {"type": "boolean"}, "note": {"type": "string"}}, icon="shield-check")
def approval_node(ctx: NodeContext, cfg: ApprovalConfig) -> NodeResult:
    from app.services.approvals import create_approval

    if ctx.in_loop:
        raise NodeError("Approval cannot run inside a loop", code="invalid_config")
    expires = utcnow() + timedelta(hours=cfg.timeout_hours) if cfg.timeout_hours else None
    req = create_approval(ctx.db, ctx.organization_id, kind="workflow_step", title=cfg.title, summary=cfg.summary,
                          payload={"workflow_id": str(ctx.execution.workflow_id)}, execution_id=ctx.execution.id,
                          node_key=ctx.node_id, expires_at=expires)
    return NodeResult(output={"approval_id": str(req.id)}, wait=Wait(kind="approval", resume_at=expires, data={"approval_id": str(req.id)}))


class LoopConfig(NodeConfig):
    items: Any = Field(..., description="A list, usually a template such as {{ nodes.fetch.body.items }}")
    max_iterations: int = Field(100, ge=1, le=1000)
    continue_on_error: bool = Field(False, description="Record failed iterations and keep going")


@node("logic.loop", "logic", "Loop", "Run the 'each' branch once per item ({{ loop.item }}), then continue from 'done'.", LoopConfig,
      handles=["each", "done"], output_schema={"count": {"type": "integer"}, "results": {"type": "array"}}, icon="repeat")
def loop_node(ctx: NodeContext, cfg: LoopConfig) -> NodeResult:  # pragma: no cover - implemented by the executor
    raise NodeError("Loop is executed by the engine", code="internal_error")


class MergeConfig(NodeConfig):
    mode: Literal["object", "list"] = Field("object", description="Combine upstream outputs into one object or a list")


@node("logic.merge", "logic", "Merge", "Wait for all incoming branches and combine their outputs.", MergeConfig,
      output_schema={"merged": {"type": "object"}}, icon="merge")
def merge_node(ctx: NodeContext, cfg: MergeConfig) -> NodeResult:
    sources = ctx.scope.get("_sources") or {}
    if cfg.mode == "list":
        return NodeResult(output={"merged": list(sources.values()), "sources": list(sources)})
    merged: dict = {}
    for out in sources.values():
        if isinstance(out, dict):
            merged.update(out)
    return NodeResult(output={"merged": merged, "sources": list(sources)})


class TransformConfig(NodeConfig):
    fields: dict[str, Any] = Field(default_factory=dict, description="Output field -> value or template")
    keep_input: bool = Field(False, description="Start from the upstream output and add/override fields")


@node("logic.transform", "logic", "Transform Data", "Build a new object from templates and upstream data.", TransformConfig,
      output_schema={}, icon="shuffle")
def transform_node(ctx: NodeContext, cfg: TransformConfig) -> NodeResult:
    base = dict(ctx.scope.get("input") or {}) if cfg.keep_input and isinstance(ctx.scope.get("input"), dict) else {}
    return NodeResult(output={**base, **cfg.fields})


class Rule(NodeConfig):
    field: str = Field(..., description="Label used in error messages")
    value: Any = None
    required: bool = True
    type: Literal["any", "string", "number", "boolean", "email", "url", "list", "object"] = "any"
    pattern: str | None = Field(None, max_length=200)
    min_length: int | None = Field(None, ge=0)
    max_length: int | None = Field(None, ge=0)


class ValidateConfig(NodeConfig):
    rules: list[Rule] = Field(default_factory=list, min_length=1)


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@node("logic.validate", "logic", "Validate Data", "Check values against rules; follow 'valid' or 'invalid'.", ValidateConfig,
      handles=["valid", "invalid"], output_schema={"valid": {"type": "boolean"}, "errors": {"type": "array"}}, icon="check-check")
def validate_node(ctx: NodeContext, cfg: ValidateConfig) -> NodeResult:
    errors: list[str] = []
    for r in cfg.rules:
        v = r.value
        if v in (None, "", [], {}):
            if r.required:
                errors.append(f"{r.field} is required")
            continue
        ok = {
            "any": True, "string": isinstance(v, str), "boolean": isinstance(v, bool),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool) or (isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v.strip()) is not None),
            "email": isinstance(v, str) and _EMAIL.match(v.strip()) is not None,
            "url": isinstance(v, str) and re.match(r"^https?://[^\s]+$", v.strip()) is not None,
            "list": isinstance(v, list), "object": isinstance(v, dict),
        }[r.type]
        if not ok:
            errors.append(f"{r.field} must be a valid {r.type}")
            continue
        if r.pattern:
            try:
                if re.search(r.pattern, str(v)[:10000]) is None:
                    errors.append(f"{r.field} has an invalid format")
            except re.error as exc:
                raise NodeError(f"Invalid pattern for {r.field}: {exc}", code="invalid_config") from exc
        if hasattr(v, "__len__"):
            if r.min_length is not None and len(v) < r.min_length:
                errors.append(f"{r.field} is too short")
            if r.max_length is not None and len(v) > r.max_length:
                errors.append(f"{r.field} is too long")
    return NodeResult(output={"valid": not errors, "errors": errors}, handles=["invalid" if errors else "valid"])
