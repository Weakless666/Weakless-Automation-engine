"""Flow nodes: calling other workflows and answering the caller of a webhook."""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from pydantic import Field

from app.core.errors import AppError, NodeError
from app.db import utcnow
from app.engine.registry import NodeConfig, NodeContext, NodeResult, Wait, node

MAX_CALL_DEPTH = 5


class RunWorkflowConfig(NodeConfig):
    workflow_id: str = Field(..., title="Workflow", description="A published workflow of this organization")
    input: Any = Field(default_factory=dict, description="Data for the called workflow; it sees it as {{ trigger.* }}")
    wait_for_result: bool = Field(True, description="Pause until the called workflow finishes and use its result")
    timeout_minutes: float = Field(60, ge=0.1, le=60 * 24 * 7, description="Follow 'error' if the called workflow takes longer")


def _call_chain(db, ex) -> list[uuid.UUID]:
    """Workflow ids from this execution up through every calling parent."""
    from app.models import WorkflowExecution

    chain, current, seen = [ex.workflow_id], ex, {ex.id}
    while current.parent_execution_id and current.parent_execution_id not in seen and len(chain) <= MAX_CALL_DEPTH + 1:
        seen.add(current.parent_execution_id)
        current = db.get(WorkflowExecution, current.parent_execution_id)
        if not current:
            break
        chain.append(current.workflow_id)
    return chain


@node("logic.run_workflow", "logic", "Run Workflow",
      "Run another workflow (a reusable sub-process) and, by default, continue with its result.", RunWorkflowConfig,
      handles=["out", "error"], idempotent=False,
      output_schema={"execution_id": {"type": "string"}, "status": {"type": "string"}, "output": {"type": "object"}, "error": {"type": "object"}},
      icon="workflow")
def run_workflow_node(ctx: NodeContext, cfg: RunWorkflowConfig) -> NodeResult:
    from app.models import Workflow
    from app.services.executions import start_execution

    try:
        child = ctx.db.get(Workflow, uuid.UUID(str(cfg.workflow_id)))
    except ValueError:
        child = None
    if not child or child.organization_id != ctx.organization_id or child.deleted_at:
        raise NodeError("Workflow not found in this organization", code="workflow_not_found")
    chain = _call_chain(ctx.db, ctx.execution)
    if child.id in chain:
        raise NodeError(f"'{child.name}' is already running higher up this chain; calling it again would loop", code="recursive_call")
    if len(chain) > MAX_CALL_DEPTH:
        raise NodeError(f"Workflows may call each other at most {MAX_CALL_DEPTH} levels deep", code="call_depth_exceeded")
    if cfg.wait_for_result and ctx.in_loop:
        raise NodeError("Inside a loop, Run Workflow cannot wait for the result; switch off 'wait for result'", code="invalid_config")
    payload = cfg.input if isinstance(cfg.input, dict) else {"input": cfg.input}
    try:
        sub = start_execution(ctx.db, child, trigger_type="subworkflow", payload=payload,
                              idempotency_key=f"call:{ctx.execution.id}:{ctx.node_id}:{ctx.scope.get('loop', {}).get('index', '')}",
                              parent_execution_id=ctx.execution.id, parent_node_key=None if not cfg.wait_for_result else ctx.node_id)
    except AppError as exc:
        raise NodeError(exc.message, code=exc.code) from exc
    if not cfg.wait_for_result:
        return NodeResult(output={"execution_id": str(sub.id), "status": sub.status})
    return NodeResult(output={"execution_id": str(sub.id), "status": "RUNNING"},
                      wait=Wait(kind="subworkflow", resume_at=utcnow() + timedelta(minutes=cfg.timeout_minutes),
                                data={"execution_id": str(sub.id)}))


class RespondConfig(NodeConfig):
    status_code: int = Field(200, ge=100, le=599)
    body: Any = Field(default_factory=dict, description="JSON object or text sent back to whoever called the trigger URL")
    content_type: str = Field("application/json", description="application/json, text/plain or text/html")


@node("action.respond_webhook", "action", "Respond to Webhook",
      "Send the reply to the system that called this workflow's webhook or form URL (when its trigger waits for a response).",
      RespondConfig, output_schema={"status_code": {"type": "integer"}}, icon="reply")
def respond_node(ctx: NodeContext, cfg: RespondConfig) -> NodeResult:
    if cfg.content_type not in ("application/json", "text/plain", "text/html"):
        raise NodeError("Content type must be application/json, text/plain or text/html", code="invalid_config")
    if cfg.content_type != "application/json" and not isinstance(cfg.body, str):
        raise NodeError("A text or HTML response needs a text body", code="invalid_config")
    response = {"status_code": cfg.status_code, "body": cfg.body, "content_type": cfg.content_type}
    # The executor stores this on the execution; the waiting webhook request returns it.
    return NodeResult(output={"status_code": cfg.status_code, "_respond": response})
