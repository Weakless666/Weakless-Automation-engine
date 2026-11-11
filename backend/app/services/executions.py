"""Starting, cancelling, retrying and resuming workflow executions."""
from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, Conflict, NotFound, ValidationFailed
from app.core.security import random_token
from app.db import utcnow
from app.engine.graph import Graph, has_errors, validate_definition
from app.engine.queue import enqueue
from app.models import (
    TERMINAL_STATUSES, ApprovalRequest, ExecutionQueueJob, Workflow, WorkflowExecution, WorkflowVersion,
)
from app.services.usage import check_quota, record_usage


def start_execution(
    db: Session,
    workflow: Workflow,
    *,
    trigger_type: str,
    trigger_node: str | None = None,
    payload: dict | None = None,
    is_test: bool = False,
    definition: dict | None = None,
    idempotency_key: str | None = None,
    created_by: uuid.UUID | None = None,
    retry_of: uuid.UUID | None = None,
) -> WorkflowExecution:
    """Create an execution and enqueue it. Flushes; the caller commits.

    * production runs use the workflow's current published version and require
      the workflow to be active (manual runs of a published workflow are allowed
      while inactive);
    * test runs execute the supplied draft ``definition``;
    * an ``idempotency_key`` returns the existing execution instead of a duplicate.
    """
    if workflow.deleted_at:
        raise NotFound("Workflow not found")
    if idempotency_key:
        existing = db.scalar(select(WorkflowExecution).where(
            WorkflowExecution.workflow_id == workflow.id, WorkflowExecution.idempotency_key == idempotency_key))
        if existing:
            return existing
    version_id = None
    variables = dict(workflow.variables or {})
    if is_test:
        if definition is None:
            raise ValidationFailed("A draft definition is required for a test run")
        issues = validate_definition(definition)
        if has_errors(issues):
            raise ValidationFailed("Workflow has validation errors", details=issues)
        graph_def = definition
    else:
        version = db.get(WorkflowVersion, workflow.current_version_id) if workflow.current_version_id else None
        if not version:
            raise Conflict("Workflow has no published version", code="not_published")
        if not workflow.is_active and trigger_type not in ("manual", "retry"):
            raise Conflict("Workflow is not active", code="workflow_inactive")
        version_id, graph_def = version.id, version.definition
        variables = {**(version.variables or {}), **variables}
        check_quota(db, workflow.organization_id, "executions_per_month")

    g = Graph(graph_def)
    triggers = g.triggers()
    if trigger_node and trigger_node not in triggers:
        raise ValidationFailed(f"Trigger node '{trigger_node}' does not exist in this workflow version")
    ex = WorkflowExecution(
        organization_id=workflow.organization_id,
        workflow_id=workflow.id,
        workflow_version_id=version_id,
        definition=definition if is_test else None,
        variables=variables,
        status="QUEUED",
        trigger_type=trigger_type,
        trigger_node=trigger_node or (triggers[0] if triggers else None),
        trigger_payload=payload or {},
        idempotency_key=idempotency_key,
        retry_of_id=retry_of,
        is_test=is_test,
        resume_token=random_token(24),
        created_by=created_by,
    )
    db.add(ex)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(WorkflowExecution).where(
            WorkflowExecution.workflow_id == workflow.id, WorkflowExecution.idempotency_key == idempotency_key))
        if existing:
            return existing
        raise
    if not is_test:
        record_usage(db, workflow.organization_id, "executions", 1, workflow_id=workflow.id, execution_id=ex.id)
    enqueue(db, workflow.organization_id, ex.id)
    return ex


def cancel_execution(db: Session, ex: WorkflowExecution) -> WorkflowExecution:
    if ex.status in TERMINAL_STATUSES:
        raise Conflict(f"Execution already {ex.status.lower()}")
    ex.cancel_requested = True
    db.flush()
    # Not currently owned by a worker: cancel immediately.
    res = db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == ex.id, WorkflowExecution.status.in_(["PENDING", "QUEUED", "WAITING", "RETRYING"]))
        .values(status="CANCELLED", finished_at=utcnow(), error={"code": "cancelled", "message": "Execution was cancelled"})
        .execution_options(synchronize_session=False)
    )
    if res.rowcount:
        db.execute(
            update(ExecutionQueueJob)
            .where(ExecutionQueueJob.execution_id == ex.id, ExecutionQueueJob.status == "QUEUED")
            .values(status="DONE", finished_at=utcnow(), last_error="cancelled")
            .execution_options(synchronize_session=False)
        )
        db.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.execution_id == ex.id, ApprovalRequest.status == "PENDING")
            .values(status="EXPIRED")
            .execution_options(synchronize_session=False)
        )
    db.flush()
    db.refresh(ex)
    return ex


def retry_execution(db: Session, ex: WorkflowExecution, user_id: uuid.UUID | None) -> WorkflowExecution:
    """Start a fresh execution with the same trigger payload (a deliberate, human-initiated retry)."""
    if ex.status not in ("FAILED", "CANCELLED"):
        raise Conflict("Only failed or cancelled executions can be retried")
    wf = db.get(Workflow, ex.workflow_id)
    if not wf or wf.deleted_at:
        raise NotFound("Workflow not found")
    return start_execution(
        db, wf, trigger_type="retry", trigger_node=ex.trigger_node, payload=ex.trigger_payload,
        is_test=ex.is_test, definition=ex.definition if ex.is_test else None, created_by=user_id, retry_of=ex.id,
    )


def resume_execution(db: Session, ex: WorkflowExecution, data: dict | None, node_id: str | None = None) -> str:
    """Resume an execution paused on an external Wait node."""
    if ex.status != "WAITING":
        raise Conflict("Execution is not waiting")
    waiting = {k: v for k, v in (ex.state or {}).get("waiting", {}).items() if v.get("kind") == "external"}
    if node_id:
        if node_id not in waiting:
            raise NotFound("No waiting node with that id")
    elif len(waiting) == 1:
        node_id = next(iter(waiting))
    else:
        raise AppError("Specify which waiting node to resume" if waiting else "Execution is not waiting for an external signal",
                       code="ambiguous_resume", status_code=409)
    enqueue(db, ex.organization_id, ex.id, payload={"resume_node": node_id, "output": data or {}})
    return node_id
