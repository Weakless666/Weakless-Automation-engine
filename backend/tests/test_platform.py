"""End-to-end journey plus agents, templates, billing, analytics, schedules and events."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.ai import providers as ai_providers
from app.db import utcnow
from app.models import (
    AIAgentExecution, ApprovalRequest, InstalledTemplate, Notification, Subscription, UsageRecord, Workflow, WorkflowExecution,
    WorkflowSchedule,
)
from tests.conftest import ADMIN_PASSWORD, login, make_user, make_workflow, run, wf_def

TRIG = {"id": "t", "type": "trigger.manual"}


def hdr(auth: dict, org_id) -> dict:
    return {**auth, "X-Organization-Id": str(org_id)}


class Block(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


class FakeAnthropic:
    """Stands in for ``anthropic.Anthropic``: returns scripted SDK-shaped responses and records requests."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(item):
            item = item(kwargs)
        blocks = []
        if isinstance(item, str):
            blocks.append(Block(type="text", text=item))
        else:
            if item.get("text"):
                blocks.append(Block(type="text", text=item["text"]))
            for i, (name, inp) in enumerate(item.get("tools", [])):
                blocks.append(Block(type="tool_use", id=f"toolu_{len(self.calls)}_{i}", name=name, input=inp))
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop, model=kwargs["model"],
                               usage=SimpleNamespace(input_tokens=120, output_tokens=30, cache_creation_input_tokens=0, cache_read_input_tokens=0))


@pytest.fixture
def fake_ai(monkeypatch):
    def install(script):
        fake = FakeAnthropic(script)
        monkeypatch.setattr(ai_providers.AnthropicProvider, "client_factory", lambda self, secret: fake)
        return fake

    return install


# ----------------------------------------------------------------------------- the complete journey


def test_end_to_end_journey(client, db, worker, http_mock, fake_ai):
    """Admin creates an organization -> adds a client member -> engineer installs a template ->
    configures the integration -> activates -> a trigger starts an execution -> actions run ->
    results are stored -> the client sees history -> the admin sees analytics."""
    ai = fake_ai(["Check-in starts at 15:00 and breakfast is served 7-10."])
    http_mock.add("GET", "https://api.telegram.org/", (200, {"ok": True, "result": {"username": "seaview_bot"}}))
    http_mock.add("POST", "https://api.telegram.org/", (200, {"ok": True, "result": {"message_id": 501}}))
    make_user(db, "admin@weakless.pro", platform_role="SUPER_ADMIN")
    make_user(db, "engineer@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    admin = login(client, "admin@weakless.pro")

    # 1. Admin creates an organization.
    org = client.post("/api/v1/organizations", headers=admin, json={
        "name": "Seaview Hotel", "industry": "hospitality", "mode": "saas", "client": {"contact_email": "gm@seaview-hotel.com"}})
    assert org.status_code == 201, org.text
    org_id = org.json()["id"]

    # 2. Admin adds a client member.
    member = client.post("/api/v1/members", headers=hdr(admin, org_id), json={
        "email": "manager@seaview-hotel.com", "full_name": "Hotel Manager", "role": "CLIENT_ADMIN", "password": ADMIN_PASSWORD})
    assert member.status_code == 201, member.text

    # 3-4. Engineer configures the integration and installs + configures a template.
    eng = login(client, "engineer@weakless.pro")
    h = hdr(eng, org_id)
    integration = client.post("/api/v1/integrations", headers=h, json={"provider_key": "telegram", "values": {"bot_token": "111222333:" + "B" * 35}})
    assert integration.status_code == 201 and integration.json()["status"] == "connected"
    templates = client.get("/api/v1/templates?industry=hospitality", headers=eng).json()["items"]
    template = next(t for t in templates if t["key"] == "hosp-guest-assistant")
    preview = client.get(f"/api/v1/templates/{template['id']}", headers=eng).json()
    assert preview["definition"]["nodes"] and "business_info" in preview["config_schema"]["properties"]
    installed = client.post(f"/api/v1/templates/{template['id']}/install", headers=h, json={"config": {
        "messaging_integration": integration.json()["id"], "business_name": "Seaview Hotel",
        "business_info": "Check-in from 15:00. Breakfast 7-10."}})
    assert installed.status_code == 201, installed.text
    assert installed.json()["missing_config"] == []
    wf_id = installed.json()["workflow_id"]

    # 5. Test, publish and activate the workflow.
    detail = client.get(f"/api/v1/workflows/{wf_id}", headers=h).json()
    assert not [i for i in detail["issues"] if i["level"] == "error"]
    assert client.post(f"/api/v1/workflows/{wf_id}/publish", headers=h, json={"notes": "initial"}).json()["version"] == 1
    assert client.post(f"/api/v1/workflows/{wf_id}/activate", headers=h).json()["is_active"] is True
    endpoint = client.get(f"/api/v1/workflows/{wf_id}", headers=h).json()["triggers"]["endpoints"][0]
    secret = client.post(f"/api/v1/webhooks/{endpoint['id']}/rotate-secret", headers=h).json()["secret"]

    # 6. A Telegram message arrives on the trigger URL.
    path = endpoint["url"].split("localhost:8000")[-1]
    update = {"update_id": 9001, "message": {"text": "What time is check-in?", "chat": {"id": 4242}}}
    forged = client.post(path, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "guess"})
    assert forged.status_code == 401
    hook = client.post(path, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert hook.status_code == 202, hook.text
    execution_id = hook.json()["execution_id"]

    # 7. The worker executes the workflow: AI writes the reply, Telegram sends it.
    assert worker.drain() >= 1
    assert ai.calls[0]["model"] == "claude-opus-5-5" and "Check-in from 15:00" in ai.calls[0]["system"]
    assert "temperature" not in ai.calls[0]
    sent = [r for r in http_mock.requests if r.url.path.endswith("/sendMessage")]
    assert len(sent) == 1
    assert json.loads(sent[0].content) == {"chat_id": "4242", "text": "Check-in starts at 15:00 and breakfast is served 7-10."}

    # 8. Results are stored.
    stored = client.get(f"/api/v1/executions/{execution_id}", headers=h).json()
    assert stored["status"] == "COMPLETED" and stored["trigger_type"] == "message"
    assert [s["node_id"] for s in stored["steps"]] == ["trigger", "has_text", "reply", "send"]
    assert all(s["status"] == "COMPLETED" for s in stored["steps"])
    assert stored["output"]["send"] == {"message_id": 501}
    assert "B" * 35 not in json.dumps(stored)  # the bot token never appears in stored execution data

    # 9. The client sees the execution history of their own organization.
    manager = login(client, "manager@seaview-hotel.com")
    me = client.get("/api/v1/auth/me", headers=manager).json()
    assert [o["name"] for o in me["organizations"]] == ["Seaview Hotel"] and me["is_staff"] is False
    history = client.get("/api/v1/executions", headers=hdr(manager, org_id)).json()
    assert history["total"] == 1 and history["items"][0]["id"] == execution_id and history["items"][0]["status"] == "COMPLETED"
    assert client.get(f"/api/v1/executions/{execution_id}", headers=hdr(manager, org_id)).status_code == 200

    # 10. The admin sees analytics computed from the stored data.
    overview = client.get("/api/v1/analytics/overview", headers=admin).json()
    assert overview["organizations"]["total"] == 1
    assert overview["executions"]["total"] == 1 and overview["executions"]["completed"] == 1 and overview["executions"]["success_rate"] == 100.0
    assert overview["ai"]["requests"] == 1 and overview["ai"]["total_tokens"] == 150
    assert overview["ai"]["estimated_cost_usd"] == pytest.approx(120 * 4 / 1e6 + 30 * 20 / 1e6, abs=1e-6)
    assert overview["workflows"]["active"] == 1 and overview["integrations"]["connected"] == 1
    assert overview["client_activity"][0]["organization_name"] == "Seaview Hotel"
    series = client.get("/api/v1/analytics/timeseries", headers=hdr(admin, org_id)).json()["items"]
    assert sum(d["total"] for d in series) == 1
    usage = client.get("/api/v1/usage", headers=hdr(admin, org_id)).json()
    assert usage["totals"]["executions"] == 1 and usage["totals"]["ai_requests"] == 1
    audit = client.get("/api/v1/audit-logs", headers=hdr(admin, org_id)).json()
    assert {"organization.created", "member.added", "integration.created", "template.installed", "workflow.published", "workflow.activated"} \
        <= {a["action"] for a in audit["items"]}


# ----------------------------------------------------------------------------- workflow API details


def test_builder_api_draft_publish_versions_and_test_run(client, db, org, worker):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    types = client.get("/api/v1/node-types", headers=h).json()["items"]
    assert {t["category"] for t in types} == {"trigger", "action", "logic", "ai"} and all("config_schema" in t for t in types)
    wf = client.post("/api/v1/workflows", headers=h, json={"name": "Builder flow"}).json()
    d1 = wf_def([TRIG, {"id": "x", "type": "logic.transform", "config": {"fields": {"v": 1}}}], [("t", "x")])
    saved = client.put(f"/api/v1/workflows/{wf['id']}/draft", headers=h, json={"definition": d1, "base_revision": 1})
    assert saved.status_code == 200 and saved.json()["issues"] == []
    stale = client.put(f"/api/v1/workflows/{wf['id']}/draft", headers=h, json={"definition": d1, "base_revision": 1})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_draft"
    # The draft is persisted server-side, not in frontend state.
    assert client.get(f"/api/v1/workflows/{wf['id']}", headers=h).json()["definition"]["nodes"][1]["config"] == {"fields": {"v": 1}}
    # Test-run the draft before publishing.
    test = client.post(f"/api/v1/workflows/{wf['id']}/test", headers=h, json={"input": {}}).json()
    worker.drain()
    assert client.get(f"/api/v1/executions/{test['id']}", headers=h).json()["output"] == {"x": {"v": 1}}
    assert client.post(f"/api/v1/workflows/{wf['id']}/execute", headers=h).status_code == 409  # not published yet
    v1 = client.post(f"/api/v1/workflows/{wf['id']}/publish", headers=h).json()
    # Edit the draft: the published version keeps running unchanged until the next publish.
    d2 = wf_def([TRIG, {"id": "x", "type": "logic.transform", "config": {"fields": {"v": 2}}}], [("t", "x")])
    client.put(f"/api/v1/workflows/{wf['id']}/draft", headers=h, json={"definition": d2})
    assert client.get(f"/api/v1/workflows/{wf['id']}", headers=h).json()["has_unpublished_changes"] is True
    first = client.post(f"/api/v1/workflows/{wf['id']}/execute", headers={**h, "Idempotency-Key": "abc"}).json()
    again = client.post(f"/api/v1/workflows/{wf['id']}/execute", headers={**h, "Idempotency-Key": "abc"}).json()
    assert first["id"] == again["id"]
    worker.drain()
    assert client.get(f"/api/v1/executions/{first['id']}", headers=h).json()["output"] == {"x": {"v": 1}}
    v2 = client.post(f"/api/v1/workflows/{wf['id']}/publish", headers=h).json()
    assert (v1["version"], v2["version"]) == (1, 2)
    versions = client.get(f"/api/v1/workflows/{wf['id']}/versions", headers=h).json()["items"]
    assert [v["version"] for v in versions] == [2, 1] and versions[0]["is_current"]
    old = client.get(f"/api/v1/workflows/{wf['id']}/versions/{v1['id']}", headers=h).json()
    assert old["definition"]["nodes"][1]["config"] == {"fields": {"v": 1}}  # published versions are preserved
    assert client.post(f"/api/v1/workflows/{wf['id']}/versions/{v1['id']}/restore", headers=h).status_code == 200
    assert client.get(f"/api/v1/workflows/{wf['id']}", headers=h).json()["definition"]["nodes"][1]["config"] == {"fields": {"v": 1}}
    # Retry + cancel through the API.
    failing = wf_def([TRIG, {"id": "v", "type": "logic.wait", "config": {}}], [("t", "v")])
    wf2 = client.post("/api/v1/workflows", headers=h, json={"name": "Waiter", "definition": failing}).json()
    client.post(f"/api/v1/workflows/{wf2['id']}/publish", headers=h)
    ex = client.post(f"/api/v1/workflows/{wf2['id']}/execute", headers=h).json()
    worker.drain()
    assert client.get(f"/api/v1/executions/{ex['id']}", headers=h).json()["waiting"][0]["node_id"] == "v"
    assert client.post(f"/api/v1/executions/{ex['id']}/cancel", headers=h).json()["status"] == "CANCELLED"
    retried = client.post(f"/api/v1/executions/{ex['id']}/retry", headers=h)
    assert retried.status_code == 202 and retried.json()["retry_of_id"] == ex["id"]


def test_schedule_trigger_fires_once_per_tick(db, org, worker):
    from app.services.scheduler import run_due_schedules

    d = wf_def([{"id": "s", "type": "trigger.schedule", "config": {"cron": "*/5 * * * *", "timezone": "Europe/Sofia"}},
                {"id": "x", "type": "logic.transform", "config": {"fields": {"at": "{{ trigger.scheduled_for }}"}}}], [("s", "x")])
    wf = make_workflow(db, org, d)
    sch = db.scalar(select(WorkflowSchedule).where(WorkflowSchedule.workflow_id == wf.id))
    assert sch.is_active and sch.next_run_at > utcnow()
    assert run_due_schedules(db) == 0  # not due yet
    sch.next_run_at = utcnow() - timedelta(seconds=5)
    db.commit()
    assert run_due_schedules(db) == 1
    assert run_due_schedules(db) == 0  # the tick was consumed
    worker.drain()
    ex = db.scalar(select(WorkflowExecution).where(WorkflowExecution.workflow_id == wf.id))
    assert ex.status == "COMPLETED" and ex.trigger_type == "schedule"
    from app.services.workflows import set_active

    set_active(db, wf, False)
    db.commit()
    db.refresh(sch)
    assert sch.is_active is False and sch.next_run_at is None  # deactivation stops the schedule


def test_app_events_and_failure_notifications(client, db, org, worker, http_mock):
    """A failing workflow notifies admins and emits workflow.failed, which starts a subscribed workflow -
    without that second workflow re-triggering itself."""
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    http_mock.add("GET", "https://api.example.com/down", (500, {}))
    failing = make_workflow(db, org, wf_def([TRIG, {"id": "h", "type": "action.http_request", "config": {"url": "https://api.example.com/down"}}], [("t", "h")]), name="Fragile")
    watcher = make_workflow(db, org, wf_def(
        [{"id": "ev", "type": "trigger.app_event", "config": {"event_type": "workflow.failed"}},
         {"id": "x", "type": "logic.transform", "config": {"fields": {"failed_workflow": "{{ trigger.data.workflow_name }}"}}}], [("ev", "x")]), name="Watcher")
    custom = make_workflow(db, org, wf_def(
        [{"id": "ev", "type": "trigger.app_event", "config": {"event_type": "crm.*"}},
         {"id": "x", "type": "logic.transform", "config": {"fields": {"deal": "{{ trigger.data.deal }}"}}}], [("ev", "x")]), name="CRM listener")
    ex = run(db, worker, failing)
    assert ex.status == "FAILED"
    worker.drain()
    watched = db.scalars(select(WorkflowExecution).where(WorkflowExecution.workflow_id == watcher.id)).all()
    assert len(watched) == 1 and watched[0].output == {"x": {"failed_workflow": "Fragile"}}
    note = db.scalar(select(Notification).where(Notification.event_type == "workflow.failed"))
    assert note is not None and "Fragile" in note.title and note.severity == "error"
    # Custom application events through the API.
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    assert client.post("/api/v1/events", headers=h, json={"event_type": "workflow.failed", "data": {}}).status_code == 422  # reserved namespace
    emitted = client.post("/api/v1/events", headers=h, json={"event_type": "crm.deal_won", "data": {"deal": "D-7"}})
    assert emitted.status_code == 202 and emitted.json()["status"] == "processed"
    worker.drain()
    got = db.scalar(select(WorkflowExecution).where(WorkflowExecution.workflow_id == custom.id))
    assert got.output == {"x": {"deal": "D-7"}}
    # Event history and replay.
    events = client.get("/api/v1/events", headers=h).json()
    assert events["total"] >= 2
    inbox = client.get("/api/v1/notifications?unread=true", headers=h).json()
    assert inbox["unread"] >= 1
    assert client.post("/api/v1/notifications/read-all", headers=h).json()["updated"] >= 1


# ----------------------------------------------------------------------------- AI agents


def _agent(client, h, **over):
    body = {"name": "Front Desk", "agent_type": "receptionist", "system_instructions": "You help hotel guests.", **over}
    r = client.post("/api/v1/agents", headers=h, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_agent_tool_calling_allowlist_and_usage(client, db, org, fake_ai, http_mock):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    kb = client.post("/api/v1/knowledge-bases", headers=h, json={"name": "Hotel facts"}).json()
    client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", headers=h, json={
        "title": "Spa", "content": "The spa is open daily from 09:00 to 21:00.\n\nThe pool closes at 20:00."})
    client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", headers=h, json={"title": "Parking", "content": "Parking costs 12 EUR per night."})
    assert "spa" in client.post(f"/api/v1/knowledge-bases/{kb['id']}/search", headers=h, json={"query": "spa opening hours"}).json()["results"][0]["content"].lower()
    agent = _agent(client, h, knowledge_base_ids=[kb["id"]])
    assert [t["name"] for t in client.get(f"/api/v1/agents/{agent['id']}", headers=h).json()["tools"]] == ["knowledge_search"]

    ai = fake_ai([
        {"text": "Let me check.", "tools": [("knowledge_search", {"query": "spa opening hours"}), ("delete_database", {"confirm": True})]},
        "The spa is open 09:00-21:00.",
    ])
    reply = client.post(f"/api/v1/agents/{agent['id']}/chat", headers=h, json={"message": "When is the spa open?"})
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert data["status"] == "COMPLETED" and data["output_text"] == "The spa is open 09:00-21:00." and data["iterations"] == 2
    assert data["input_tokens"] == 240 and data["output_tokens"] == 60 and data["cost_usd"] == pytest.approx(0.00216)
    # Only the allowlisted tool was offered to the model...
    assert [t["name"] for t in ai.calls[0]["tools"]] == ["knowledge_search"]
    # ...the real tool ran, and the tool the model invented was refused instead of executed.
    results = ai.calls[1]["messages"][-1]["content"]
    assert "09:00 to 21:00" in results[0]["content"] and results[0].get("is_error") is None
    assert results[1]["is_error"] is True and "not available" in results[1]["content"]
    # The assistant turn is replayed verbatim to the same model (provider-native blocks).
    assert ai.calls[1]["messages"][1]["content"][1]["type"] == "tool_use"
    # Conversation memory: a follow-up continues the same conversation.
    client.post(f"/api/v1/agents/{agent['id']}/chat", headers=h, json={"message": "And the pool?", "conversation_id": data["conversation_id"]})
    assert len(ai.calls[-1]["messages"]) == 5
    msgs = client.get(f"/api/v1/conversations/{data['conversation_id']}/messages", headers=h).json()["items"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant", "user", "assistant"]
    assert "native" not in json.dumps(msgs)


def test_high_impact_agent_tool_requires_human_approval(client, db, org, fake_ai, http_mock, worker):
    http_mock.add("GET", "https://api.telegram.org/", (200, {"ok": True, "result": {"username": "b"}}))
    http_mock.add("POST", "https://api.telegram.org/", (200, {"ok": True, "result": {"message_id": 1}}))
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    integration = client.post("/api/v1/integrations", headers=h, json={"provider_key": "telegram", "values": {"bot_token": "111222333:" + "C" * 35}}).json()
    agent = _agent(client, h)
    tool = client.post(f"/api/v1/agents/{agent['id']}/tools", headers=h, json={"name": "notify_guest", "tool_key": "send_message", "config": {"integration_id": integration["id"]}})
    assert tool.status_code == 201 and tool.json()["requires_approval"] is True  # default for high-impact tools
    bad = client.post(f"/api/v1/agents/{agent['id']}/tools", headers=h, json={"name": "shell", "tool_key": "run_shell", "config": {}})
    assert bad.status_code == 422  # only tools from the controlled registry exist

    fake_ai([{"tools": [("notify_guest", {"to": "4242", "text": "Your room is ready"})]}, "I have notified the guest."])
    first = client.post(f"/api/v1/agents/{agent['id']}/chat", headers=h, json={"message": "Tell guest 4242 their room is ready"}).json()
    assert first["status"] == "WAITING_APPROVAL" and first["awaiting_approval_id"]
    assert not [r for r in http_mock.requests if r.url.path.endswith("/sendMessage")]  # nothing sent before approval
    pending = client.get("/api/v1/approvals", headers=h).json()["items"]
    assert pending[0]["kind"] == "agent_tool" and pending[0]["payload"]["input"]["text"] == "Your room is ready"
    decided = client.post(f"/api/v1/approvals/{pending[0]['id']}/decide", headers=h, json={"approved": True, "note": "go"})
    assert decided.json()["status"] == "APPROVED"
    assert client.post(f"/api/v1/approvals/{pending[0]['id']}/decide", headers=h, json={"approved": False}).status_code == 409
    worker.drain()
    done = client.get(f"/api/v1/agent-executions/{first['id']}", headers=h).json()
    assert done["status"] == "COMPLETED" and done["output_text"] == "I have notified the guest."
    assert len([r for r in http_mock.requests if r.url.path.endswith("/sendMessage")]) == 1
    assert any(t.get("approved_by_human") for t in done["trace"] if t["type"] == "tool")

    # Rejection path: the tool does not run and the model is told why.
    ai2 = fake_ai([{"tools": [("notify_guest", {"to": "1", "text": "spam"})]}, "Understood, I will not send it."])
    second = client.post(f"/api/v1/agents/{agent['id']}/chat", headers=h, json={"message": "Spam the guest"}).json()
    approval = db.scalar(select(ApprovalRequest).where(ApprovalRequest.status == "PENDING"))
    client.post(f"/api/v1/approvals/{approval.id}/decide", headers=h, json={"approved": False, "note": "not appropriate"})
    worker.drain()
    assert client.get(f"/api/v1/agent-executions/{second['id']}", headers=h).json()["status"] == "COMPLETED"
    assert len([r for r in http_mock.requests if r.url.path.endswith("/sendMessage")]) == 1  # still only the approved one
    assert ai2.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_agent_http_tool_domain_allowlist_and_limits(client, db, org, fake_ai, http_mock):
    http_mock.add("GET", "https://api.partner.com/rates", (200, {"rate": 99}))
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    agent = _agent(client, h, limits={"max_executions_per_day": 2})
    client.post(f"/api/v1/agents/{agent['id']}/tools", headers=h, json={
        "name": "fetch", "tool_key": "http_request", "config": {"allowed_domains": ["api.partner.com"]}})
    ai = fake_ai([{"tools": [("fetch", {"method": "GET", "url": "https://api.partner.com/rates"}),
                             ("fetch", {"method": "GET", "url": "https://evil.example.org/steal"}),
                             ("fetch", {"method": "DELETE", "url": "https://api.partner.com/rates"})]}, "done"])
    client.post(f"/api/v1/agents/{agent['id']}/chat", headers=h, json={"message": "get rates"})
    r = ai.calls[1]["messages"][-1]["content"]
    assert '"rate": 99' in r[0]["content"] and r[1]["is_error"] and "allowlist" in r[1]["content"] and r[2]["is_error"]
    assert [str(q.url) for q in http_mock.requests] == ["https://api.partner.com/rates"]
    client.post(f"/api/v1/agents/{agent['id']}/chat", headers=h, json={"message": "again"})
    limited = client.post(f"/api/v1/agents/{agent['id']}/chat", headers=h, json={"message": "third"})
    assert limited.status_code == 409 and limited.json()["error"]["code"] == "agent_limit_reached"


def test_mcp_client_and_scoped_mcp_tool(client, db, org, fake_ai, http_mock):
    calls = []

    def mcp(request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        calls.append((msg["method"], request.headers.get("authorization"), msg.get("params")))
        if msg["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18"}}, headers={"Mcp-Session-Id": "s1"})
        if msg["method"] == "notifications/initialized":
            return httpx.Response(202)
        if msg["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": "lookup_booking", "description": "Find a booking", "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"}}}},
                {"name": "delete_booking", "description": "Delete", "inputSchema": {"type": "object", "properties": {}}}]}})
        body = 'event: message\ndata: ' + json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "Booking R-1: 2 nights"}]}}) + "\n\n"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    http_mock.add("POST", "https://mcp.partner.com/mcp", mcp)
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    server = client.post("/api/v1/integrations", headers=h, json={"provider_key": "mcp_server", "values": {"url": "https://mcp.partner.com/mcp", "api_key": "mcp-secret-token"}})
    assert server.json()["status"] == "connected"
    discovered = client.get(f"/api/v1/integrations/{server.json()['id']}/mcp-tools", headers=h).json()["items"]
    assert [t["name"] for t in discovered] == ["lookup_booking", "delete_booking"]
    agent = _agent(client, h)
    # Only lookup_booking is allowlisted; delete_booking exists on the server but is never exposed.
    added = client.post(f"/api/v1/agents/{agent['id']}/tools", headers=h, json={
        "name": "lookup_booking", "tool_key": "mcp_tool", "requires_approval": False,
        "config": {"integration_id": server.json()["id"], "tool_name": "lookup_booking", "input_schema": discovered[0]["input_schema"], "description": "Find a booking"}})
    assert added.status_code == 201
    ai = fake_ai([{"tools": [("lookup_booking", {"ref": "R-1"}), ("delete_booking", {})]}, "Found it."])
    client.post(f"/api/v1/agents/{agent['id']}/chat", headers=h, json={"message": "find R-1"})
    assert [t["name"] for t in ai.calls[0]["tools"]] == ["lookup_booking"]
    results = ai.calls[1]["messages"][-1]["content"]
    assert results[0]["content"] == "Booking R-1: 2 nights" and results[1]["is_error"] is True
    tool_calls = [c for c in calls if c[0] == "tools/call"]
    assert len(tool_calls) == 1 and tool_calls[0][2] == {"name": "lookup_booking", "arguments": {"ref": "R-1"}}
    assert tool_calls[0][1] == "Bearer mcp-secret-token"


def test_ai_nodes_structured_output_and_provider_fallback(db, org, worker, fake_ai, http_mock):
    from app.integrations.service import create_integration

    def scripted(kwargs):
        if "Classify" in kwargs.get("system", ""):
            assert kwargs["output_config"]["format"]["schema"]["properties"]["label"]["enum"] == ["complaint", "praise"]
            return json.dumps({"label": "complaint", "confidence": 0.93, "reason": "late delivery"})
        return json.dumps({"order_id": "A-77", "amount": 19.5})

    fake_ai([scripted])
    d = wf_def(
        [TRIG, {"id": "c", "type": "ai.classify", "config": {"text": "{{ trigger.msg }}", "categories": ["complaint", "praise"]}},
         {"id": "e", "type": "ai.extract", "config": {"text": "{{ trigger.msg }}", "fields": [{"name": "order_id"}, {"name": "amount", "type": "number"}]}},
         {"id": "dec", "type": "ai.decision", "config": {"question": "Refund?", "context": "{{ nodes.c.reason }}", "options": ["refund", "apologize"]}}],
        [("t", "c"), ("c", "e"), ("e", "dec")])
    wf = make_workflow(db, org, d)
    ex = run(db, worker, wf, {"msg": "Order A-77 for 19.50 arrived late"})
    # The decision node got an answer that is not one of its options -> invalid output is an error, not a guess.
    assert ex.status == "FAILED" and ex.error["code"] == "ai_invalid_output"
    assert ex.state["outputs"]["c"]["label"] == "complaint" and ex.state["outputs"]["e"]["data"] == {"order_id": "A-77", "amount": 19.5}

    # Fallback: OpenAI primary fails with a server error, the Anthropic fallback answers.
    from app.ai.service import complete

    http_mock.add("GET", "https://api.openai.com/v1/models", (200, {"data": [{"id": "some-model"}]}))
    http_mock.add("POST", "https://api.openai.com/v1/chat/completions", (500, {"error": {"message": "overloaded"}}))
    create_integration(db, org.id, "openai", "OpenAI", {"api_key": "sk-test-openai-key-000000000000"}, None)
    fake_ai(["from the fallback"])
    result = complete(db, org.id, provider="openai", model="some-model", messages=[{"role": "user", "text": "hi"}],
                      fallbacks=[{"provider": "anthropic", "model": "claude-sonnet-5-5"}])
    db.commit()
    assert result.text == "from the fallback" and result.provider == "anthropic"
    last = db.scalars(select(AIAgentExecution).order_by(AIAgentExecution.created_at.desc())).first()
    assert last.trace[0]["attempts"] == [{"provider": "openai", "model": "some-model", "error": "ai_provider_error"},
                                         {"provider": "anthropic", "model": "claude-sonnet-5-5", "ok": True}]


def test_openai_compatible_and_gemini_request_mapping(http_mock):
    from app.ai.providers import LLMRequest, get_provider

    seen = {}

    def openai(request):
        seen["openai"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "local-llm", "choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\": \"x\"}"}}]}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7}})

    def gemini(request):
        seen["gemini"] = json.loads(request.content)
        seen["gemini_key"] = request.headers.get("x-goog-api-key")
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "bonjour"}]}}],
                                         "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}})

    http_mock.add("POST", "https://llm.partner.com/v1/chat/completions", openai)
    http_mock.add("POST", "https://generativelanguage.googleapis.com/", gemini)
    tools = [{"name": "lookup", "description": "d", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "additionalProperties": False}}]
    req = LLMRequest(model="local-llm", system="sys", tools=tools, temperature=0.2, max_tokens=500, messages=[
        {"role": "user", "text": "hi"}, {"role": "assistant", "text": "", "tool_calls": [{"id": "c0", "name": "lookup", "input": {"q": "a"}}]},
        {"role": "tool", "tool_results": [{"tool_call_id": "c0", "name": "lookup", "content": "res"}]}])
    r = get_provider("openai_compatible").complete(req, {"api_key": "k"}, {"base_url": "https://llm.partner.com/v1"})
    assert r.tool_calls == [{"id": "c1", "name": "lookup", "input": {"q": "x"}}] and (r.input_tokens, r.output_tokens) == (11, 7)
    body = seen["openai"]
    assert body["messages"][0] == {"role": "system", "content": "sys"} and body["messages"][-1] == {"role": "tool", "tool_call_id": "c0", "content": "res"}
    assert body["max_tokens"] == 500 and body["temperature"] == 0.2 and body["tools"][0]["function"]["name"] == "lookup"
    g = get_provider("gemini").complete(LLMRequest(model="gemini-test", system="sys", tools=tools, messages=req.messages), {"api_key": "gk"}, {})
    assert g.text == "bonjour" and seen["gemini_key"] == "gk"
    assert seen["gemini"]["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert "additionalProperties" not in json.dumps(seen["gemini"]["tools"])
    assert seen["gemini"]["contents"][1]["parts"][0]["functionCall"] == {"name": "lookup", "args": {"q": "a"}}


# ----------------------------------------------------------------------------- templates & packages


def test_template_installs_are_isolated_per_organization(client, db, org, other_org):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    eng = login(client, "eng@weakless.pro")
    listing = client.get("/api/v1/templates?limit=200", headers=eng).json()
    assert listing["total"] >= 40 and {"Hospitality", "Restaurants", "Beauty & Salons", "Real Estate", "E-commerce"} <= {t["category"] for t in listing["items"]}
    tid = next(t["id"] for t in listing["items"] if t["key"] == "custom-webhook-relay")
    a = client.post(f"/api/v1/templates/{tid}/install", headers=hdr(eng, org.id), json={"config": {"target_url": "https://a.example.com/in"}}).json()
    b = client.post(f"/api/v1/templates/{tid}/install", headers=hdr(eng, other_org.id), json={"config": {"target_url": "https://b.example.com/in"}}).json()
    assert a["workflow_id"] != b["workflow_id"]
    wa, wb = db.get(Workflow, uuid.UUID(a["workflow_id"])), db.get(Workflow, uuid.UUID(b["workflow_id"]))
    assert (wa.organization_id, wb.organization_id) == (org.id, other_org.id)
    assert wa.variables == {"target_url": "https://a.example.com/in"} and wb.variables == {"target_url": "https://b.example.com/in"}
    # Editing one tenant's copy changes neither the template nor the other tenant's copy.
    ha = hdr(eng, org.id)
    d = client.get(f"/api/v1/workflows/{a['workflow_id']}", headers=ha).json()["definition"]
    d["nodes"][1]["config"]["fields"]["source"] = "tenant-a-custom"
    client.put(f"/api/v1/workflows/{a['workflow_id']}/draft", headers=ha, json={"definition": d})
    other = client.get(f"/api/v1/workflows/{b['workflow_id']}", headers=hdr(eng, other_org.id)).json()["definition"]
    assert other["nodes"][1]["config"]["fields"]["source"] == "wae"
    assert client.get(f"/api/v1/templates/{tid}", headers=eng).json()["definition"]["nodes"][1]["config"]["fields"]["source"] == "wae"
    # Reconfigure; variables apply without touching the other tenant.
    client.put(f"/api/v1/installed-templates/{a['id']}/config", headers=ha, json={"config": {"target_url": "https://a2.example.com/in"}})
    db.expire_all()
    assert db.get(Workflow, uuid.UUID(a["workflow_id"])).variables["target_url"] == "https://a2.example.com/in"
    assert db.get(Workflow, uuid.UUID(b["workflow_id"])).variables["target_url"] == "https://b.example.com/in"
    assert client.get("/api/v1/installed-templates", headers=ha).json()["total"] == 1


def test_template_update_flow_and_industry_package_install(client, db, org):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    eng = login(client, "eng@weakless.pro")
    h = hdr(eng, org.id)
    packages = client.get("/api/v1/industry-packages", headers=eng).json()["items"]
    assert {p["key"] for p in packages} == {"hospitality", "restaurant", "beauty-salon", "real-estate", "ecommerce", "digital-agency", "ai-marketing-team"}
    salon = next(p for p in packages if p["key"] == "beauty-salon")
    detail = client.get(f"/api/v1/industry-packages/{salon['id']}", headers=eng).json()
    assert len(detail["templates"]) == 6
    result = client.post(f"/api/v1/industry-packages/{salon['id']}/install", headers=h, json={"config": {"business_name": "Glow Studio"}})
    assert result.status_code == 201 and len(result.json()["installed"]) == 6
    # Shared configuration reached every template that defines the key; required integrations are reported as missing.
    with_name = [i for i in result.json()["installed"] if "business_name" in i["config_schema"]["properties"]]
    assert with_name and all(i["config"]["business_name"] == "Glow Studio" for i in with_name)
    assert all(i["missing_config"] for i in result.json()["installed"])
    assert db.scalars(select(Workflow).where(Workflow.organization_id == org.id)).all().__len__() == 6
    again = client.post(f"/api/v1/industry-packages/{salon['id']}/install", headers=h, json={})
    assert again.json()["installed"] == []  # idempotent: nothing is installed twice

    # A new template version becomes an available update that only replaces the draft.
    inst = result.json()["installed"][0]
    tmpl = client.get(f"/api/v1/templates/{inst['template_id']}", headers=eng).json()
    new_def = tmpl["definition"]
    new_def["nodes"][0]["name"] = "Renamed trigger"
    patched = client.patch(f"/api/v1/templates/{inst['template_id']}", headers=eng, json={"definition": new_def, "changelog": "rename"})
    assert patched.status_code == 200 and patched.json()["version"] == 2
    row = next(i for i in client.get("/api/v1/installed-templates", headers=h).json()["items"] if i["id"] == inst["id"])
    assert row["update_available"] is True and (row["version"], row["latest_version"]) == (1, 2)
    updated = client.post(f"/api/v1/installed-templates/{inst['id']}/update", headers=h).json()
    assert updated["updated"] is True and updated["installed"]["update_available"] is False
    assert client.get(f"/api/v1/workflows/{inst['workflow_id']}", headers=h).json()["definition"]["nodes"][0]["name"] == "Renamed trigger"
    assert db.scalar(select(InstalledTemplate).where(InstalledTemplate.id == uuid.UUID(inst["id"]))).status == "installed"


# ----------------------------------------------------------------------------- billing & usage


def test_quota_enforcement_usage_alerts_and_plan_changes(client, db, org, worker):
    make_user(db, "admin@weakless.pro", platform_role="SUPER_ADMIN")
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    admin = login(client, "admin@weakless.pro")
    h = hdr(admin, org.id)
    plans = client.get("/api/v1/billing/plans", headers=admin).json()["items"]
    assert {p["key"] for p in plans} >= {"internal", "starter", "growth", "scale"}
    # Administrators can edit plan configuration.
    starter = next(p for p in plans if p["key"] == "starter")
    edited = client.put(f"/api/v1/billing/plans/{starter['id']}", headers=admin, json={
        **{k: starter[k] for k in ("key", "name", "description", "price_monthly_cents", "currency", "features", "is_active", "is_public", "sort_order")},
        "limits": {"executions_per_month": 5, "ai_tokens_per_month": 1000, "integrations": 1, "workflows": 3, "concurrency": 1}})
    assert edited.status_code == 200 and edited.json()["limits"]["executions_per_month"] == 5
    assert client.post("/api/v1/billing/change-plan", headers=h, json={"plan": "starter"}).json()["result"] == "applied"

    wf = make_workflow(db, org, wf_def([TRIG], []))
    for _ in range(5):
        assert run(db, worker, wf).status == "COMPLETED"
    blocked = client.post(f"/api/v1/workflows/{wf.id}/execute", headers=h)
    assert blocked.status_code == 402 and blocked.json()["error"]["code"] == "quota_exceeded"
    sub = client.get("/api/v1/billing/subscription", headers=h).json()
    assert sub["plan"]["key"] == "starter" and sub["quotas"]["executions_per_month"] == {"used": 5.0, "limit": 5, "percent": 100.0, "exceeded": True}
    alerts = db.scalars(select(Notification).where(Notification.event_type == "usage.threshold")).all()
    assert {a.data["threshold"] for a in alerts} == {80, 100}
    assert len({(a.user_id, a.data["threshold"]) for a in alerts}) == len(alerts)  # each threshold notified once per user
    # Test runs do not consume quota.
    assert client.post(f"/api/v1/workflows/{wf.id}/test", headers=h).status_code == 202
    # Other limits.
    for i in range(2):
        client.post("/api/v1/workflows", headers=h, json={"name": f"w{i}"})
    assert client.post("/api/v1/workflows", headers=h, json={"name": "one too many"}).status_code == 402

    # A client asking for an upgrade is never charged or silently upgraded without a payment provider.
    owner = login(client, "owner@acme.co")
    asked = client.post("/api/v1/billing/change-plan", headers=hdr(owner, org.id), json={"plan": "growth"}).json()
    assert asked == {"result": "requested", "checkout_url": None}
    db.expire_all()
    assert client.get("/api/v1/billing/subscription", headers=h).json()["plan"]["key"] == "starter"
    assert client.post("/api/v1/billing/change-plan", headers=hdr(owner, org.id), json={"plan": "internal"}).status_code == 404  # non-public plan
    # Staff upgrade applies immediately; a downgrade is scheduled for the period end.
    assert client.post("/api/v1/billing/change-plan", headers=h, json={"plan": "growth"}).json()["result"] == "applied"
    assert client.post(f"/api/v1/workflows/{wf.id}/execute", headers=h).status_code == 202
    assert client.post("/api/v1/billing/change-plan", headers=h, json={"plan": "starter"}).json()["result"] == "scheduled"
    sub = client.get("/api/v1/billing/subscription", headers=h).json()
    assert sub["plan"]["key"] == "growth" and sub["subscription"]["pending_plan"] == "Starter"
    from app.services.billing import roll_periods

    s = db.scalar(select(Subscription).where(Subscription.organization_id == org.id))
    s.current_period_end = utcnow() - timedelta(minutes=1)
    db.commit()
    assert roll_periods(db) == 1
    assert client.get("/api/v1/billing/subscription", headers=h).json()["plan"]["key"] == "starter"
    types = [e["type"] for e in client.get("/api/v1/billing/events", headers=h).json()["items"]]
    assert "subscription.change_requested" in types and "subscription.downgrade_scheduled" in types
    history = client.get("/api/v1/usage/history", headers=h).json()["items"]
    assert history[0]["executions"] == 6.0
    assert db.scalar(select(UsageRecord).where(UsageRecord.metric == "executions")) is not None


def test_stripe_webhook_requires_configuration_and_valid_signature(client, monkeypatch):
    import time

    from app.config import get_settings
    from app.core.security import hmac_sha256_hex

    body = json.dumps({"id": "evt_1", "type": "invoice.paid", "data": {"object": {"id": "in_1"}}}).encode()
    assert client.post("/api/v1/billing/webhooks/stripe", content=body).status_code == 503  # not configured: nothing is processed
    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", "whsec_unit")
    assert client.post("/api/v1/billing/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=bad"}).status_code == 400
    ts = str(int(time.time()))
    sig = hmac_sha256_hex("whsec_unit", ts.encode() + b"." + body)
    ok = client.post("/api/v1/billing/webhooks/stripe", content=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
    assert ok.status_code == 200 and ok.json()["status"] == "unmatched"
    dup = client.post("/api/v1/billing/webhooks/stripe", content=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
    assert dup.json()["status"] == "duplicate"


# ----------------------------------------------------------------------------- notifications & platform admin


def test_notification_channels_preferences_and_delivery_log(client, db, org, worker, http_mock):
    received = []

    def channel(request):
        received.append((request.headers.get("x-wae-signature"), json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    http_mock.add("POST", "https://hooks.partner.com/wae", channel)
    http_mock.add("POST", "https://hooks.slack.com/", (500, {}))
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    assert client.post("/api/v1/notification-channels", headers=h, json={"kind": "webhook", "name": "bad", "values": {"url": "http://insecure.example.com"}}).status_code == 422
    made = client.post("/api/v1/notification-channels", headers=h, json={
        "kind": "webhook", "name": "Ops webhook", "values": {"url": "https://hooks.partner.com/wae", "signing_secret": "chan-secret"}, "events": ["workflow.failed"]})
    assert made.status_code == 201 and "hooks.partner.com" not in made.text and made.json()["has_secret"] is True
    client.post("/api/v1/notification-channels", headers=h, json={"kind": "slack", "name": "Slack", "values": {"url": "https://hooks.slack.com/services/T/B/x"}})
    assert client.post(f"/api/v1/notification-channels/{made.json()['id']}/test", headers=h).json()["status"] == "sent"
    assert received[0][0].startswith("sha256=")
    # A failed workflow reaches the subscribed channel; a broken channel is logged, not fatal.
    http_mock.add("GET", "https://api.example.com/down", (500, {}))
    ex = run(db, worker, make_workflow(db, org, wf_def([TRIG, {"id": "h", "type": "action.http_request", "config": {"url": "https://api.example.com/down"}}], [("t", "h")])))
    assert ex.status == "FAILED"
    assert received[-1][1]["event"] == "workflow.failed"
    log = client.get("/api/v1/notification-deliveries", headers=h).json()["items"]
    assert {(d["channel_kind"], d["status"]) for d in log} >= {("webhook", "sent"), ("slack", "failed")}
    # Preferences: muting an event stops in-app notifications for that user.
    prefs = client.get("/api/v1/notifications/preferences", headers=h).json()["items"]
    assert next(p for p in prefs if p["event_type"] == "workflow.failed")["in_app"] is True
    client.put("/api/v1/notifications/preferences", headers=h, json={"items": [{"event_type": "workflow.failed", "in_app": False, "email": False}]})
    before = client.get("/api/v1/notifications", headers=h).json()["total"]
    run(db, worker, db.get(Workflow, ex.workflow_id))
    assert client.get("/api/v1/notifications", headers=h).json()["total"] == before


def test_platform_admin_surfaces(client, db, org):
    make_user(db, "root@weakless.pro", platform_role="SUPER_ADMIN")
    root = login(client, "root@weakless.pro")
    created = client.post("/api/v1/users", headers=root, json={"email": "sales@weakless.pro", "full_name": "Sales", "password": ADMIN_PASSWORD, "platform_role": "SALES"})
    assert created.status_code == 201
    weak = client.post("/api/v1/users", headers=root, json={"email": "w@weakless.pro", "full_name": "W", "password": "short", "platform_role": "SALES"})
    assert weak.status_code == 422
    settings = client.get("/api/v1/settings", headers=root).json()
    assert settings["environment"]["stripe_configured"] is False and "secret" not in json.dumps(settings).lower().replace("client_secret", "")
    assert client.put("/api/v1/settings/ai.model_pricing", headers=root, json={"value": {"my-model": {"input": 1, "output": 2}}}).status_code == 200
    assert client.put("/api/v1/settings/ai.model_pricing", headers=root, json={"value": {"m": {"input": -1, "output": 2}}}).status_code == 422
    assert client.put("/api/v1/settings/not.a.setting", headers=root, json={"value": 1}).status_code == 404
    health = client.get("/api/v1/analytics/system", headers=root).json()
    assert health["database"] == "ok" and health["queue_status"] == "ok"
    roles = client.get("/api/v1/roles", headers=root).json()
    assert len(roles["roles"]) == 9
    # Sales can create organizations but not build workflows.
    sales = login(client, "sales@weakless.pro")
    assert client.post("/api/v1/organizations", headers=sales, json={"name": "Prospect Ltd"}).status_code == 201
    assert client.post("/api/v1/workflows", headers=hdr(sales, org.id), json={"name": "x"}).status_code == 403
    assert client.get("/api/openapi.json").json()["info"]["title"] == "Weakless Automation Engine API"
    paths = client.get("/api/openapi.json").json()["paths"]
    for p in ["/api/v1/organizations", "/api/v1/workspaces", "/api/v1/workflows", "/api/v1/workflows/{workflow_id}/versions",
              "/api/v1/workflows/{workflow_id}/execute", "/api/v1/executions", "/api/v1/agents", "/api/v1/integrations", "/api/v1/webhooks",
              "/api/v1/templates", "/api/v1/industry-packages", "/api/v1/analytics/overview", "/api/v1/usage", "/api/v1/billing/subscription",
              "/api/v1/notifications", "/api/v1/audit-logs"]:
        assert p in paths, p
