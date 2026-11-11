"""Workflow engine: node execution, branching, loops, waits, retries, recovery."""
from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import select

from app.db import utcnow
from app.engine.graph import has_errors, validate_definition
from app.engine.queue import process_job, reclaim_stale
from app.models import ApprovalRequest, ExecutionQueueJob, WorkflowExecution
from tests.conftest import make_workflow, run, steps, wf_def

TRIG = {"id": "t", "type": "trigger.manual"}


def cond(left, op, right=None):
    return {"combinator": "and", "conditions": [{"left": left, "operator": op, "right": right}]}


def test_linear_workflow_executes_and_persists_steps(db, org, worker):
    wf = make_workflow(db, org, wf_def(
        [TRIG, {"id": "x", "type": "logic.transform", "config": {"fields": {"greeting": "Hello {{ trigger.name }}", "n": "{{ trigger.n }}"}}}],
        [("t", "x")]))
    ex = run(db, worker, wf, {"name": "Ada", "n": 3})
    assert ex.status == "COMPLETED"
    assert ex.output == {"x": {"greeting": "Hello Ada", "n": 3}}  # single-expression templates keep their type
    st = steps(db, ex)
    assert [(s.node_key, s.status) for s in st] == [("t", "COMPLETED"), ("x", "COMPLETED")]
    assert ex.duration_ms is not None and ex.started_at and ex.finished_at


def test_if_else_takes_one_branch_and_skips_the_other(db, org, worker):
    d = wf_def(
        [TRIG, {"id": "if", "type": "logic.if", "config": {"condition": cond("{{ trigger.amount }}", "gt", 100)}},
         {"id": "big", "type": "logic.transform", "config": {"fields": {"size": "big"}}},
         {"id": "small", "type": "logic.transform", "config": {"fields": {"size": "small"}}},
         {"id": "merge", "type": "logic.merge", "config": {"mode": "object"}}],
        [("t", "if"), ("if", "big", "true"), ("if", "small", "false"), ("big", "merge"), ("small", "merge")])
    wf = make_workflow(db, org, d)
    ex = run(db, worker, wf, {"amount": 250})
    assert ex.status == "COMPLETED"
    assert ex.state["status"] == {"t": "completed", "if": "completed", "big": "completed", "small": "skipped", "merge": "completed"}
    assert ex.output["merge"]["merged"] == {"size": "big"}
    ex2 = run(db, worker, wf, {"amount": 5})
    assert ex2.output["merge"]["merged"] == {"size": "small"}


def test_switch_router_and_validate(db, org, worker):
    d = wf_def(
        [TRIG,
         {"id": "v", "type": "logic.validate", "config": {"rules": [{"field": "email", "value": "{{ trigger.email }}", "type": "email"}]}},
         {"id": "sw", "type": "logic.switch", "config": {"value": "{{ trigger.plan }}", "cases": ["pro", "free"]}},
         {"id": "pro", "type": "logic.transform", "config": {"fields": {"tier": "pro"}}},
         {"id": "other", "type": "logic.transform", "config": {"fields": {"tier": "other"}}},
         {"id": "bad", "type": "logic.transform", "config": {"fields": {"errors": "{{ nodes.v.errors }}"}}}],
        [("t", "v"), ("v", "sw", "valid"), ("v", "bad", "invalid"), ("sw", "pro", "pro"), ("sw", "other", "default")])
    wf = make_workflow(db, org, d)
    assert run(db, worker, wf, {"email": "a@b.co", "plan": "PRO"}).output == {"pro": {"tier": "pro"}}
    assert run(db, worker, wf, {"email": "a@b.co", "plan": "enterprise"}).output == {"other": {"tier": "other"}}
    assert run(db, worker, wf, {"email": "nope", "plan": "pro"}).output == {"bad": {"errors": ["email must be a valid email"]}}


def test_loop_runs_body_per_item(db, org, worker):
    d = wf_def(
        [TRIG, {"id": "loop", "type": "logic.loop", "config": {"items": "{{ trigger.items }}"}},
         {"id": "body", "type": "logic.transform", "config": {"fields": {"doubled": "{{ loop.item | multiply(2) }}", "i": "{{ loop.index }}"}}},
         {"id": "after", "type": "logic.transform", "config": {"fields": {"count": "{{ nodes.loop.count }}"}}}],
        [("t", "loop"), ("loop", "body", "each"), ("loop", "after", "done")])
    wf = make_workflow(db, org, d)
    ex = run(db, worker, wf, {"items": [1, 2, 3]})
    assert ex.status == "COMPLETED", ex.error
    assert ex.state["outputs"]["loop"]["count"] == 3
    assert [r["outputs"]["body"]["doubled"] for r in ex.state["outputs"]["loop"]["results"]] == [2, 4, 6]
    assert ex.output == {"after": {"count": 3}}
    assert [s.iteration for s in steps(db, ex) if s.node_key == "body"] == ["loop:0", "loop:1", "loop:2"]


def test_http_node_and_error_handle(db, org, worker, http_mock):
    http_mock.add("GET", "https://api.example.com/ok", (200, {"value": 42}))
    http_mock.add("GET", "https://api.example.com/broken", (400, {"detail": "bad"}))
    d = wf_def(
        [TRIG, {"id": "ok", "type": "action.http_request", "config": {"url": "https://api.example.com/ok"}},
         {"id": "broken", "type": "action.http_request", "config": {"url": "https://api.example.com/broken"}},
         {"id": "handled", "type": "logic.transform", "config": {"fields": {"code": "{{ nodes.broken.error.code }}", "value": "{{ nodes.ok.body.value }}"}}}],
        [("t", "ok"), ("ok", "broken"), ("broken", "handled", "error")])
    ex = run(db, worker, make_workflow(db, org, d))
    assert ex.status == "COMPLETED"
    assert ex.output == {"handled": {"code": "http_400", "value": 42}}


def test_unhandled_failure_fails_execution_with_node_reference(db, org, worker, http_mock):
    http_mock.add("GET", "https://api.example.com/broken", (404, {}))
    d = wf_def([TRIG, {"id": "broken", "name": "Fetch", "type": "action.http_request", "config": {"url": "https://api.example.com/broken"}}], [("t", "broken")])
    ex = run(db, worker, make_workflow(db, org, d))
    assert ex.status == "FAILED"
    assert ex.error["node_id"] == "broken" and ex.error["code"] == "http_404"


def test_ssrf_is_blocked(db, org, worker):
    d = wf_def([TRIG, {"id": "h", "type": "action.http_request", "config": {"url": "http://169.254.169.254/latest/meta-data"}}], [("t", "h")])
    ex = run(db, worker, make_workflow(db, org, d))
    assert ex.status == "FAILED" and ex.error["code"] == "ssrf_blocked"
    d2 = wf_def([TRIG, {"id": "h", "type": "action.http_request", "config": {"url": "http://localhost:8000/api/v1/health"}}], [("t", "h")])
    assert run(db, worker, make_workflow(db, org, d2, name="w2")).error["code"] == "ssrf_blocked"


def test_transient_failure_retries_with_backoff_only_when_allowed(db, org, worker, http_mock):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(503 if calls["n"] < 3 else 200, json={"n": calls["n"]})

    http_mock.add("POST", "https://api.example.com/flaky", flaky)
    node = {"id": "h", "type": "action.http_request", "config": {"url": "https://api.example.com/flaky", "method": "POST"}}
    # Non-idempotent node without explicit permission: no automatic retry.
    ex = run(db, worker, make_workflow(db, org, wf_def([TRIG, node], [("t", "h")]), name="no-retry"))
    assert ex.status == "FAILED" and calls["n"] == 1
    # Explicitly allowed: durable retry through the queue with back-off.
    calls["n"] = 0
    node2 = {**node, "settings": {"retry": {"max_attempts": 4, "backoff_seconds": 0.01, "allow_non_idempotent": True}}}
    wf = make_workflow(db, org, wf_def([TRIG, node2], [("t", "h")]), name="retry")
    ex = run(db, worker, wf)
    for _ in range(5):
        if ex.status != "RETRYING":
            break
        db.execute(ExecutionQueueJob.__table__.update().values(run_at=utcnow() - timedelta(seconds=1)))
        db.commit()
        worker.drain()
        db.expire_all()
        ex = db.get(WorkflowExecution, ex.id)
    assert ex.status == "COMPLETED" and calls["n"] == 3
    assert [s.status for s in steps(db, ex) if s.node_key == "h"] == ["FAILED", "FAILED", "COMPLETED"]


def test_delay_suspends_and_resumes_durably(db, org, worker):
    d = wf_def([TRIG, {"id": "d", "type": "logic.delay", "config": {"seconds": 3600}},
                {"id": "x", "type": "logic.transform", "config": {"fields": {"done": True}}}], [("t", "d"), ("d", "x")])
    ex = run(db, worker, make_workflow(db, org, d))
    assert ex.status == "WAITING" and "d" in ex.state["waiting"]
    job = db.scalar(select(ExecutionQueueJob).where(ExecutionQueueJob.execution_id == ex.id, ExecutionQueueJob.status == "QUEUED"))
    assert job.run_at > utcnow() + timedelta(minutes=59)
    assert worker.drain() == 0  # nothing is due yet
    job.run_at = utcnow() - timedelta(seconds=1)
    db.commit()
    worker.drain()
    db.expire_all()
    ex = db.get(WorkflowExecution, ex.id)
    assert ex.status == "COMPLETED" and ex.output == {"x": {"done": True}}


def test_approval_node_waits_for_human_decision(db, org, worker):
    from app.services.approvals import decide_approval
    from tests.conftest import make_user

    d = wf_def([TRIG, {"id": "a", "type": "logic.approval", "config": {"title": "Refund {{ trigger.amount }}"}},
                {"id": "yes", "type": "logic.transform", "config": {"fields": {"refunded": True}}},
                {"id": "no", "type": "logic.transform", "config": {"fields": {"refunded": False}}}],
               [("t", "a"), ("a", "yes", "approved"), ("a", "no", "rejected")])
    wf = make_workflow(db, org, d)
    ex = run(db, worker, wf, {"amount": 50})
    assert ex.status == "WAITING"
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.execution_id == ex.id))
    assert req.status == "PENDING" and req.title == "Refund 50"
    user = make_user(db, "boss@weakless.pro", platform_role="ADMIN")
    decide_approval(db, req, approved=True, user_id=user.id, note="ok")
    db.commit()
    worker.drain()
    db.expire_all()
    ex = db.get(WorkflowExecution, ex.id)
    assert ex.status == "COMPLETED" and ex.output == {"yes": {"refunded": True}}


def test_cancel_waiting_execution(db, org, worker):
    from app.services.executions import cancel_execution

    d = wf_def([TRIG, {"id": "w", "type": "logic.wait", "config": {}}], [("t", "w")])
    ex = run(db, worker, make_workflow(db, org, d))
    assert ex.status == "WAITING"
    cancel_execution(db, ex)
    db.commit()
    assert db.get(WorkflowExecution, ex.id).status == "CANCELLED"


def test_external_wait_resume(db, org, worker):
    from app.services.executions import resume_execution

    d = wf_def([TRIG, {"id": "w", "type": "logic.wait", "config": {}},
                {"id": "x", "type": "logic.transform", "config": {"fields": {"answer": "{{ nodes.w.data.answer }}"}}}], [("t", "w"), ("w", "x")])
    ex = run(db, worker, make_workflow(db, org, d))
    resume_execution(db, ex, {"answer": "yes"})
    db.commit()
    worker.drain()
    db.expire_all()
    assert db.get(WorkflowExecution, ex.id).output == {"x": {"answer": "yes"}}


def test_idempotency_key_prevents_duplicate_executions(db, org, worker):
    from app.services.executions import start_execution

    wf = make_workflow(db, org, wf_def([TRIG], []))
    a = start_execution(db, wf, trigger_type="manual", payload={}, idempotency_key="order-1")
    b = start_execution(db, wf, trigger_type="manual", payload={}, idempotency_key="order-1")
    db.commit()
    assert a.id == b.id
    assert db.scalar(select(ExecutionQueueJob).where(ExecutionQueueJob.execution_id == a.id)) is not None


def test_crash_recovery_does_not_repeat_non_idempotent_step(db, org, worker, http_mock):
    """A worker that dies mid-step leaves a CLAIMED job. After the visibility
    timeout the job is recovered: idempotent steps re-run, others fail safely."""
    from app.services.executions import start_execution

    http_mock.add("POST", "https://api.example.com/charge", (200, {"ok": True}))
    d = wf_def([TRIG, {"id": "charge", "type": "action.http_request", "config": {"url": "https://api.example.com/charge", "method": "POST"}}], [("t", "charge")])
    wf = make_workflow(db, org, d)
    ex = start_execution(db, wf, trigger_type="manual", payload={})
    db.commit()
    # Simulate the crash: job claimed long ago, execution RUNNING with the step marked in-flight.
    job = db.scalar(select(ExecutionQueueJob).where(ExecutionQueueJob.execution_id == ex.id))
    job.status, job.locked_by, job.locked_at, job.attempts = "CLAIMED", "dead-worker", utcnow() - timedelta(hours=1), 1
    ex.status = "RUNNING"
    ex.state = {"started": True, "outputs": {"t": {}}, "status": {"t": "completed"}, "edges": {"0": "taken"}, "waiting": {}, "attempts": {}, "seq": 1,
                "running": {"node": "charge", "safe": False, "step": None}}
    db.commit()
    assert reclaim_stale(db) == 1
    worker.drain()
    db.expire_all()
    ex = db.get(WorkflowExecution, ex.id)
    assert ex.status == "FAILED" and ex.error["code"] == "interrupted_non_idempotent"
    assert len(http_mock.requests) == 0  # the charge was NOT repeated


def test_dead_letter_after_repeated_infrastructure_failure(db, org, worker, monkeypatch):
    from app.engine import queue as q
    from app.services.executions import start_execution

    wf = make_workflow(db, org, wf_def([TRIG], []))
    ex = start_execution(db, wf, trigger_type="manual", payload={})
    db.commit()

    def boom(*a, **k):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(q, "run_execution", boom)
    for _ in range(5):
        db.execute(ExecutionQueueJob.__table__.update().where(ExecutionQueueJob.status == "QUEUED").values(run_at=utcnow() - timedelta(seconds=1)))
        db.commit()
        worker.drain()
    db.expire_all()
    job = db.scalar(select(ExecutionQueueJob).where(ExecutionQueueJob.execution_id == ex.id))
    assert job.status == "DEAD" and job.attempts == job.max_attempts
    assert db.get(WorkflowExecution, ex.id).status == "FAILED"


def test_validation_catches_structural_problems():
    issues = validate_definition(wf_def([{"id": "a", "type": "logic.transform"}], []))
    assert any("at least one trigger" in i["message"] for i in issues)
    cyc = wf_def([TRIG, {"id": "a", "type": "logic.transform"}, {"id": "b", "type": "logic.transform"}], [("t", "a"), ("a", "b"), ("b", "a")])
    assert any("cycle" in i["message"] for i in validate_definition(cyc))
    bad_handle = wf_def([TRIG, {"id": "a", "type": "logic.transform"}], [("t", "a", "nope")])
    assert has_errors(validate_definition(bad_handle))
    unknown = wf_def([TRIG, {"id": "a", "type": "system.exec", "config": {"cmd": "rm -rf /"}}], [("t", "a")])
    assert any("Unknown node type" in i["message"] for i in validate_definition(unknown))
    bad_cfg = wf_def([TRIG, {"id": "a", "type": "action.http_request", "config": {"url": "https://x.example", "method": "TRACE"}}], [("t", "a")])
    assert has_errors(validate_definition(bad_cfg))
    loop_delay = wf_def([TRIG, {"id": "l", "type": "logic.loop", "config": {"items": [1]}}, {"id": "d", "type": "logic.delay", "config": {"seconds": 5}}],
                        [("t", "l"), ("l", "d", "each")])
    assert any("inside a loop" in i["message"] for i in validate_definition(loop_delay))


def test_every_registered_node_type_describes_itself():
    from app.engine.registry import all_specs

    specs = all_specs()
    types = {s.type for s in specs}
    for required in ["trigger.webhook", "trigger.schedule", "trigger.manual", "trigger.form", "trigger.database_event", "trigger.email",
                     "trigger.message", "trigger.payment", "trigger.app_event", "action.send_email", "action.send_whatsapp",
                     "action.send_telegram", "action.send_discord", "action.crm_create_lead", "action.crm_update_record",
                     "action.db_create_record", "action.db_update_record", "action.http_request", "action.call_api", "action.ai_response",
                     "action.generate_document", "action.upload_file", "action.create_calendar_event", "action.send_notification",
                     "logic.if", "logic.switch", "logic.filter", "logic.router", "logic.delay", "logic.wait", "logic.loop", "logic.merge",
                     "logic.transform", "logic.validate", "ai.agent", "ai.completion", "ai.classify", "ai.extract", "ai.summarize",
                     "ai.translate", "ai.decision", "ai.tool_call", "ai.kb_search"]:
        assert required in types, required
    for s in specs:
        d = s.describe()
        assert d["config_schema"]["type"] == "object" and "output_schema" in d


def test_all_library_templates_are_valid_workflows():
    from app.templates.library import PACKAGES, TEMPLATES

    keys = {t["key"] for t in TEMPLATES}
    for t in TEMPLATES:
        issues = [i for i in validate_definition(t["definition"]) if i["level"] == "error"]
        assert not issues, (t["key"], issues)
    for p in PACKAGES:
        assert set(p["templates"]) <= keys and len(p["templates"]) >= 6


def test_process_job_missing_is_harmless():
    import uuid

    assert process_job(uuid.uuid4()) == "missing"
