"""AI teams: a lead agent that delegates to specialists, installed together with a package.

A team is data (like templates): members with a module, instructions and tools. Installing it
creates, for one organization:

* a knowledge base (the team's playbook) seeded from the install form, which every member can search
  and the analyst adds weekly learnings to;
* one agent per member, the lead with a ``delegate_to_<member>`` tool per specialist;
* workflow variables ``<member>_agent`` (agent ids) so the package's workflows can run the members.

What a member can do is limited to the tools listed here: writers propose content to the calendar
(a person approves before anything is published), the booker's calendar tool and the lead tools ask
for human approval, and nobody can post to a social network directly.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.models import AIAgent, AIAgentTool, Integration, KnowledgeBase

_SOCIAL = ["linkedin", "instagram", "facebook", "tiktok", "youtube", "x"]

_BASE = ("You are part of the AI marketing team of {business}. Look up the playbook (knowledge_search) before you work: it holds "
         "the business description, audience, brand voice, offers and what the team learned in past weeks. Write in the brand voice. "
         "Never invent facts, prices, results or testimonials: if something is not in the playbook, say what is missing.")

MARKETING: dict[str, Any] = {
    "key": "marketing",
    "name": "AI Marketing Team",
    "playbook": "Marketing playbook",
    "config_schema": {
        "type": "object",
        "required": ["business_name", "business_description", "target_audience"],
        "properties": {
            "business_name": {"type": "string", "title": "Business name"},
            "business_description": {"type": "string", "title": "What the business sells and what makes it different", "x-multiline": True},
            "target_audience": {"type": "string", "title": "Who the customers are", "x-multiline": True},
            "brand_voice": {"type": "string", "title": "Brand voice", "default": "Friendly, clear and confident. No jargon, no hype."},
            "offers": {"type": "string", "title": "Current offers and calls to action", "x-multiline": True, "default": ""},
            "channels": {"type": "string", "title": "Channels to plan for (comma separated)", "default": "linkedin, instagram, newsletter, blog"},
            "timezone": {"type": "string", "title": "Timezone for the weekly rhythm", "default": "UTC"},
            "notify_email": {"type": "string", "title": "Email address that receives weekly reports", "default": ""},
            "calendar_integration": {"type": "string", "title": "Calendar for booking calls (optional)",
                                     "x-integration": ["google_calendar", "outlook", "microsoft_365"], "default": ""},
            "crm_integration": {"type": "string", "title": "CRM for new leads (optional)", "x-integration": ["hubspot", "pipedrive", "salesforce"], "default": ""},
            "email_integration": {"type": "string", "title": "Email account for reports and lead follow-up (optional)",
                                  "x-integration": ["gmail", "outlook", "microsoft_365", "smtp"], "default": ""},
        },
    },
    "lead": {
        "key": "director", "name": "Marketing Director", "module": None, "agent_type": "marketing",
        "description": "Plans the week, briefs the specialists and combines their work into one plan.",
        "instructions": _BASE + "\n\nYou are the Marketing Director. Each week you decide the themes, brief the right specialists, check their "
                        "work against the playbook and the latest learnings, and make sure the calendar has a balanced plan across the "
                        "channels: {channels}. Finish with a short summary of what was planned and what needs a person's decision.",
        "tools": [("content_calendar_list", {})],
    },
    "members": [
        {"key": "trend_spotter", "name": "Trend Spotter", "module": "Research & Content", "agent_type": "research",
         "description": "Finds themes worth posting about this week.",
         "instructions": "You find timely themes for {business}: seasonal moments, recurring customer questions, and topics the playbook "
                         "shows performed well. Return 3-5 themes, each with why it matters to the audience now. You have no live web "
                         "access unless an engineer adds a research tool; say so when freshness matters.",
         "tools": [("content_calendar_list", {})]},
        {"key": "audience_researcher", "name": "Audience Researcher", "module": "Research & Content", "agent_type": "research",
         "description": "Turns a theme into the audience's real questions and objections.",
         "instructions": "For a given theme, list the audience's questions, pains and objections in their own words, and the angle that "
                         "would make {business} genuinely useful to them.", "tools": []},
        {"key": "keyword_finder", "name": "Keyword Finder", "module": "Research & Content", "agent_type": "research",
         "description": "Suggests search phrases and hashtags for a theme.",
         "instructions": "Suggest search phrases people would type and relevant hashtags for a theme. Mark which are broad and which are "
                         "specific. You estimate from experience; say clearly that you have no search-volume data.", "tools": []},
        {"key": "post_writer", "name": "Post Writer", "module": "Research & Content", "agent_type": "marketing",
         "description": "Writes social posts and puts them in the calendar for approval.",
         "instructions": "Write ready-to-publish social posts for {business}. Fit each platform (LinkedIn: insight and story; Instagram: "
                         "visual hook and caption; TikTok/YouTube: a short script with a hook in the first two seconds). Add a media brief "
                         "in media_notes. Put every post in the content calendar with a sensible publish time; a person approves it.",
         "tools": [("content_calendar_add", {"channels": _SOCIAL}), ("content_calendar_list", {})]},
        {"key": "email_writer", "name": "Email Writer", "module": "Email & Nurture", "agent_type": "marketing",
         "description": "Writes the newsletter and nurture emails.",
         "instructions": "Write the newsletter and nurture emails for {business}: one idea per email, a clear subject line, a single call "
                         "to action from the playbook offers. Put each email in the content calendar (channel newsletter or email) for approval.",
         "tools": [("content_calendar_add", {"channels": ["newsletter", "email"]}), ("content_calendar_list", {})]},
        {"key": "seo_writer", "name": "SEO Writer", "module": "Search & Website", "agent_type": "marketing",
         "description": "Drafts blog articles around the week's keywords.",
         "instructions": "Draft helpful blog articles for {business} around a theme and its keywords: a clear structure with headings, "
                         "answers to the audience's questions, no keyword stuffing. Put each article in the calendar (channel blog) for approval.",
         "tools": [("content_calendar_add", {"channels": ["blog"]}), ("content_calendar_list", {})]},
        {"key": "lead_scorer", "name": "Lead Scorer", "module": "Leads & Booking", "agent_type": "sales",
         "description": "Scores new enquiries and drafts the first reply.",
         "instructions": "Score a new enquiry for {business} as hot, warm or cold against the audience and offers in the playbook, explain "
                         "why in one sentence, and draft a short, personal first reply. Start your answer with HOT, WARM or COLD.", "tools": []},
        {"key": "call_booker", "name": "Call Booker", "module": "Leads & Booking", "agent_type": "booking",
         "description": "Books discovery calls in the calendar (with approval).",
         "instructions": "Book discovery calls for {business} when a lead asks for one. Confirm the time zone, keep calls at 30 minutes and "
                         "include the lead's question in the event description.",
         "tools": [("create_calendar_event", {"integration_id": "{calendar_integration}"})], "needs": "calendar_integration"},
        {"key": "analyst", "name": "Performance Analyst", "module": "Analytics Loop", "agent_type": "research",
         "description": "Reviews the week and records what to repeat or drop.",
         "instructions": "Review the last week of the content calendar for {business}: what was planned, approved, rejected and published, "
                         "and any results a person added. Write 3-5 concrete learnings (what to repeat, what to drop, what to test next) and "
                         "save them to the playbook so next week's plan uses them.",
         "tools": [("content_calendar_list", {}), ("knowledge_note", {"knowledge_base_id": "{playbook}"})]},
    ],
}

TEAMS = {"marketing": MARKETING}


def team_members(db: Session, organization_id: uuid.UUID, team_key: str) -> dict[str, AIAgent]:
    agents = db.scalars(select(AIAgent).where(AIAgent.organization_id == organization_id, AIAgent.team_key == team_key, AIAgent.deleted_at.is_(None)))
    return {(a.permissions or {}).get("team_member", ""): a for a in agents}


def _playbook_text(team: dict, cfg: dict) -> str:
    return (f"# {cfg['business_name']}\n\n## What we sell\n{cfg['business_description']}\n\n## Who our customers are\n{cfg['target_audience']}\n\n"
            f"## Brand voice\n{cfg.get('brand_voice') or ''}\n\n## Offers and calls to action\n{cfg.get('offers') or 'None recorded yet.'}\n\n"
            f"## Channels\n{cfg.get('channels') or ''}\n")


def install_team(db: Session, organization_id: uuid.UUID, team_key: str, config: dict, *, provider: str, model: str) -> dict[str, str]:
    """Create (or reuse) the team for this organization. Returns workflow variables: ``<member>_agent``
    agent ids and ``playbook_kb``. Installing twice reuses the existing members."""
    from app.ai.knowledge import add_document
    from app.templates.service import normalize_config

    team = TEAMS.get(team_key)
    if not team:
        raise ValidationFailed(f"Unknown team '{team_key}'")
    cfg = normalize_config(team["config_schema"], config, require=True)
    for field in ("calendar_integration", "crm_integration", "email_integration"):
        if cfg.get(field):
            try:
                integ = db.get(Integration, uuid.UUID(str(cfg[field])))
            except ValueError:
                integ = None
            if not integ or integ.organization_id != organization_id or integ.deleted_at:
                raise ValidationFailed(f"'{team['config_schema']['properties'][field]['title']}' is not an integration of this organization")
    existing = team_members(db, organization_id, team_key)
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.organization_id == organization_id, KnowledgeBase.name == team["playbook"]))
    if kb is None:
        kb = KnowledgeBase(organization_id=organization_id, name=team["playbook"], description=f"Shared memory of the {team['name']}")
        db.add(kb)
        db.flush()
        add_document(db, kb, f"{cfg['business_name']} - marketing brief", _playbook_text(team, cfg), source="team install")
    fill = {"business": cfg["business_name"], "channels": cfg.get("channels") or "", "playbook": str(kb.id),
            "calendar_integration": cfg.get("calendar_integration") or ""}

    def make(spec: dict, lead: bool) -> AIAgent:
        agent = existing.get(spec["key"])
        if agent is not None:
            return agent
        instructions = spec["instructions"].format(**fill)
        if not lead:
            instructions = _BASE.format(**fill) + "\n\n" + instructions
        agent = AIAgent(organization_id=organization_id, name=spec["name"], description=spec["description"], agent_type=spec["agent_type"],
                        system_instructions=instructions, provider=provider, model=model, max_iterations=12 if lead else 8,
                        knowledge_base_ids=[str(kb.id)], limits={"max_executions_per_day": 60 if lead else 120},
                        permissions={"team_member": spec["key"]}, team_key=team_key, team_module=spec["module"], team_lead=lead)
        db.add(agent)
        db.flush()
        db.add(AIAgentTool(organization_id=organization_id, agent_id=agent.id, name="knowledge_search", tool_key="knowledge_search",
                           config={"knowledge_base_ids": [str(kb.id)]}, requires_approval=False))
        from app.ai.tools import TOOLS

        for tool_key, tool_config in spec["tools"]:
            conf = {k: (v.format(**fill) if isinstance(v, str) else v) for k, v in tool_config.items()}
            if any(v in ("", None) for v in conf.values()):
                continue  # optional integration not chosen at install: the member works without that tool
            db.add(AIAgentTool(organization_id=organization_id, agent_id=agent.id, name=tool_key, tool_key=tool_key, config=conf,
                               requires_approval=TOOLS[tool_key].high_impact))
        return agent

    members = {spec["key"]: make(spec, False) for spec in team["members"]}
    lead = make(team["lead"], True)
    have = {t.name for t in db.scalars(select(AIAgentTool).where(AIAgentTool.agent_id == lead.id))}
    for key, member in members.items():
        name = f"delegate_to_{key}"
        if name not in have:
            db.add(AIAgentTool(organization_id=organization_id, agent_id=lead.id, name=name, tool_key="delegate_to_agent",
                               config={"agent_id": str(member.id), "description": f"Team member: {member.name} - {member.description}"},
                               requires_approval=False))
    db.flush()
    out = {f"{key}_agent": str(a.id) for key, a in members.items()}
    out.update({f"{team['lead']['key']}_agent": str(lead.id), "playbook_kb": str(kb.id)})
    out.update({k: v for k, v in cfg.items() if isinstance(v, (str, int, float))})
    return out
