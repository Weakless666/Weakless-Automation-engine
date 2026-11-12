"""Data retention: bounded clean-up of operational history.

The policy is the ``data.retention`` platform setting (Settings -> Platform). Each
entry is a number of days, or ``None`` to keep that data forever:

* ``execution_details_days`` - for finished executions: delete the per-step log and
  clear trigger payload, interpreter state, variables and output. The execution row
  (status, timings, error) stays, so analytics and history lists are unaffected.
* ``executions_days`` - delete finished executions entirely (with their steps, queue
  jobs and approvals). Analytics only covers executions that still exist.
* ``events_days`` - delete the inbound event log (webhooks, forms, app events).
  De-duplication only recognises redeliveries of events that are still stored.
* ``usage_detail_days`` - compact usage records into one row per organization,
  metric, billing period, workflow and day. Totals per period and per day are
  unchanged, so quotas, billing and charts read the same numbers.
* ``notifications_days`` - delete in-app notifications and channel delivery logs.
* ``queue_jobs_days`` - delete completed queue jobs (dead letters are kept).
* ``audit_days`` - delete audit log and sign-in history. Off by default.

Finished shared rate-limit windows are removed on every run. Expired sign-in sessions, account links and OAuth states are always removed a week
after they expire. Every sweep works in batches so it never holds long locks; the
worker runs it hourly and ``python -m app.cli retention`` runs it on demand.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    TERMINAL_STATUSES, AIAgentExecution, ApprovalRequest, AuditLog, ExecutionQueueJob, GeneratedDocument, LoginHistory,
    Notification, NotificationDelivery, OAuthState, SystemSetting, UsageRecord, UserSession, UserToken, WebhookEvent,
    WorkflowExecution, WorkflowExecutionStep,
)

log = logging.getLogger("wae.retention")

SETTING_KEY = "data.retention"
DEFAULT_POLICY: dict[str, int | None] = {
    "execution_details_days": 30,
    "executions_days": 365,
    "events_days": 30,
    "usage_detail_days": 62,
    "notifications_days": 90,
    "queue_jobs_days": 7,
    "audit_days": None,
}
MINIMUM_DAYS = {"audit_days": 30}  # everything else: at least 1 day
EXPIRED_GRACE = timedelta(days=7)
BATCH = 500
MAX_BATCHES = 20  # per category per run; a backlog is worked off over several runs


def validate_policy(value: object) -> dict[str, int | None]:
    """Return the complete policy or raise ValueError with a message for the user."""
    if not isinstance(value, dict):
        raise ValueError("Retention policy must be an object of day counts")
    unknown = set(value) - set(DEFAULT_POLICY)
    if unknown:
        raise ValueError(f"Unknown retention setting: {', '.join(sorted(unknown))}")
    policy = {**DEFAULT_POLICY, **value}
    for key, days in policy.items():
        if days is None:
            continue
        if isinstance(days, bool) or not isinstance(days, int):
            raise ValueError(f"'{key}' must be a whole number of days or null (keep forever)")
        if days < MINIMUM_DAYS.get(key, 1) or days > 3650:
            raise ValueError(f"'{key}' must be between {MINIMUM_DAYS.get(key, 1)} and 3650 days")
    details, whole = policy["execution_details_days"], policy["executions_days"]
    if whole is not None and (details is None or details > whole):
        raise ValueError("Execution details cannot be kept longer than the executions themselves")
    return policy


def get_policy(db: Session) -> dict[str, int | None]:
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == SETTING_KEY))
    try:
        return validate_policy(row.value if row is not None and row.value is not None else {})
    except ValueError:
        log.warning("Ignoring invalid %s setting; using defaults", SETTING_KEY)
        return dict(DEFAULT_POLICY)


def _cutoff(now: datetime, days: int) -> datetime:
    """Start of the UTC day `days` ago: sweeps act on whole days."""
    day = (now - timedelta(days=days)).astimezone(timezone.utc).date()
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _batched(db: Session, ids_query, act) -> int:
    total = 0
    for _ in range(MAX_BATCHES):
        ids = list(db.scalars(ids_query.limit(BATCH)))
        if not ids:
            break
        act(ids)
        db.commit()
        total += len(ids)
        if len(ids) < BATCH:
            break
    return total


def _delete_by_id(db: Session, model, ids_query) -> int:
    return _batched(db, ids_query, lambda ids: db.execute(delete(model).where(model.id.in_(ids)).execution_options(synchronize_session=False)))


def purge_execution_details(db: Session, cutoff: datetime, now: datetime) -> int:
    E = WorkflowExecution

    def act(ids):
        db.execute(delete(WorkflowExecutionStep).where(WorkflowExecutionStep.execution_id.in_(ids)).execution_options(synchronize_session=False))
        db.execute(update(E).where(E.id.in_(ids)).values(trigger_payload={}, state={}, variables={}, output=None, definition=None,
                                                          resume_token=None, details_purged_at=now)
                   .execution_options(synchronize_session=False))

    q = select(E.id).where(E.status.in_(TERMINAL_STATUSES), E.finished_at < cutoff, E.details_purged_at.is_(None)).order_by(E.finished_at)
    return _batched(db, q, act)


def purge_executions(db: Session, cutoff: datetime) -> int:
    E = WorkflowExecution

    def act(ids):
        opts = {"synchronize_session": False}
        db.execute(delete(WorkflowExecutionStep).where(WorkflowExecutionStep.execution_id.in_(ids)).execution_options(**opts))
        db.execute(delete(ExecutionQueueJob).where(ExecutionQueueJob.execution_id.in_(ids)).execution_options(**opts))
        db.execute(delete(ApprovalRequest).where(ApprovalRequest.execution_id.in_(ids)).execution_options(**opts))
        db.execute(update(AIAgentExecution).where(AIAgentExecution.workflow_execution_id.in_(ids)).values(workflow_execution_id=None).execution_options(**opts))
        db.execute(update(GeneratedDocument).where(GeneratedDocument.execution_id.in_(ids)).values(execution_id=None).execution_options(**opts))
        db.execute(delete(E).where(E.id.in_(ids)).execution_options(**opts))

    q = select(E.id).where(E.status.in_(TERMINAL_STATUSES), E.finished_at < cutoff).order_by(E.finished_at)
    agent_runs = _delete_by_id(db, AIAgentExecution, select(AIAgentExecution.id).where(
        AIAgentExecution.status.in_(("COMPLETED", "FAILED", "CANCELLED")), AIAgentExecution.finished_at < cutoff,
        ~AIAgentExecution.id.in_(select(ApprovalRequest.agent_execution_id).where(ApprovalRequest.agent_execution_id.is_not(None)))))
    return _batched(db, q, act) + agent_runs


def compact_usage(db: Session, cutoff: datetime) -> int:
    """Merge usage rows older than `cutoff` into one row per org/metric/period/workflow/day.
    Returns the number of rows removed by merging."""
    U = UsageRecord
    postgres = db.bind is not None and db.bind.dialect.name == "postgresql"
    day = func.date(func.timezone("UTC", U.occurred_at)) if postgres else func.date(U.occurred_at)  # UTC day on every backend
    groups_q = (select(U.organization_id, U.metric, U.period, U.workflow_id, day, func.sum(U.quantity), func.count())
                .where(U.occurred_at < cutoff).group_by(U.organization_id, U.metric, U.period, U.workflow_id, day)
                .having(func.count() > 1).limit(BATCH))
    removed = 0
    for _ in range(MAX_BATCHES):
        groups = db.execute(groups_q).all()
        if not groups:
            break
        for org_id, metric, period, workflow_id, d, quantity, count in groups:
            start = datetime.fromisoformat(str(d)[:10]).replace(tzinfo=timezone.utc)
            same_workflow = U.workflow_id.is_(None) if workflow_id is None else U.workflow_id == workflow_id
            db.execute(delete(U).where(and_(U.organization_id == org_id, U.metric == metric, U.period == period, same_workflow,
                                            U.occurred_at >= start, U.occurred_at < min(start + timedelta(days=1), cutoff)))
                       .execution_options(synchronize_session=False))
            db.add(U(organization_id=org_id, metric=metric, period=period, workflow_id=workflow_id, quantity=quantity,
                     occurred_at=start, meta={"compacted_records": int(count)}))
            removed += int(count) - 1
        db.commit()
        if len(groups) < BATCH:
            break
    return removed


def run_retention(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Apply the retention policy once. Returns the number of rows affected per category."""
    now = now or utcnow()
    policy = get_policy(db)
    days = lambda key: policy[key]  # noqa: E731
    out: dict[str, int] = {}
    if days("executions_days"):
        out["executions_deleted"] = purge_executions(db, _cutoff(now, days("executions_days")))
    if days("execution_details_days"):
        out["execution_details_removed"] = purge_execution_details(db, _cutoff(now, days("execution_details_days")), now)
    if days("events_days"):
        c = _cutoff(now, days("events_days"))
        out["events_deleted"] = _delete_by_id(db, WebhookEvent, select(WebhookEvent.id).where(WebhookEvent.received_at < c))
    if days("usage_detail_days"):
        out["usage_records_merged"] = compact_usage(db, _cutoff(now, days("usage_detail_days")))
    if days("notifications_days"):
        c = _cutoff(now, days("notifications_days"))
        out["notifications_deleted"] = (
            _delete_by_id(db, Notification, select(Notification.id).where(Notification.created_at < c))
            + _delete_by_id(db, NotificationDelivery, select(NotificationDelivery.id).where(NotificationDelivery.created_at < c)))
    if days("queue_jobs_days"):
        c = _cutoff(now, days("queue_jobs_days"))
        out["queue_jobs_deleted"] = _delete_by_id(db, ExecutionQueueJob, select(ExecutionQueueJob.id).where(
            ExecutionQueueJob.status == "DONE", ExecutionQueueJob.finished_at < c))
    if days("audit_days"):
        c = _cutoff(now, days("audit_days"))
        out["audit_entries_deleted"] = (
            _delete_by_id(db, AuditLog, select(AuditLog.id).where(AuditLog.created_at < c))
            + _delete_by_id(db, LoginHistory, select(LoginHistory.id).where(LoginHistory.created_at < c)))
    expired = now - EXPIRED_GRACE
    out["expired_credentials_deleted"] = (
        _delete_by_id(db, UserSession, select(UserSession.id).where(UserSession.expires_at < expired))
        + _delete_by_id(db, UserToken, select(UserToken.id).where(UserToken.expires_at < expired))
        + _delete_by_id(db, OAuthState, select(OAuthState.id).where(OAuthState.expires_at < expired)))
    from app.core.ratelimit import purge_expired

    out["rate_limit_windows_deleted"] = purge_expired(db, now.timestamp())
    if any(out.values()):
        log.info("retention: %s", ", ".join(f"{k}={v}" for k, v in out.items() if v))
    return out
