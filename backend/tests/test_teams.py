"""AI teams: installing the AI Marketing Team, delegation between agents, and the content calendar."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.ai.agents import run_agent
from app.db import utcnow
from app.models import (
    AIAgent, AIAgentExecution, AIAgentTool, ContentItem, IndustryPackage, InstalledTemplate, KnowledgeDocument, Permission, Role, RolePermission,
    WebhookEvent, Workflow, WorkflowExecution,
)
from app.services import workflows as wfs
from app.services.content import release_due_content
from tests.conftest import login, make_user, make_workflow, wf_def
from tests.test_platform import fake_ai, hdr  # noqa: F401 - fixture reused

BRIEF = {"business_name": "Sunny Yoga", "business_description": "A yoga studio in Varna with beginner and prenatal classes.",
         "target_audience": "Busy professionals and expecting mothers in Varna.", "channels": "instagram, linkedin, newsletter"}


def _install(client, h, **config):
    pkg = next(p for p in client.get("/api/v1/industry-packages", headers=h).json()["items"] if p["key"] == "ai-marketing-team")
    return client.post(f"/api/v1/industry-packages/{pkg['id']}/install", headers=h, json={"config": {**BRIEF, **config}})


def _team(db, org) -> dict[str, AIAgent]:
    return {a.permissions["team_member"]: a for a in db.scalars(select(AIAgent).where(AIAgent.organization_id == org.id, AIAgent.team_key == "marketing"))}


def test_installing_the_package_creates_the_team_playbook_and_wired_workflows(client, db, org):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    detail = client.get(f"/api/v1/industry-packages/{db.scalar(select(IndustryPackage.id).where(IndustryPackage.key == 'ai-marketing-team'))}", headers=h).json()
    assert detail["team"]["name"] == "AI Marketing Team" and len(detail["team"]["members"]) == 10
    assert "business_name" in detail["team"]["config_schema"]["required"]

    missing = _install(client, h, business_name="")
    assert missing.status_code == 422 and "Business name" in missing.text
    r = _install(client, h)
    assert r.status_code == 201, r.text
    assert len(r.json()["installed"]) == 5 and len(r.json()["team"]["agents"]) == 10

    team = _team(db, org)
    lead = team["director"]
    assert lead.team_lead and lead.team_module is None and all(not a.team_lead for k, a in team.items() if k != "director")
    assert {a.team_module for a in team.values() if not a.team_lead} == {"Research & Content", "Email & Nurture", "Search & Website", "Leads & Booking", "Analytics Loop"}
    delegates = db.scalars(select(AIAgentTool).where(AIAgentTool.agent_id == lead.id, AIAgentTool.tool_key == "delegate_to_agent")).all()
    assert len(delegates) == 9 and {d.config["agent_id"] for d in delegates} == {str(a.id) for k, a in team.items() if k != "director"}
    # Optional integrations that were not chosen leave the member without that tool instead of a broken one.
    assert not db.scalars(select(AIAgentTool).where(AIAgentTool.agent_id == team["call_booker"].id, AIAgentTool.tool_key == "create_calendar_event")).all()
    writer_tools = {t.tool_key: t for t in db.scalars(select(AIAgentTool).where(AIAgentTool.agent_id == team["post_writer"].id))}
    assert writer_tools["content_calendar_add"].config["channels"] and "newsletter" not in writer_tools["content_calendar_add"].config["channels"]
    assert "Sunny Yoga" in team["post_writer"].system_instructions
    playbook = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.organization_id == org.id))
    assert "prenatal" in playbook.content
    plan = db.scalar(select(Workflow).where(Workflow.organization_id == org.id, Workflow.name == "Weekly Content Plan"))
    assert plan.variables["director_agent"] == str(lead.id)

    again = _install(client, h)  # reinstalling reuses the team instead of duplicating it
    assert again.status_code == 201 and again.json()["installed"] == []
    assert db.scalar(select(func.count()).select_from(AIAgent).where(AIAgent.organization_id == org.id)) == 10

    teams = client.get("/api/v1/teams", headers=h).json()["items"]
    assert teams[0]["name"] == "AI Marketing Team" and teams[0]["lead"]["name"] == "Marketing Director"
    assert sum(len(m["agents"]) for m in teams[0]["modules"]) == 9


def test_the_director_delegates_and_specialists_propose_content_for_approval(client, db, org, fake_ai):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    assert _install(client, h).status_code == 201
    team = _team(db, org)
    when = (utcnow() + timedelta(days=2)).replace(microsecond=0).isoformat()
    ai = fake_ai([
        {"text": "I'll brief the Post Writer.", "tools": [("delegate_to_post_writer", {"task": "Write one Instagram post about beginner classes",
                                                                                         "context": "Theme: first class nerves"})]},
        {"tools": [("content_calendar_add", {"title": "Your first class", "channel": "instagram", "body": "Nervous about your first class? ...",
                                             "media_notes": "Bright studio photo", "hashtags": ["#yoga", "varna"], "scheduled_for": when})]},
        "Added one Instagram post for approval.",
        "This week: one Instagram post on first-class nerves is waiting for your approval.",
    ])
    lead_run = run_agent(db, team["director"], "Plan this week")
    db.commit()
    assert lead_run.status == "COMPLETED" and "waiting for your approval" in lead_run.output_text
    sub = db.scalar(select(AIAgentExecution).where(AIAgentExecution.parent_execution_id == lead_run.id))
    assert sub.agent_id == team["post_writer"].id and sub.status == "COMPLETED"
    assert "first class nerves" in str(ai.calls[1]["messages"][0])  # the member got the task and the lead's context
    assert len(ai.calls[1]["messages"]) == 1  # ...in a fresh conversation, not the lead's history
    tool_names = {t["name"] for t in ai.calls[0]["tools"]}
    assert "delegate_to_post_writer" in tool_names and "content_calendar_add" not in tool_names  # the lead plans, specialists write
    item = db.scalar(select(ContentItem))
    assert item.status == "awaiting_approval" and item.created_by_agent_id == team["post_writer"].id and item.hashtags == ["yoga", "varna"]
    assert client.get("/api/v1/content", headers=h).json()["items"][0]["title"] == "Your first class"
    kpis = client.get("/api/v1/teams", headers=h).json()["items"][0]["kpis"]
    assert kpis["delegations_7d"] == 1 and kpis["content_awaiting_approval"] == 1


def test_delegation_cannot_loop_and_writers_are_limited_to_their_channels(db, org, fake_ai):
    a = AIAgent(organization_id=org.id, name="Alpha", provider="anthropic", model="claude-opus-5-5")
    b = AIAgent(organization_id=org.id, name="Beta", provider="anthropic", model="claude-opus-5-5")
    db.add_all([a, b])
    db.flush()
    db.add_all([AIAgentTool(organization_id=org.id, agent_id=a.id, name="ask_beta", tool_key="delegate_to_agent", config={"agent_id": str(b.id)}),
                AIAgentTool(organization_id=org.id, agent_id=b.id, name="ask_alpha", tool_key="delegate_to_agent", config={"agent_id": str(a.id)}),
                AIAgentTool(organization_id=org.id, agent_id=b.id, name="plan", tool_key="content_calendar_add", config={"channels": ["blog"]})])
    db.commit()
    ai = fake_ai([
        {"tools": [("ask_beta", {"task": "Help"})]},
        {"tools": [("ask_alpha", {"task": "Help me back"}), ("plan", {"title": "T", "channel": "tiktok", "body": "x"})]},
        "Done what I could.",
        "Finished.",
    ])
    run = run_agent(db, a, "Start")
    db.commit()
    assert run.status == "COMPLETED"
    results = ai.calls[2]["messages"][-1]["content"]
    texts = " ".join(str(r) for r in results)
    assert "already working on this task" in texts and "may only plan content for: blog" in texts
    assert db.scalar(select(func.count()).select_from(ContentItem)) == 0


def test_approved_content_is_handed_to_the_publishing_workflow_and_reported_back(client, db, org, worker):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    make_user(db, "viewer@client.example", org=org, role="VIEWER")
    make_user(db, "member@client.example", org=org, role="CLIENT_MEMBER")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    publish = make_workflow(db, org, wf_def(
        [{"id": "due", "type": "trigger.app_event", "config": {"event_type": "content.due"}},
         {"id": "done", "type": "action.update_content", "config": {"item_id": "{{ trigger.data.id }}", "status": "published",
                                                                    "published_url": "https://example.org/p/{{ trigger.data.id }}"}}],
        [("due", "done")]), name="Publisher")
    later = (utcnow() + timedelta(hours=3)).isoformat()
    created = client.post("/api/v1/content", headers=h, json={"title": "Prenatal class launch", "channel": "linkedin", "body": "We are opening...",
                                                             "scheduled_for": later, "status": "awaiting_approval"})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]

    viewer = hdr(login(client, "viewer@client.example"), org.id)
    member = hdr(login(client, "member@client.example"), org.id)
    assert client.get("/api/v1/content", headers=viewer).status_code == 200
    assert client.post(f"/api/v1/content/{cid}/approve", headers=viewer, json={}).status_code == 403
    assert client.post(f"/api/v1/content/{cid}/approve", headers=member, json={}).status_code == 403  # members draft, admins approve
    assert client.patch(f"/api/v1/content/{cid}", headers=member, json={"body": "We are opening a prenatal class!"}).status_code == 200

    approved = client.post(f"/api/v1/content/{cid}/approve", headers=h, json={"note": "Looks good"})
    assert approved.json()["status"] == "approved"
    assert client.patch(f"/api/v1/content/{cid}", headers=h, json={"body": "changed"}).status_code == 409  # approved text is frozen
    assert release_due_content(db) == 0  # not due yet
    item = db.get(ContentItem, __import__("uuid").UUID(cid))
    item.scheduled_for = utcnow() - timedelta(minutes=1)
    db.commit()
    assert release_due_content(db) == 1 and release_due_content(db) == 0  # released once
    worker.drain()
    run = db.scalar(select(WorkflowExecution).where(WorkflowExecution.workflow_id == publish.id))
    assert run.status == "COMPLETED"
    final = client.get(f"/api/v1/content/{cid}", headers=h).json()
    assert final["status"] == "published" and final["published_url"].endswith(cid)
    assert db.scalar(select(WebhookEvent).where(WebhookEvent.event_type == "content.due")).status == "processed"
    assert client.delete(f"/api/v1/content/{cid}", headers=h).status_code == 409  # published content stays as a record
    assert client.post("/api/v1/events", headers=h, json={"event_type": "content.due", "data": {}}).status_code == 422  # reserved


def test_new_permissions_reach_existing_roles_without_undoing_admin_changes(db):
    from app.rbac import seed_rbac

    viewer = db.scalar(select(Role).where(Role.key == "VIEWER"))
    perm = {p.key: p for p in db.scalars(select(Permission))}
    # An administrator removed a long-standing grant, and this deployment predates content.read.
    db.query(RolePermission).filter(RolePermission.role_id == viewer.id, RolePermission.permission_id == perm["analytics.read"].id).delete()
    db.query(RolePermission).filter(RolePermission.permission_id == perm["content.read"].id).delete()
    db.delete(perm["content.read"])
    db.commit()
    seed_rbac(db)
    db.commit()
    keys = set(db.scalars(select(Permission.key).join(RolePermission, RolePermission.permission_id == Permission.id).where(RolePermission.role_id == viewer.id)))
    assert "content.read" in keys and "analytics.read" not in keys
