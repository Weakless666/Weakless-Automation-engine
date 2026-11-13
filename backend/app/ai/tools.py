"""Controlled tool registry for AI agents.

Agents have no ambient capabilities. Every external action goes through a tool
defined here, and an agent can only call the tools an engineer attached to it
(``ai_agent_tools``), each with its own scoped configuration (which
integration, which domains, which workflow, which MCP tool). There are no
shell, filesystem, raw-database, payment or administrative tools.

``high_impact`` tools default to requiring human approval when attached.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.core import http
from app.core.errors import AppError, NodeError
from app.db import utcnow


@dataclass
class ToolContext:
    db: Session
    organization_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    secrets: set[str] = field(default_factory=set)
    execution_id: uuid.UUID | None = None  # the agent execution calling the tool (delegation chains hang off it)


MAX_DELEGATION_DEPTH = 2  # lead -> member -> member's helper; deeper chains cost a lot and rarely help


def _delegation_chain(db: Session, execution_id: uuid.UUID | None) -> list[uuid.UUID]:
    """Agent ids from the calling execution up through every delegating parent."""
    from app.models import AIAgentExecution

    chain, seen = [], set()
    while execution_id and execution_id not in seen and len(chain) <= MAX_DELEGATION_DEPTH + 1:
        seen.add(execution_id)
        ex = db.get(AIAgentExecution, execution_id)
        if not ex:
            break
        if ex.agent_id:
            chain.append(ex.agent_id)
        execution_id = ex.parent_execution_id
    return chain


def _delegate(tc: ToolContext, config: dict, inp: dict) -> Any:
    """Hand one task to a specific team member and return its answer. The member works in a fresh
    conversation, with its own tools, approvals, limits and usage metering."""
    from app.ai.agents import run_agent
    from app.models import AIAgent

    try:
        target = tc.db.get(AIAgent, uuid.UUID(str(config.get("agent_id"))))
    except ValueError:
        target = None
    if not target or target.organization_id != tc.organization_id or target.deleted_at:
        raise NodeError("That team member does not exist in this organization", code="tool_denied")
    chain = _delegation_chain(tc.db, tc.execution_id)
    if target.id in chain or target.id == tc.agent_id:
        raise NodeError(f"{target.name} is already working on this task higher up the chain; delegating back would loop", code="tool_denied")
    if len(chain) > MAX_DELEGATION_DEPTH:
        raise NodeError("Delegation is too deep: finish this part yourself", code="tool_denied")
    task = str(inp.get("task") or "").strip()
    if not task:
        raise NodeError("Describe the task to delegate", code="invalid_input")
    context = str(inp.get("context") or "").strip()
    message = f"{task}\n\nContext from the team lead:\n{context}" if context else task
    sub = run_agent(tc.db, target, message[:20000], parent_execution_id=tc.execution_id)
    if sub.status == "COMPLETED":
        return {"team_member": target.name, "result": sub.output_text or ""}
    if sub.status == "WAITING_APPROVAL":
        return {"team_member": target.name, "status": "waiting_for_human_approval",
                "note": "Part of this task needs a person's approval. It continues on its own once approved; do not repeat it."}
    raise NodeError(f"{target.name} could not complete the task: {sub.error or 'unknown error'}", code="delegation_failed")


def _content_add(tc: ToolContext, config: dict, inp: dict) -> Any:
    from datetime import datetime, timezone

    from app.services.content import create_item

    allowed = [c.lower() for c in config.get("channels") or []]
    channel = str(inp.get("channel") or "").lower()
    if allowed and channel not in allowed:
        raise NodeError(f"This agent may only plan content for: {', '.join(allowed)}", code="tool_denied")
    when = None
    if inp.get("scheduled_for"):
        try:
            when = datetime.fromisoformat(str(inp["scheduled_for"]).replace("Z", "+00:00"))
            when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        except ValueError:
            raise NodeError("scheduled_for must be an ISO 8601 date and time", code="invalid_input")
    try:
        item = create_item(tc.db, tc.organization_id, title=str(inp.get("title") or ""), channel=channel, body=str(inp.get("body") or ""),
                           media_notes=str(inp.get("media_notes") or ""), hashtags=list(inp.get("hashtags") or []),
                           scheduled_for=when, status="awaiting_approval", agent_id=tc.agent_id,
                           meta={k: inp[k] for k in ("campaign", "keywords") if inp.get(k)})
    except AppError as exc:
        raise NodeError(exc.message, code="invalid_input") from exc
    return {"content_item_id": str(item.id), "status": item.status, "note": "Added to the content calendar; a person approves it before anything is published."}


def _knowledge_note(tc: ToolContext, config: dict, inp: dict) -> Any:
    from app.ai.knowledge import add_document
    from app.models import KnowledgeBase

    try:
        kb = tc.db.get(KnowledgeBase, uuid.UUID(str(config.get("knowledge_base_id"))))
    except ValueError:
        kb = None
    if not kb or kb.organization_id != tc.organization_id:
        raise NodeError("Knowledge base not found in this organization", code="tool_denied")
    title, text = str(inp.get("title") or "").strip(), str(inp.get("content") or "").strip()
    if not title or not text:
        raise NodeError("A note needs a title and content", code="invalid_input")
    doc = add_document(tc.db, kb, title[:300], text[:20000], source="agent", meta={"agent_id": str(tc.agent_id) if tc.agent_id else None})
    return {"document_id": str(doc.id), "knowledge_base": kb.name}


def _content_list(tc: ToolContext, config: dict, inp: dict) -> Any:
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import ContentItem
    from app.services.content import view

    days = max(1, min(90, int(inp.get("days") or 14)))
    since = utcnow() - timedelta(days=days)
    q = select(ContentItem).where(ContentItem.organization_id == tc.organization_id, ContentItem.updated_at >= since)
    if inp.get("status"):
        q = q.where(ContentItem.status == str(inp["status"]))
    rows = tc.db.scalars(q.order_by(ContentItem.scheduled_for.desc().nulls_last()).limit(50)).all()
    return {"items": [{k: v for k, v in view(r).items() if k in ("id", "title", "channel", "status", "scheduled_for", "published_url")} for r in rows]}


@dataclass
class ToolDef:
    key: str
    label: str
    description: str
    input_schema: dict
    config_fields: list[dict]  # [{name, label, type, required, help}] - rendered as a form in the UI
    run: Callable[[ToolContext, dict, dict], Any]
    high_impact: bool = False

    def describe(self) -> dict:
        return {"key": self.key, "label": self.label, "description": self.description, "input_schema": self.input_schema,
                "config_fields": self.config_fields, "high_impact": self.high_impact}

    def validate_config(self, config: dict) -> None:
        for f in self.config_fields:
            if f.get("required") and config.get(f["name"]) in (None, "", []):
                raise AppError(f"Tool '{self.label}' requires '{f['label']}'", code="validation_error", status_code=422)


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_S = {"type": "string"}
_INTEGRATION = {"name": "integration_id", "label": "Integration", "type": "integration", "required": True}


def _action(action: str, mapper: Callable[[dict], dict] = lambda i: i):
    def run(tc: ToolContext, config: dict, inp: dict) -> Any:
        from app.integrations.service import run_action

        return run_action(tc.db, tc.organization_id, config.get("integration_id"), action, mapper(inp), secrets_out=tc.secrets)

    return run


def _http_request(tc: ToolContext, config: dict, inp: dict) -> Any:
    allowed = [d.strip().lower() for d in config.get("allowed_domains") or [] if d.strip()]
    methods = [m.upper() for m in (config.get("allowed_methods") or ["GET"])]
    host = (urlsplit(str(inp.get("url", ""))).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in allowed):
        raise NodeError(f"Domain '{host}' is not on this tool's allowlist", code="tool_denied")
    method = str(inp.get("method") or "GET").upper()
    if method not in methods:
        raise NodeError(f"HTTP method {method} is not allowed for this tool", code="tool_denied")
    resp = http.request(method, inp["url"], json=inp.get("body") if method != "GET" else None, timeout=20)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:8000]
    return {"status": resp.status_code, "body": body}


def _knowledge_search(tc: ToolContext, config: dict, inp: dict) -> Any:
    from app.ai.knowledge import search

    return {"results": search(tc.db, tc.organization_id, config.get("knowledge_base_ids") or [], str(inp.get("query", "")), limit=5)}


def _run_workflow(tc: ToolContext, config: dict, inp: dict) -> Any:
    from app.models import Workflow
    from app.services.executions import start_execution

    try:
        wf = tc.db.get(Workflow, uuid.UUID(str(config.get("workflow_id"))))
    except ValueError:
        wf = None
    if not wf or wf.organization_id != tc.organization_id or wf.deleted_at:
        raise NodeError("Workflow not found in this organization", code="tool_denied")
    try:
        ex = start_execution(tc.db, wf, trigger_type="manual", payload={"source": "agent", "input": inp.get("payload") or {}})
    except AppError as exc:
        raise NodeError(exc.message, code=exc.code) from exc
    return {"execution_id": str(ex.id), "status": ex.status}


def _mcp_tool(tc: ToolContext, config: dict, inp: dict) -> Any:
    from app.ai.mcp import McpClient
    from app.integrations.service import connection, get_integration

    integration = get_integration(tc.db, tc.organization_id, config.get("integration_id"))
    if integration.provider_key != "mcp_server":
        raise NodeError("Integration is not an MCP server", code="tool_denied")
    if integration.status != "connected":
        raise NodeError("MCP server integration is not connected", code="integration_not_connected")
    conn = connection(tc.db, integration)
    tc.secrets.update(conn.secrets())
    # The remote tool name comes from the stored allowlist entry, never from the model.
    return McpClient(str(conn.get("url")), conn.get("api_key") or None).call_tool(str(config.get("tool_name")), inp)


TOOLS: dict[str, ToolDef] = {t.key: t for t in [
    ToolDef("current_time", "Current time", "Get the current date and time in UTC (ISO 8601).", _obj({}, []), [],
            lambda tc, c, i: {"utc": utcnow().isoformat()}),
    ToolDef("knowledge_search", "Knowledge base search", "Search the organization's knowledge base for relevant passages.",
            _obj({"query": {**_S, "description": "What to look for"}}, ["query"]),
            [{"name": "knowledge_base_ids", "label": "Knowledge bases", "type": "knowledge_bases", "required": True}], _knowledge_search),
    ToolDef("http_request", "HTTP request", "Call an HTTP API on an allowlisted domain.",
            _obj({"method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]}, "url": _S, "body": {"type": "object"}}, ["method", "url"]),
            [{"name": "allowed_domains", "label": "Allowed domains", "type": "string_list", "required": True, "help": "e.g. api.example.com"},
             {"name": "allowed_methods", "label": "Allowed methods", "type": "string_list", "required": False, "help": "Default: GET only"}],
            _http_request),
    ToolDef("send_email", "Send email", "Send an email through the configured email integration.",
            _obj({"to": _S, "subject": _S, "body": _S}, ["to", "subject", "body"]), [_INTEGRATION], _action("send_email"), high_impact=True),
    ToolDef("send_message", "Send message", "Send a chat message (WhatsApp, Telegram, Slack or Discord) through the configured integration.",
            _obj({"to": {**_S, "description": "Recipient: phone number, chat id or channel"}, "text": _S}, ["to", "text"]),
            [_INTEGRATION], _action("send_message"), high_impact=True),
    ToolDef("create_crm_lead", "Create CRM lead", "Create a lead/contact in the configured CRM.",
            _obj({"email": _S, "first_name": _S, "last_name": _S, "phone": _S, "company": _S}, ["email"]),
            [_INTEGRATION], _action("create_lead"), high_impact=True),
    ToolDef("create_calendar_event", "Create calendar event", "Create an event in the configured calendar.",
            _obj({"title": _S, "start": {**_S, "description": "ISO 8601 start"}, "end": {**_S, "description": "ISO 8601 end"},
                  "description": _S, "timezone": _S}, ["title", "start", "end"]),
            [_INTEGRATION], _action("create_calendar_event"), high_impact=True),
    ToolDef("run_workflow", "Run workflow", "Start one specific, pre-selected workflow of this organization.",
            _obj({"payload": {"type": "object", "description": "Input data for the workflow"}}, []),
            [{"name": "workflow_id", "label": "Workflow", "type": "workflow", "required": True}], _run_workflow, high_impact=True),
    ToolDef("mcp_tool", "MCP tool", "Call one allowlisted tool on a connected MCP server.", _obj({}, []),
            [{**_INTEGRATION, "label": "MCP server integration"},
             {"name": "tool_name", "label": "Remote tool name", "type": "string", "required": True},
             {"name": "input_schema", "label": "Input schema (JSON Schema)", "type": "json", "required": False,
              "help": "Filled automatically when you pick a tool discovered on the server."},
             {"name": "description", "label": "Description shown to the model", "type": "string", "required": False}],
            _mcp_tool, high_impact=True),
    ToolDef("delegate_to_agent", "Delegate to team member", "Give one task to a specific team member and get their result back.",
            _obj({"task": {**_S, "description": "What the team member should do, with the expected output"},
                  "context": {**_S, "description": "Facts, earlier results or constraints they need"}}, ["task"]),
            [{"name": "agent_id", "label": "Team member", "type": "agent", "required": True},
             {"name": "description", "label": "When to use this member", "type": "string", "required": False,
              "help": "Shown to the model, e.g. 'Writes social posts'."}], _delegate),
    ToolDef("content_calendar_add", "Add to content calendar",
            "Propose a post, email or article for the content calendar. A person approves it before it is published.",
            _obj({"title": _S, "channel": {"type": "string", "enum": ["linkedin", "instagram", "facebook", "tiktok", "youtube", "x", "blog", "newsletter", "email", "other"]},
                  "body": {**_S, "description": "The full text, ready to publish"},
                  "media_notes": {**_S, "description": "Brief for the image or video to go with it"},
                  "hashtags": {"type": "array", "items": _S}, "scheduled_for": {**_S, "description": "ISO 8601 date and time to publish"},
                  "campaign": _S, "keywords": {"type": "array", "items": _S}}, ["title", "channel", "body"]),
            [{"name": "channels", "label": "Allowed channels", "type": "string_list", "required": False, "help": "Leave empty to allow every channel"}],
            _content_add),
    ToolDef("content_calendar_list", "Read content calendar", "See recent and planned content and its status (what was approved, published or rejected).",
            _obj({"days": {"type": "integer", "description": "How far back to look (default 14)"},
                  "status": {"type": "string", "enum": ["awaiting_approval", "approved", "published", "rejected", "failed"]}}, []),
            [], _content_list),
    ToolDef("knowledge_note", "Save note to knowledge base",
            "Save a finding, decision or lesson to a knowledge base so the team can look it up later.",
            _obj({"title": _S, "content": {**_S, "description": "The note in plain language"}}, ["title", "content"]),
            [{"name": "knowledge_base_id", "label": "Knowledge base", "type": "knowledge_base", "required": True}], _knowledge_note),
]}


def tool_definition_for_model(name: str, tool_key: str, config: dict) -> dict:
    """The tool schema exposed to the model for one allowlisted agent tool."""
    t = TOOLS[tool_key]
    schema, description = t.input_schema, t.description
    if tool_key == "mcp_tool":
        schema = config.get("input_schema") or {"type": "object", "properties": {}}
        description = config.get("description") or f"MCP tool '{config.get('tool_name')}'"
    elif tool_key == "delegate_to_agent" and config.get("description"):
        description = f"{description} {config['description']}"  # who this member is, so the lead picks the right one
    return {"name": name, "description": description, "input_schema": schema}
