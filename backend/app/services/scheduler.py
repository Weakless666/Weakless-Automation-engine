"""Cron scheduling for Schedule Trigger nodes."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db import utcnow
from app.models import Workflow, WorkflowSchedule

log = logging.getLogger("wae.scheduler")


def valid_cron(expr: str) -> bool:
    return bool(expr) and len(expr.split()) == 5 and croniter.is_valid(expr)


def next_run(cron: str, tz: str, after: datetime | None = None) -> datetime:
    try:
        zone = ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    base = (after or utcnow()).astimezone(zone)
    return croniter(cron, base).get_next(datetime).astimezone(ZoneInfo("UTC"))


def run_due_schedules(db: Session) -> int:
    """Start executions for due schedules. Each schedule is advanced with a
    compare-and-swap so two workers never fire the same tick."""
    from app.services.executions import start_execution

    now = utcnow()
    due = db.scalars(select(WorkflowSchedule).where(
        WorkflowSchedule.is_active.is_(True), WorkflowSchedule.next_run_at.is_not(None), WorkflowSchedule.next_run_at <= now)).all()
    fired = 0
    for sch in due:
        scheduled_for = sch.next_run_at
        res = db.execute(
            update(WorkflowSchedule)
            .where(WorkflowSchedule.id == sch.id, WorkflowSchedule.next_run_at == scheduled_for)
            .values(next_run_at=next_run(sch.cron, sch.timezone, now), last_run_at=now)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if res.rowcount == 0:
            continue
        wf = db.get(Workflow, sch.workflow_id)
        if not wf or wf.deleted_at or not wf.is_active:
            continue
        try:
            start_execution(
                db, wf, trigger_type="schedule", trigger_node=sch.node_key,
                payload={"scheduled_for": scheduled_for.isoformat(), "cron": sch.cron, "timezone": sch.timezone},
                idempotency_key=f"schedule:{sch.id}:{scheduled_for.isoformat()}",
            )
            db.commit()
            fired += 1
        except AppError as exc:
            db.rollback()
            log.warning("schedule %s could not start workflow %s: %s", sch.id, wf.id, exc.message)
    return fired
