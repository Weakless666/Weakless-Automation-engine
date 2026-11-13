"""Sub-workflows, responding to webhooks, and the data nodes."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from app.db import utcnow
from app.models import ExecutionQueueJob, WebhookEndpoint, WorkflowExecution
from app.services import workflows as wfs
from app.services.executions import cancel_execution
from tests.conftest import make_workflow, run, steps, wf_def

TRIG = {"id": "t", "type": "trigger.manual"}


def _child(db, org, *, fail: bool = False, wait: bool = False, active: bool = False):
    body = ({"id": "boom", "type": "logic.validate", "config": {"rules": [{"field": "n", "value": "{{ trigger.missing }}", "required": True}]}}
            if fail else {"id": "x", "type": "logic.transform", "config": {"fields": {"doubled": "{{ trigger.n | multiply(2) }}"}}})
    nodes = [TRIG, body] + ([{"id": "hold", "type": "logic.wait", "config": {}}] if wait else [])
    edges = [("t", body["id"])] + ([(body["id"], "hold")] if wait else [])
    if fail:  # a validation failure that nothing handles fails the child
        nodes[1] = {"id": "boom", "type": "action.http_request", "config": {"url": "http://localhost:9/nope"}}
        edges = [("t", "boom")]
    return make_workflow(db, org, wf_def(nodes, edges), name="Child", active=active)


def _parent(db, org, child, *, wait: bool = True, timeout: float = 60, error_branch: bool = False, name: str = "Parent"):
    nodes = [TRIG, {"id": "call", "type": "logic.run_workflow", "config": {"workflow_id": str(child.id), "input": {"n": 21},
                                                                           "wait_for_result": wait, "timeout_minutes": timeout}},
             {"id": "use", "type": "logic.transform", "config": {"fields": {"answer": "{{ nodes.call.output.x.doubled }}"}}}]
    edges = [("t", "call"), ("call", "use")]
    if error_branch:
        nodes.append({"id": "handle", "type": "logic.transform", "config": {"fields": {"why": "{{ nodes.call.error.message }}"}}})
        edges.append(("call", "handle", "error"))
    return make_workflow(db, org, wf_def(nodes, edges), name=name)


def test_a_workflow_calls_another_and_continues_with_its_result(db, org, worker):
    child = _child(db, org)  # published but not active: a reusable sub-process needs no trigger of its own
    parent = _parent(db, org, child)
    ex = run(db, worker, parent)
    worker.drain()
    db.expire_all()
    ex = db.get(WorkflowExecution, ex.id)
    assert ex.status == "COMPLETED" and ex.output == {"use": {"answer": 42}}
    sub = db.scalar(select(WorkflowExecution).where(WorkflowExecution.workflow_id == child.id))
    assert sub.parent_execution_id == ex.id and sub.trigger_type == "subworkflow" and sub.trigger_payload == {"n": 21}
    call = next(s for s in steps(db, ex) if s.node_key == "call")
    assert call.status == "COMPLETED" and call.output["status"] == "COMPLETED"


def test_a_failing_sub_workflow_follows_the_error_branch_or_fails_the_caller(db, org, worker):
    child = _child(db, org, fail=True)
    handled = _parent(db, org, child, error_branch=True, name="Handled")
    unhandled = _parent(db, org, child, name="Unhandled")
    a, b = run(db, worker, handled), run(db, worker, unhandled)
    worker.drain()
    db.expire_all()
    a, b = db.get(WorkflowExecution, a.id), db.get(WorkflowExecution, b.id)
    assert a.status == "COMPLETED" and "localhost" in a.output["handle"]["why"]
    assert b.status == "FAILED" and b.error["node_id"] == "call" and "localhost" in b.error["message"]


def test_calls_cannot_recurse_and_fire_and_forget_does_not_wait(db, org, worker):
    loop = make_workflow(db, org, wf_def([TRIG], []), name="Self caller")
    definition = wf_def([TRIG, {"id": "again", "type": "logic.run_workflow", "config": {"workflow_id": str(loop.id)}}], [("t", "again")])
    wfs.save_draft(db, loop, definition)
    wfs.publish(db, loop, None)
    db.commit()
    ex = run(db, worker, loop)
    assert ex.status == "FAILED" and ex.error["code"] == "recursive_call"

    child = _child(db, org)
    parent = make_workflow(db, org, wf_def([TRIG, {"id": "call", "type": "logic.run_workflow",
                                                   "config": {"workflow_id": str(child.id), "input": {"n": 1}, "wait_for_result": False}}],
                                           [("t", "call")]), name="Fire and forget")
    ex = run(db, worker, parent)
    assert ex.status == "COMPLETED" and ex.output["call"]["execution_id"]
    sub = db.get(WorkflowExecution, __import__("uuid").UUID(ex.output["call"]["execution_id"]))
    assert sub.status == "COMPLETED" and sub.parent_node_key is None


def test_a_slow_sub_workflow_times_out_and_cancelling_the_caller_cancels_it(db, org, worker):
    child = _child(db, org, wait=True)
    parent = _parent(db, org, child, timeout=1, error_branch=True)
    ex = run(db, worker, parent)
    worker.drain()
    db.expire_all()
    assert db.get(WorkflowExecution, ex.id).status == "WAITING"
    db.execute(update(ExecutionQueueJob).where(ExecutionQueueJob.execution_id == ex.id, ExecutionQueueJob.status == "QUEUED")
               .values(run_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    worker.drain()
    db.expire_all()
    ex = db.get(WorkflowExecution, ex.id)
    assert ex.status == "COMPLETED" and ex.output["handle"]["why"] == "The called workflow did not finish in time"
    worker.drain()
    db.expire_all()
    assert db.scalar(select(WorkflowExecution.status).where(WorkflowExecution.workflow_id == child.id)) == "CANCELLED"

    other = run(db, worker, _parent(db, org, child, name="Cancelled parent"))
    worker.drain()
    cancel_execution(db, db.get(WorkflowExecution, other.id))
    db.commit()
    sub = db.scalar(select(WorkflowExecution).where(WorkflowExecution.parent_execution_id == other.id))
    assert sub.cancel_requested or sub.status == "CANCELLED"


def test_retry_from_the_failed_step_does_not_repeat_what_succeeded(client, db, org, worker, http_mock):
    from tests.conftest import login, make_user
    from tests.test_platform import hdr

    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        import httpx

        return httpx.Response(500 if calls["n"] == 1 else 200, json={"ok": calls["n"] > 1})

    http_mock.add("POST", "https://api.example.com/notify", (200, {"sent": True}))
    http_mock.add("POST", "https://api.example.com/flaky", flaky)
    wf = make_workflow(db, org, wf_def([
        TRIG,
        {"id": "notify", "type": "action.http_request", "config": {"method": "POST", "url": "https://api.example.com/notify", "body": {}}},
        {"id": "flaky", "type": "action.http_request", "config": {"method": "POST", "url": "https://api.example.com/flaky", "body": {}}},
        {"id": "done", "type": "logic.transform", "config": {"fields": {"after": "{{ nodes.notify.body.sent }}"}}},
    ], [("t", "notify"), ("notify", "flaky"), ("flaky", "done")]))
    failed = run(db, worker, wf, {"order": 7})
    assert failed.status == "FAILED" and failed.error["node_id"] == "flaky"
    notify_calls = lambda: sum(1 for r in http_mock.requests if r.url.path == "/notify")  # noqa: E731
    assert notify_calls() == 1

    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    detail = client.get(f"/api/v1/executions/{failed.id}", headers=h).json()
    assert detail["can_resume"] is True
    r = client.post(f"/api/v1/executions/{failed.id}/retry?from_failed_step=true", headers=h)
    assert r.status_code == 202, r.text
    worker.drain()
    db.expire_all()
    resumed = db.get(WorkflowExecution, __import__("uuid").UUID(r.json()["id"]))
    assert resumed.status == "COMPLETED" and resumed.output == {"done": {"after": True}}  # earlier results carried over
    assert notify_calls() == 1  # the step that already succeeded was not sent again
    assert [s.node_key for s in steps(db, resumed)] == ["flaky", "done"]
    assert client.get(f"/api/v1/executions/{resumed.id}", headers=h).json()["resumed_from"]["steps"] == ["flaky"]

    ok = run(db, worker, make_workflow(db, org, wf_def([TRIG], []), name="Fine"))
    assert client.post(f"/api/v1/executions/{ok.id}/retry?from_failed_step=true", headers=h).status_code == 409


def _hook(db, org, nodes, edges, mode="respond_step", timeout=5):
    trig = {"id": "hook", "type": "trigger.webhook", "config": {"signature_scheme": "none", "response_mode": mode, "response_timeout_seconds": timeout}}
    wf = make_workflow(db, org, wf_def([trig, *nodes], edges), name=f"Hook {len(nodes)} {mode}")
    return db.scalar(select(WebhookEndpoint.endpoint_key).where(WebhookEndpoint.workflow_id == wf.id))


def test_webhook_callers_get_the_workflow_reply(client, db, org):
    greet = [{"id": "x", "type": "logic.transform", "config": {"fields": {"greeting": "Hi {{ trigger.body.name }}"}}},
             {"id": "reply", "type": "action.respond_webhook", "config": {"status_code": 201, "body": {"message": "{{ nodes.x.greeting }}", "ok": True}}}]
    key = _hook(db, org, greet, [("hook", "x"), ("x", "reply")])
    r = client.post(f"/api/v1/hooks/{key}", json={"name": "Ana"})
    assert r.status_code == 201 and r.json() == {"message": "Hi Ana", "ok": True}

    text = [{"id": "reply", "type": "action.respond_webhook", "config": {"body": "pong", "content_type": "text/plain"}}]
    r = client.post(f"/api/v1/hooks/{_hook(db, org, text, [('hook', 'reply')])}", json={})
    assert r.status_code == 200 and r.text == "pong" and r.headers["content-type"].startswith("text/plain")

    silent = [{"id": "x", "type": "logic.transform", "config": {"fields": {"a": 1}}}]  # finishes without replying
    r = client.post(f"/api/v1/hooks/{_hook(db, org, silent, [('hook', 'x')])}", json={})
    assert r.status_code == 202 and r.json()["status"] == "processed"

    immediate = _hook(db, org, greet, [("hook", "x"), ("x", "reply")], mode="immediately")
    r = client.post(f"/api/v1/hooks/{immediate}", json={"name": "Ana"})
    assert r.status_code == 202 and "execution_id" in r.json()  # the default: answer at once, run in the background


def test_list_and_date_nodes(db, org, worker):
    people = [{"name": "Ana", "city": "Varna", "spent": 120}, {"name": "Bo", "city": "Sofia", "spent": 40},
              {"name": "ana", "city": "Varna", "spent": 60}, {"name": "Cvet", "city": "Sofia", "spent": None}]
    wf = make_workflow(db, org, wf_def([
        TRIG,
        {"id": "top", "type": "data.sort", "config": {"items": "{{ trigger.people }}", "sort_by": "spent", "order": "descending", "limit": 3}},
        {"id": "uniq", "type": "data.dedupe", "config": {"items": "{{ trigger.people }}", "by": "name"}},
        {"id": "per_city", "type": "data.aggregate", "config": {"items": "{{ trigger.people }}", "group_by": "city",
                                                                "metrics": [{"op": "count"}, {"op": "sum", "field": "spent", "name": "total"}]}},
        {"id": "month", "type": "data.datetime", "config": {"operation": "add", "value": "2026-01-31T10:00:00+00:00", "amount": 1, "unit": "months",
                                                            "format": "%d.%m.%Y"}},
        {"id": "gap", "type": "data.datetime", "config": {"operation": "difference", "value": "2026-11-01 09:00", "other": "2026-11-03 21:00",
                                                          "timezone": "Europe/Sofia"}},
        {"id": "week", "type": "data.datetime", "config": {"operation": "start_of", "period": "week", "value": "2026-11-13T22:30:00+00:00",
                                                           "timezone": "Europe/Sofia"}},
    ], [("t", "top"), ("t", "uniq"), ("t", "per_city"), ("t", "month"), ("t", "gap"), ("t", "week")]))
    ex = run(db, worker, wf, {"people": people})
    assert ex.status == "COMPLETED", ex.error
    out = ex.output
    assert [p["name"] for p in out["top"]["items"]] == ["Ana", "ana", "Bo"]  # missing values sort last
    assert out["uniq"]["count"] == 3 and out["uniq"]["removed"] == 1  # "Ana" and "ana" are the same name
    assert {g["key"]: (g["count"], g["total"]) for g in out["per_city"]["groups"]} == {"Varna": (2, 180), "Sofia": (2, 40)}
    assert out["month"]["date"] == "2026-02-28" and out["month"]["formatted"] == "28.02.2026"  # end of month is respected
    assert out["gap"]["difference"]["hours"] == 60
    assert out["week"]["date"] == "2026-11-09" and out["week"]["iso"].endswith("+02:00")  # 00:30 Saturday in Sofia; week starts Monday
