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
    parent_execution_id: uuid.UUID | None = None,
    parent_node_key: str | None = None,
    version_id: uuid.UUID | None = None,
    resume_state: dict | None = None,
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
        pinned = version_id or workflow.current_version_id  # a resumed retry keeps the version its saved progress belongs to
        version = db.get(WorkflowVersion, pinned) if pinned else None
        if version is not None and version.workflow_id != workflow.id:
            raise ValidationFailed("That version belongs to another workflow")
        if not version:
            raise Conflict("Workflow has no published version", code="not_published")
        # Manual runs, retries and calls from another workflow (a reusable sub-process need not have triggers of its own) run inactive workflows.
        if not workflow.is_active and trigger_type not in ("manual", "retry", "subworkflow"):
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
        parent_execution_id=parent_execution_id,
        parent_node_key=parent_node_key,
        state=resume_state or {},
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
    # Workflows this one called and is waiting for are cancelled with it.
    for child in db.scalars(select(WorkflowExecution).where(WorkflowExecution.parent_execution_id == ex.id,
                                                            WorkflowExecution.status.not_in(TERMINAL_STATUSES))).all():
        cancel_execution(db, child)
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


def resumable_state(ex: WorkflowExecution) -> dict | None:
    """The progress of a failed execution up to (not including) the step that failed, or None when
    there is nothing to resume from."""
    import copy

    state = ex.state or {}
    failed = [n for n, s in (state.get("status") or {}).items() if s == "failed"]
    if ex.status != "FAILED" or not failed or not state.get("started") or (not ex.is_test and not ex.workflow_version_id):
        return None
    carried = copy.deepcopy(state)
    for nid in failed:
        carried["status"].pop(nid, None)
        carried.get("outputs", {}).pop(nid, None)
    for key in ("running", "last_error", "fail_now", "retry_delay"):
        carried.pop(key, None)
    carried["waiting"], carried["attempts"], carried["seq"] = {}, {}, 0
    carried["resumed_from"] = {"execution_id": str(ex.id), "steps": failed}
    return carried


def retry_execution(db: Session, ex: WorkflowExecution, user_id: uuid.UUID | None, *, from_failed_step: bool = False) -> WorkflowExecution:
    """Start a new execution with the same trigger payload (a deliberate, human-initiated retry).
    ``from_failed_step`` keeps the results of the steps that succeeded and continues at the step that
    failed, on the same workflow version, so completed actions (emails, CRM writes) are not repeated."""
    if ex.status not in ("FAILED", "CANCELLED"):
        raise Conflict("Only failed or cancelled executions can be retried")
    if ex.details_purged_at:
        raise Conflict("This execution's trigger data was removed by the data retention policy, so it cannot be retried")
    wf = db.get(Workflow, ex.workflow_id)
    if not wf or wf.deleted_at:
        raise NotFound("Workflow not found")
    carried = None
    if from_failed_step:
        carried = resumable_state(ex)
        if carried is None:
            raise Conflict("There is no failed step to continue from; run it again from the start instead")
    return start_execution(
        db, wf, trigger_type="retry", trigger_node=ex.trigger_node, payload=ex.trigger_payload,
        is_test=ex.is_test, definition=ex.definition if ex.is_test else None, created_by=user_id, retry_of=ex.id,
        version_id=ex.workflow_version_id if carried else None, resume_state=carried,
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
