"""Data retention: old history is removed in bounded sweeps, recent data and totals are untouched."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.db import utcnow
from app.models import (
    AuditLog, ExecutionQueueJob, Notification, SystemSetting, UsageRecord, User, UserToken, WebhookEvent, WorkflowExecution,
    WorkflowExecutionStep,
)
from app.services.retention import DEFAULT_POLICY, run_retention
from app.services.usage import usage_totals
from tests.conftest import login, make_user, make_workflow, run, steps, wf_def

DEF = wf_def([{"id": "t", "type": "trigger.manual"}, {"id": "x", "type": "logic.transform", "config": {"fields": {"who": "{{ trigger.name }}"}}}],
             [("t", "x")])


def _age(db, ex, days: int) -> None:
    ex.finished_at = utcnow() - timedelta(days=days)
    db.commit()


def test_execution_details_are_removed_then_old_executions_are_deleted(client, db, org, worker):
    wf = make_workflow(db, org, DEF)
    old, recent, waiting = (run(db, worker, wf, {"name": n}) for n in ("Old", "Recent", "Paused"))
    assert old.status == recent.status == "COMPLETED" and steps(db, old)
    _age(db, old, 45)
    _age(db, recent, 2)
    waiting.status = "WAITING"  # never finished: kept whatever its age
    _age(db, waiting, 400)

    result = run_retention(db)
    assert result["execution_details_removed"] == 1 and result["executions_deleted"] == 0
    db.expire_all()
    old = db.get(WorkflowExecution, old.id)
    assert old.details_purged_at and old.trigger_payload == {} and old.output is None and old.state == {}
    assert old.status == "COMPLETED" and old.duration_ms is not None  # the summary row stays for history and analytics
    assert steps(db, old) == [] and len(steps(db, recent)) == 2 and recent.trigger_payload == {"name": "Recent"}

    make_user(db, "root@weakless.pro", platform_role="SUPER_ADMIN")
    root = login(client, "root@weakless.pro")
    detail = client.get(f"/api/v1/executions/{old.id}", headers=root).json()
    assert detail["details_purged_at"] and detail["steps"] == []
    assert client.get(f"/api/v1/executions/{recent.id}", headers=root).json()["details_purged_at"] is None
    old.status = "FAILED"
    db.commit()
    retry = client.post(f"/api/v1/executions/{old.id}/retry", headers=root)
    assert retry.status_code == 409 and "retention" in retry.json()["error"]["message"]

    assert run_retention(db)["execution_details_removed"] == 0  # idempotent
    old_id, recent_id, waiting_id = old.id, recent.id, waiting.id
    later = run_retention(db, now=utcnow() + timedelta(days=400))
    assert later["executions_deleted"] == 2  # old and recent; the waiting one is never deleted
    db.expire_all()
    assert db.get(WorkflowExecution, old_id) is None and db.get(WorkflowExecution, waiting_id) is not None
    assert db.scalar(select(func.count()).select_from(WorkflowExecutionStep).where(WorkflowExecutionStep.execution_id == recent_id)) == 0


def test_usage_detail_is_compacted_without_changing_totals(db, org):
    wf_a, wf_b = uuid.uuid4(), uuid.uuid4()
    day = (utcnow() - timedelta(days=80)).replace(hour=0, minute=0, second=0, microsecond=0)
    period = day.strftime("%Y-%m")
    for i in range(6):
        db.add(UsageRecord(organization_id=org.id, metric="executions", quantity=Decimal(1), period=period,
                           workflow_id=wf_a if i % 2 else wf_b, occurred_at=day + timedelta(hours=i)))
    for i in range(3):
        db.add(UsageRecord(organization_id=org.id, metric="ai_input_tokens", quantity=Decimal("100.5"), period=period,
                           occurred_at=day + timedelta(hours=i, minutes=5)))
    recent = utcnow() - timedelta(days=1)
    for _ in range(2):
        db.add(UsageRecord(organization_id=org.id, metric="executions", quantity=Decimal(1), period=recent.strftime("%Y-%m"), occurred_at=recent))
    db.commit()
    before_old, before_new = usage_totals(db, org.id, period), usage_totals(db, org.id, recent.strftime("%Y-%m"))

    assert run_retention(db)["usage_records_merged"] == 6  # 6 -> 2 rows (one per workflow), 3 -> 1
    assert usage_totals(db, org.id, period) == before_old and usage_totals(db, org.id, recent.strftime("%Y-%m")) == before_new
    old_rows = db.scalars(select(UsageRecord).where(UsageRecord.period == period)).all()
    assert len(old_rows) == 3 and all(r.occurred_at.date() == day.date() for r in old_rows)
    assert sorted(r.meta["compacted_records"] for r in old_rows) == [3, 3, 3]
    assert run_retention(db)["usage_records_merged"] == 0


def test_events_notifications_jobs_and_expired_links_are_swept(db, org):
    long_ago, now = utcnow() - timedelta(days=200), utcnow()
    user = make_user(db, "someone@example.com")
    db.add_all([
        WebhookEvent(organization_id=org.id, source="app", event_type="old", received_at=long_ago),
        WebhookEvent(organization_id=org.id, source="app", event_type="new", received_at=now),
        Notification(user_id=user.id, event_type="x", title="old", created_at=long_ago),
        Notification(user_id=user.id, event_type="x", title="new", created_at=now),
        ExecutionQueueJob(organization_id=org.id, status="DONE", finished_at=long_ago),
        ExecutionQueueJob(organization_id=org.id, status="DEAD", finished_at=long_ago, last_error="kept for operators"),
        UserToken(user_id=user.id, purpose="password_reset", token_hash="a" * 64, expires_at=long_ago),
        UserToken(user_id=user.id, purpose="password_reset", token_hash="b" * 64, expires_at=now + timedelta(hours=1)),
        AuditLog(organization_id=org.id, action="old.thing", created_at=long_ago),
    ])
    db.commit()
    result = run_retention(db)
    assert result["events_deleted"] == 1 and result["notifications_deleted"] == 1 and result["queue_jobs_deleted"] == 1
    assert result["expired_credentials_deleted"] == 1 and "audit_entries_deleted" not in result  # audit is kept by default
    assert db.scalars(select(WebhookEvent.event_type)).all() == ["new"]
    assert db.scalars(select(Notification.title)).all() == ["new"]
    assert db.scalars(select(ExecutionQueueJob.status)).all() == ["DEAD"]
    assert db.scalar(select(func.count()).select_from(UserToken)) == 1
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "old.thing")) == 1
    assert db.get(User, user.id) is not None


def test_policy_is_validated_and_can_be_applied_from_the_api(client, db, org):
    stored = db.scalar(select(SystemSetting).where(SystemSetting.key == "data.retention"))
    assert stored.value == DEFAULT_POLICY  # seeded so administrators see the defaults
    make_user(db, "root@weakless.pro", platform_role="SUPER_ADMIN")
    root = login(client, "root@weakless.pro")
    put = lambda value: client.put("/api/v1/settings/data.retention", headers=root, json={"value": value})  # noqa: E731
    assert put({"events_days": 0}).status_code == 422
    assert put({"audit_days": 7}).status_code == 422
    assert put({"execution_details_days": 400, "executions_days": 100}).status_code == 422
    assert put({"unknown_days": 5}).status_code == 422
    assert put({"events_days": "30"}).status_code == 422
    ok = put({"audit_days": 60, "events_days": None})
    assert ok.status_code == 200 and ok.json()["value"] == {**DEFAULT_POLICY, "audit_days": 60, "events_days": None}

    db.add(WebhookEvent(organization_id=org.id, source="app", event_type="ancient", received_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    db.add(AuditLog(organization_id=org.id, action="ancient.thing", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    db.commit()
    ran = client.post("/api/v1/retention/run", headers=root)
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["policy"]["audit_days"] == 60 and "events_deleted" not in body["result"] and body["result"]["audit_entries_deleted"] >= 1
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1  # events are kept forever under this policy
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "retention.run")) == 1

    member = make_user(db, "member@example.com", org=org, role="CLIENT_ADMIN")
    assert member and client.post("/api/v1/retention/run", headers=login(client, "member@example.com")).status_code == 403
