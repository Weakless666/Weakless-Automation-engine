"""Application event bus.

``emit_app_event`` records an event in the central event log and starts every
active workflow of the organization that has an Application Event trigger
subscribed to it. Execution lifecycle events (``workflow.completed`` /
``workflow.failed``) are emitted here too; executions that were themselves
started by a lifecycle event do not emit further lifecycle events, which
prevents feedback loops.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.redact import redact
from app.models import AppEventSubscription, Workflow, WorkflowExecution, WebhookEvent

log = logging.getLogger("wae.events")
LIFECYCLE_EVENTS = ("workflow.completed", "workflow.failed")


def _matches(pattern: str, event_type: str) -> bool:
    pattern = (pattern or "").strip()
    if pattern in ("*", event_type):
        return True
    return pattern.endswith("*") and event_type.startswith(pattern[:-1])


def emit_app_event(db: Session, organization_id: uuid.UUID, event_type: str, payload: dict, *, source: str = "app") -> WebhookEvent:
    from app.services.executions import start_execution

    event = WebhookEvent(organization_id=organization_id, source=source, event_type=event_type,
                         payload=redact(payload), status="received")
    db.add(event)
    db.flush()
    started = 0
    # Indexed lookup: exact subscriptions for this type plus the organization's (few) wildcard ones.
    S = AppEventSubscription
    rows = db.execute(select(S.node_key, S.pattern, Workflow).join(Workflow, Workflow.id == S.workflow_id).where(
        S.organization_id == organization_id, or_(S.exact_type == event_type, S.exact_type.is_(None)),
        Workflow.is_active.is_(True), Workflow.deleted_at.is_(None), Workflow.current_version_id.is_not(None),
    ).order_by(Workflow.created_at, S.node_key)).all()
    for node_key, pattern, wf in rows:
        if not _matches(pattern, event_type):
            continue
        if str(wf.id) == str(payload.get("workflow_id")) and event_type in LIFECYCLE_EVENTS:
            continue  # a workflow never reacts to its own lifecycle events
        try:
            ex = start_execution(db, wf, trigger_type="app_event", trigger_node=node_key,
                                 payload={"event_type": event_type, "data": payload, "event_id": str(event.id)})
            event.execution_id = event.execution_id or ex.id
            started += 1
        except AppError as exc:
            log.warning("app event %s could not start workflow %s: %s", event_type, wf.id, exc.message)
    event.status = "processed" if started else "ignored"
    db.flush()
    return event


def on_execution_finished(db: Session, ex: WorkflowExecution) -> None:
    """Notifications and lifecycle events after an execution reaches a terminal state."""
    from app.services.notifications import notify

    if ex.is_test or ex.status not in ("COMPLETED", "FAILED"):
        return
    wf = db.get(Workflow, ex.workflow_id)
    name = wf.name if wf else "Workflow"
    event_type = "workflow.completed" if ex.status == "COMPLETED" else "workflow.failed"
    data = {"workflow_id": str(ex.workflow_id), "workflow_name": name, "execution_id": str(ex.id),
            "status": ex.status, "duration_ms": ex.duration_ms}
    if ex.status == "FAILED":
        err = ex.error or {}
        data["error"] = {"code": err.get("code"), "message": err.get("message"), "node_id": err.get("node_id")}
        notify(db, event_type, ex.organization_id, title=f"Workflow failed: {name}",
               body=str(err.get("message") or "Execution failed")[:500], severity="error", data=data)
    else:
        notify(db, event_type, ex.organization_id, title=f"Workflow completed: {name}", data=data)
    started_by_lifecycle = ex.trigger_type == "app_event" and (ex.trigger_payload or {}).get("event_type") in LIFECYCLE_EVENTS
    if not started_by_lifecycle:
        emit_app_event(db, ex.organization_id, event_type, data, source="system")
