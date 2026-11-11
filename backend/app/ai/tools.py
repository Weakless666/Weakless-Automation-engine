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
]}


def tool_definition_for_model(name: str, tool_key: str, config: dict) -> dict:
    """The tool schema exposed to the model for one allowlisted agent tool."""
    t = TOOLS[tool_key]
    schema, description = t.input_schema, t.description
    if tool_key == "mcp_tool":
        schema = config.get("input_schema") or {"type": "object", "properties": {}}
        description = config.get("description") or f"MCP tool '{config.get('tool_name')}'"
    return {"name": name, "description": description, "input_schema": schema}
