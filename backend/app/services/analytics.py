"""Analytics computed from stored data - no hardcoded numbers anywhere."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    AIAgentExecution, AuditLog, Integration, Organization, UsageRecord, User, Workflow, WorkflowExecution,
)

RUNNING = ("PENDING", "QUEUED", "RUNNING", "WAITING", "RETRYING")


class Filters:
    def __init__(self, organization_id: uuid.UUID | None, start: datetime | None, end: datetime | None,
                 workflow_id: uuid.UUID | None = None, status: str | None = None, include_tests: bool = False):
        self.organization_id = organization_id
        self.end = end or utcnow()
        self.start = start or (self.end - timedelta(days=30))
        self.workflow_id = workflow_id
        self.status = status
        self.include_tests = include_tests

    def executions(self, q):
        E = WorkflowExecution
        q = q.where(E.created_at >= self.start, E.created_at <= self.end)
        if self.organization_id:
            q = q.where(E.organization_id == self.organization_id)
        if self.workflow_id:
            q = q.where(E.workflow_id == self.workflow_id)
        if self.status:
            q = q.where(E.status == self.status)
        if not self.include_tests:
            q = q.where(E.is_test.is_(False))
        return q

    def org(self, q, column):
        return q.where(column == self.organization_id) if self.organization_id else q


def execution_summary(db: Session, f: Filters) -> dict:
    E = WorkflowExecution
    row = db.execute(f.executions(select(
        func.count(),
        func.sum(case((E.status == "COMPLETED", 1), else_=0)),
        func.sum(case((E.status == "FAILED", 1), else_=0)),
        func.sum(case((E.status.in_(RUNNING), 1), else_=0)),
        func.sum(case((E.status == "CANCELLED", 1), else_=0)),
        func.avg(E.duration_ms),
    ))).one()
    total, ok, failed, running, cancelled, avg = (row[0] or 0, int(row[1] or 0), int(row[2] or 0), int(row[3] or 0), int(row[4] or 0), row[5])
    finished = ok + failed
    return {
        "total": total, "completed": ok, "failed": failed, "running": running, "cancelled": cancelled,
        "success_rate": round(ok / finished * 100, 1) if finished else None,
        "failure_rate": round(failed / finished * 100, 1) if finished else None,
        "avg_duration_ms": round(float(avg)) if avg is not None else None,
    }


def ai_summary(db: Session, f: Filters) -> dict:
    U = UsageRecord
    q = select(U.metric, func.coalesce(func.sum(U.quantity), 0)).where(U.occurred_at >= f.start, U.occurred_at <= f.end).group_by(U.metric)
    q = f.org(q, U.organization_id)
    if f.workflow_id:
        q = q.where(U.workflow_id == f.workflow_id)
    totals = {m: float(v) for m, v in db.execute(q).all()}
    A = AIAgentExecution
    unpriced = db.scalar(f.org(select(func.count()).select_from(A).where(
        A.created_at >= f.start, A.created_at <= f.end, A.cost_usd.is_(None), A.status == "COMPLETED"), A.organization_id)) or 0
    return {
        "requests": int(totals.get("ai_requests", 0)),
        "input_tokens": int(totals.get("ai_input_tokens", 0)),
        "output_tokens": int(totals.get("ai_output_tokens", 0)),
        "total_tokens": int(totals.get("ai_input_tokens", 0) + totals.get("ai_output_tokens", 0)),
        "estimated_cost_usd": round(totals.get("ai_cost_usd", 0.0), 6),
        "unpriced_requests": int(unpriced),  # calls on models without a configured price are excluded from the estimate
    }


def integration_health(db: Session, f: Filters) -> dict:
    I = Integration
    rows = dict(db.execute(f.org(select(I.status, func.count()).where(I.deleted_at.is_(None)).group_by(I.status), I.organization_id)).all())
    total = sum(rows.values())
    return {"total": total, "connected": rows.get("connected", 0), "error": rows.get("error", 0),
            "awaiting_credentials": rows.get("awaiting_credentials", 0), "pending": rows.get("pending", 0), "revoked": rows.get("revoked", 0),
            "uptime_percent": round(rows.get("connected", 0) / (rows.get("connected", 0) + rows.get("error", 0)) * 100, 1)
            if rows.get("connected", 0) + rows.get("error", 0) else None}


def _exec_row(e: WorkflowExecution, names: dict, orgs: dict) -> dict:
    return {"id": str(e.id), "workflow_id": str(e.workflow_id), "workflow_name": names.get(e.workflow_id, ""),
            "organization_id": str(e.organization_id), "organization_name": orgs.get(e.organization_id, ""),
            "status": e.status, "trigger_type": e.trigger_type, "created_at": e.created_at.isoformat(),
            "duration_ms": e.duration_ms, "error": (e.error or {}).get("message") if e.error else None}


def _lookup(db: Session, executions: list[WorkflowExecution]) -> tuple[dict, dict]:
    wf_ids = {e.workflow_id for e in executions}
    org_ids = {e.organization_id for e in executions}
    names = dict(db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(wf_ids or {uuid.uuid4()}))).all())
    orgs = dict(db.execute(select(Organization.id, Organization.name).where(Organization.id.in_(org_ids or {uuid.uuid4()}))).all())
    return names, orgs


def overview(db: Session, f: Filters, *, platform: bool) -> dict:
    E = WorkflowExecution
    W = Workflow
    active_workflows = db.scalar(f.org(select(func.count()).select_from(W).where(W.is_active.is_(True), W.deleted_at.is_(None)), W.organization_id)) or 0
    total_workflows = db.scalar(f.org(select(func.count()).select_from(W).where(W.deleted_at.is_(None)), W.organization_id)) or 0
    recent = db.scalars(f.executions(select(E)).order_by(E.created_at.desc()).limit(10)).all()
    errors = db.scalars(f.executions(select(E)).where(E.status == "FAILED").order_by(E.created_at.desc()).limit(10)).all()
    names, orgs = _lookup(db, [*recent, *errors])
    out: dict = {
        "range": {"start": f.start.isoformat(), "end": f.end.isoformat()},
        "workflows": {"active": active_workflows, "total": total_workflows},
        "executions": execution_summary(db, f),
        "ai": ai_summary(db, f),
        "integrations": integration_health(db, f),
        "recent_activity": [_exec_row(e, names, orgs) for e in recent],
        "recent_errors": [_exec_row(e, names, orgs) for e in errors],
    }
    if platform and not f.organization_id:
        from app.engine.queue import queue_stats

        O = Organization
        out["organizations"] = {
            "total": db.scalar(select(func.count()).select_from(O).where(O.deleted_at.is_(None))) or 0,
            "active": db.scalar(select(func.count()).select_from(O).where(O.deleted_at.is_(None), O.status == "active")) or 0,
        }
        out["clients"] = {"registered_users": db.scalar(select(func.count()).select_from(User).where(User.platform_role.is_(None), User.deleted_at.is_(None))) or 0}
        out["queue"] = queue_stats(db)
        rows = db.execute(f.executions(select(E.organization_id, func.count(), func.sum(case((E.status == "FAILED", 1), else_=0)), func.max(E.created_at)))
                          .group_by(E.organization_id).order_by(func.count().desc()).limit(10)).all()
        org_names = dict(db.execute(select(O.id, O.name).where(O.id.in_([r[0] for r in rows] or [uuid.uuid4()]))).all())
        out["client_activity"] = [{"organization_id": str(r[0]), "organization_name": org_names.get(r[0], ""), "executions": r[1],
                                   "failed": int(r[2] or 0), "last_execution_at": _iso(r[3])} for r in rows]
    return out


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def timeseries(db: Session, f: Filters) -> list[dict]:
    """Executions per day (UTC) split by outcome, with zero-filled gaps."""
    E = WorkflowExecution
    day = func.date(E.created_at)
    rows = db.execute(f.executions(select(
        day, func.count(), func.sum(case((E.status == "COMPLETED", 1), else_=0)), func.sum(case((E.status == "FAILED", 1), else_=0)),
        func.avg(E.duration_ms))).group_by(day).order_by(day)).all()
    by_day = {str(r[0]): r for r in rows}
    U = UsageRecord
    uday = func.date(U.occurred_at)
    tokens = dict(db.execute(f.org(select(uday, func.sum(U.quantity)).where(
        U.occurred_at >= f.start, U.occurred_at <= f.end, U.metric.in_(["ai_input_tokens", "ai_output_tokens"])), U.organization_id).group_by(uday)).all())
    tokens = {str(k): float(v) for k, v in tokens.items()}
    out = []
    cur, last = f.start.date(), f.end.date()
    while cur <= last and len(out) < 400:
        key = cur.isoformat()
        r = by_day.get(key)
        out.append({"date": key, "total": r[1] if r else 0, "completed": int(r[2] or 0) if r else 0, "failed": int(r[3] or 0) if r else 0,
                    "avg_duration_ms": round(float(r[4])) if r and r[4] is not None else None, "ai_tokens": int(tokens.get(key, 0))})
        cur += timedelta(days=1)
    return out


def by_workflow(db: Session, f: Filters, limit: int = 50) -> list[dict]:
    E = WorkflowExecution
    rows = db.execute(f.executions(select(
        E.workflow_id, func.count(), func.sum(case((E.status == "COMPLETED", 1), else_=0)), func.sum(case((E.status == "FAILED", 1), else_=0)),
        func.avg(E.duration_ms), func.max(E.created_at))).group_by(E.workflow_id).order_by(func.count().desc()).limit(limit)).all()
    names = dict(db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_([r[0] for r in rows] or [uuid.uuid4()]))).all())
    return [{"workflow_id": str(r[0]), "workflow_name": names.get(r[0], ""), "executions": r[1], "completed": int(r[2] or 0), "failed": int(r[3] or 0),
             "success_rate": round(int(r[2] or 0) / (int(r[2] or 0) + int(r[3] or 0)) * 100, 1) if (int(r[2] or 0) + int(r[3] or 0)) else None,
             "avg_duration_ms": round(float(r[4])) if r[4] is not None else None, "last_execution_at": _iso(r[5])} for r in rows]


def ai_by_model(db: Session, f: Filters) -> list[dict]:
    A = AIAgentExecution
    q = select(A.provider, A.model, func.count(), func.sum(A.input_tokens), func.sum(A.output_tokens), func.sum(A.cost_usd),
               func.sum(case((A.status == "FAILED", 1), else_=0))).where(A.created_at >= f.start, A.created_at <= f.end).group_by(A.provider, A.model)
    rows = db.execute(f.org(q, A.organization_id)).all()
    return [{"provider": r[0], "model": r[1], "runs": r[2], "input_tokens": int(r[3] or 0), "output_tokens": int(r[4] or 0),
             "estimated_cost_usd": round(float(r[5]), 4) if r[5] is not None else None, "failed": int(r[6] or 0)} for r in rows]


def integrations_detail(db: Session, f: Filters) -> list[dict]:
    I = Integration
    rows = db.scalars(f.org(select(I).where(I.deleted_at.is_(None)).order_by(I.status, I.name).limit(200), I.organization_id)).all()
    return [{"id": str(i.id), "name": i.name, "provider_key": i.provider_key, "status": i.status, "organization_id": str(i.organization_id),
             "last_checked_at": _iso(i.last_checked_at), "last_ok_at": _iso(i.last_ok_at), "last_error": i.last_error} for i in rows]


def audit_activity(db: Session, f: Filters) -> int:
    A = AuditLog
    return db.scalar(f.org(select(func.count()).select_from(A).where(A.created_at >= f.start, A.created_at <= f.end), A.organization_id)) or 0
