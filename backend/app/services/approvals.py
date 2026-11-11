"""Human approval requests for workflow steps and agent tool calls."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict
from app.db import utcnow
from app.engine.queue import enqueue
from app.models import ApprovalRequest
from app.services.notifications import notify


def create_approval(
    db: Session,
    organization_id: uuid.UUID,
    *,
    kind: str,
    title: str,
    summary: str = "",
    payload: dict | None = None,
    execution_id: uuid.UUID | None = None,
    node_key: str | None = None,
    agent_execution_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> ApprovalRequest:
    req = ApprovalRequest(
        organization_id=organization_id, kind=kind, title=title[:300], summary=summary, payload=payload or {},
        execution_id=execution_id, node_key=node_key, agent_execution_id=agent_execution_id, expires_at=expires_at,
    )
    db.add(req)
    db.flush()
    notify(db, "approval.requested", organization_id, title=f"Approval needed: {title[:200]}", body=summary[:1000],
           severity="warning", data={"approval_id": str(req.id), "kind": kind})
    return req


def decide_approval(db: Session, req: ApprovalRequest, *, approved: bool, user_id: uuid.UUID, note: str = "") -> ApprovalRequest:
    if req.status != "PENDING":
        raise Conflict(f"Approval already {req.status.lower()}")
    if req.expires_at and req.expires_at <= utcnow():
        req.status = "EXPIRED"
        db.flush()
        raise Conflict("Approval has expired")
    req.status = "APPROVED" if approved else "REJECTED"
    req.decided_by, req.decided_at, req.decision_note = user_id, utcnow(), note
    db.flush()
    if req.kind == "workflow_step" and req.execution_id and req.node_key:
        enqueue(db, req.organization_id, req.execution_id, payload={
            "resume_node": req.node_key,
            "output": {"approved": approved, "note": note, "decided_by": str(user_id), "approval_id": str(req.id)},
        })
    elif req.kind == "agent_tool" and req.agent_execution_id:
        enqueue(db, req.organization_id, None, kind="agent_resume",
                payload={"agent_execution_id": str(req.agent_execution_id), "approval_id": str(req.id)})
    notify(db, "approval.completed", req.organization_id,
           title=f"Approval {'granted' if approved else 'rejected'}: {req.title[:200]}", body=note[:1000],
           data={"approval_id": str(req.id), "approved": approved})
    return req


def expire_approvals(db: Session) -> int:
    """Expire overdue agent-tool approvals (workflow-step approvals expire through their timeout job)."""
    now = utcnow()
    rows = db.scalars(select(ApprovalRequest).where(
        ApprovalRequest.status == "PENDING", ApprovalRequest.expires_at.is_not(None), ApprovalRequest.expires_at <= now,
        ApprovalRequest.kind == "agent_tool")).all()
    for req in rows:
        req.status = "EXPIRED"
        if req.agent_execution_id:
            enqueue(db, req.organization_id, None, kind="agent_resume",
                    payload={"agent_execution_id": str(req.agent_execution_id), "approval_id": str(req.id)})
    db.commit()
    return len(rows)
