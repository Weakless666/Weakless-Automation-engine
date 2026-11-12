"""AI agents, their tool allowlists, conversations and knowledge bases."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import func, select

from app.ai import agents as agent_engine
from app.ai import knowledge
from app.ai.providers import ANTHROPIC_MODELS
from app.ai.tools import TOOLS
from app.api import schemas
from app.api.common import ApiRoute
from app.api.common import Body, Paging, iso, owned, paginate
from app.core.errors import AppError, Conflict, NodeError, ValidationFailed
from app.db import utcnow
from app.deps import Ctx, org_ctx
from app.models import (
    AIAgent, AIAgentExecution, AIAgentTool, AIConversation, AIMessage, Integration, KnowledgeBase, KnowledgeChunk, KnowledgeDocument, Workflow,
)
from app.services.audit import audit

router = APIRouter(route_class=ApiRoute, tags=["agents"])
PROVIDERS = ("anthropic", "openai", "gemini", "openai_compatible")


def agent_view(a: AIAgent, tools: list[AIAgentTool] | None = None) -> dict:
    out = {"id": str(a.id), "name": a.name, "description": a.description, "agent_type": a.agent_type,
           "system_instructions": a.system_instructions, "provider": a.provider, "model": a.model, "fallbacks": a.fallbacks or [],
           "temperature": a.temperature, "max_tokens": a.max_tokens, "max_iterations": a.max_iterations,
           "provider_options": a.provider_options or {}, "limits": a.limits or {}, "knowledge_base_ids": a.knowledge_base_ids or [],
           "status": a.status, "created_at": iso(a.created_at), "updated_at": iso(a.updated_at)}
    if tools is not None:
        out["tools"] = [tool_view(t) for t in tools]
    return out


def tool_view(t: AIAgentTool) -> dict:
    return {"id": str(t.id), "name": t.name, "tool_key": t.tool_key, "config": t.config or {}, "requires_approval": t.requires_approval,
            "high_impact": TOOLS[t.tool_key].high_impact if t.tool_key in TOOLS else False}


class Fallback(Body):
    provider: Literal["anthropic", "openai", "gemini", "openai_compatible"]
    model: str = Field(..., min_length=1, max_length=120)


class Limits(Body):
    max_executions_per_day: int | None = Field(None, ge=1)
    max_tokens_per_day: int | None = Field(None, ge=1)


class AgentBody(Body):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=5000)
    agent_type: str = "custom"
    system_instructions: str = Field("", max_length=50000)
    provider: Literal["anthropic", "openai", "gemini", "openai_compatible"] = "anthropic"
    model: str = Field("claude-opus-5-5", min_length=1, max_length=120)
    fallbacks: list[Fallback] = Field(default_factory=list, max_length=3)
    temperature: float | None = Field(None, ge=0, le=2, description="Ignored by providers/models that do not accept sampling parameters (current Claude models)")
    max_tokens: int = Field(16000, ge=256, le=64000)
    max_iterations: int = Field(8, ge=1, le=30)
    provider_options: dict[str, Any] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    status: Literal["active", "disabled"] = "active"


def _validate_agent(ctx: Ctx, body: AgentBody) -> None:
    if body.agent_type not in agent_engine.AGENT_TYPES:
        raise ValidationFailed(f"agent_type must be one of: {', '.join(agent_engine.AGENT_TYPES)}")
    effort = body.provider_options.get("effort")
    if effort is not None and effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValidationFailed("provider_options.effort must be low, medium, high, xhigh or max")
    if set(body.provider_options) - {"effort"}:
        raise ValidationFailed("Unsupported provider option")
    for kb_id in body.knowledge_base_ids:
        owned(ctx.db, KnowledgeBase, kb_id, ctx.org_id, "Knowledge base")


@router.get("/agents/meta", response_model=schemas.AgentsMeta)
def agents_meta(ctx: Ctx = Depends(org_ctx("agents.read"))) -> dict:
    return {"agent_types": [{"key": k, "label": v} for k, v in agent_engine.AGENT_TYPES.items()],
            "providers": list(PROVIDERS), "anthropic_models": ANTHROPIC_MODELS, "default_model": "claude-opus-5-5",
            "tools": [t.describe() for t in TOOLS.values()]}


@router.get("/agents", response_model=schemas.Page[schemas.AgentOut])
def list_agents(paging: Paging = Depends(), ctx: Ctx = Depends(org_ctx("agents.read"))) -> dict:
    q = select(AIAgent).where(AIAgent.organization_id == ctx.org_id, AIAgent.deleted_at.is_(None)).order_by(AIAgent.name)
    page = paginate(ctx.db, q, paging, agent_view)
    counts = dict(ctx.db.execute(select(AIAgentTool.agent_id, func.count()).where(AIAgentTool.organization_id == ctx.org_id).group_by(AIAgentTool.agent_id)).all())
    for item in page["items"]:
        item["tool_count"] = next((v for k, v in counts.items() if str(k) == item["id"]), 0)
    return page


def _sync_kb_tool(ctx: Ctx, agent: AIAgent) -> None:
    """Agents with knowledge bases get a knowledge_search tool scoped to exactly those bases."""
    tool = ctx.db.scalar(select(AIAgentTool).where(AIAgentTool.agent_id == agent.id, AIAgentTool.name == "knowledge_search"))
    if agent.knowledge_base_ids:
        if not tool:
            tool = AIAgentTool(organization_id=agent.organization_id, agent_id=agent.id, name="knowledge_search", tool_key="knowledge_search")
            ctx.db.add(tool)
        tool.config = {"knowledge_base_ids": list(agent.knowledge_base_ids)}
    elif tool:
        ctx.db.delete(tool)
    ctx.db.flush()


@router.post("/agents", response_model=schemas.AgentOut, status_code=201)
def create_agent(body: AgentBody, ctx: Ctx = Depends(org_ctx("agents.write"))) -> dict:
    _validate_agent(ctx, body)
    data = body.model_dump()
    data["limits"] = body.limits.model_dump(exclude_none=True)
    agent = AIAgent(organization_id=ctx.org_id, **data)
    ctx.db.add(agent)
    ctx.db.flush()
    _sync_kb_tool(ctx, agent)
    audit(ctx.db, "agent.created", ctx=ctx, resource_type="agent", resource_id=agent.id, meta={"name": agent.name})
    ctx.db.commit()
    return agent_view(agent, [])


def _agent_tools(ctx: Ctx, agent: AIAgent) -> list[AIAgentTool]:
    return list(ctx.db.scalars(select(AIAgentTool).where(AIAgentTool.agent_id == agent.id).order_by(AIAgentTool.name)))


@router.get("/agents/{agent_id}", response_model=schemas.AgentOut)
def get_agent(agent_id: str, ctx: Ctx = Depends(org_ctx("agents.read"))) -> dict:
    agent = owned(ctx.db, AIAgent, agent_id, ctx.org_id, "Agent")
    return agent_view(agent, _agent_tools(ctx, agent))


@router.put("/agents/{agent_id}", response_model=schemas.AgentOut)
def update_agent(agent_id: str, body: AgentBody, ctx: Ctx = Depends(org_ctx("agents.write"))) -> dict:
    agent = owned(ctx.db, AIAgent, agent_id, ctx.org_id, "Agent")
    _validate_agent(ctx, body)
    data = body.model_dump()
    data["limits"] = body.limits.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(agent, k, v)
    _sync_kb_tool(ctx, agent)
    audit(ctx.db, "agent.updated", ctx=ctx, resource_type="agent", resource_id=agent.id)
    ctx.db.commit()
    return agent_view(agent, _agent_tools(ctx, agent))


@router.delete("/agents/{agent_id}", response_model=schemas.Ok)
def delete_agent(agent_id: str, ctx: Ctx = Depends(org_ctx("agents.write"))) -> dict:
    agent = owned(ctx.db, AIAgent, agent_id, ctx.org_id, "Agent")
    agent.deleted_at, agent.status = utcnow(), "disabled"
    audit(ctx.db, "agent.deleted", ctx=ctx, resource_type="agent", resource_id=agent.id, meta={"name": agent.name})
    ctx.db.commit()
    return {"ok": True}


class ToolBody(Body):
    name: str = Field(..., pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$", description="Name the model sees")
    tool_key: str
    config: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool | None = Field(None, description="Defaults to true for high-impact tools")


def _validate_tool(ctx: Ctx, body: ToolBody) -> bool:
    tool = TOOLS.get(body.tool_key)
    if not tool:
        raise ValidationFailed(f"Unknown tool '{body.tool_key}'")
    tool.validate_config(body.config)
    # Every referenced resource must belong to this organization.
    if body.config.get("integration_id"):
        owned(ctx.db, Integration, body.config["integration_id"], ctx.org_id, "Integration")
    if body.config.get("workflow_id"):
        owned(ctx.db, Workflow, body.config["workflow_id"], ctx.org_id, "Workflow")
    for kb in body.config.get("knowledge_base_ids") or []:
        owned(ctx.db, KnowledgeBase, kb, ctx.org_id, "Knowledge base")
    if body.tool_key == "http_request":
        for d in body.config.get("allowed_domains") or []:
            if not isinstance(d, str) or "/" in d or "*" in d or "." not in d:
                raise ValidationFailed("Allowed domains must be plain hostnames such as api.example.com")
    return tool.high_impact if body.requires_approval is None else body.requires_approval


@router.post("/agents/{agent_id}/tools", response_model=schemas.AgentToolOut, status_code=201)
def add_tool(agent_id: str, body: ToolBody, ctx: Ctx = Depends(org_ctx("agents.write"))) -> dict:
    agent = owned(ctx.db, AIAgent, agent_id, ctx.org_id, "Agent")
    requires = _validate_tool(ctx, body)
    if ctx.db.scalar(select(AIAgentTool.id).where(AIAgentTool.agent_id == agent.id, AIAgentTool.name == body.name)):
        raise Conflict("This agent already has a tool with that name")
    t = AIAgentTool(organization_id=ctx.org_id, agent_id=agent.id, name=body.name, tool_key=body.tool_key, config=body.config, requires_approval=requires)
    ctx.db.add(t)
    ctx.db.flush()
    audit(ctx.db, "agent.tool_added", ctx=ctx, resource_type="agent", resource_id=agent.id,
          meta={"tool": body.name, "tool_key": body.tool_key, "requires_approval": requires})
    ctx.db.commit()
    return tool_view(t)


@router.put("/agents/{agent_id}/tools/{tool_id}", response_model=schemas.AgentToolOut)
def update_tool(agent_id: str, tool_id: str, body: ToolBody, ctx: Ctx = Depends(org_ctx("agents.write"))) -> dict:
    agent = owned(ctx.db, AIAgent, agent_id, ctx.org_id, "Agent")
    t = owned(ctx.db, AIAgentTool, tool_id, ctx.org_id, "Tool")
    if t.agent_id != agent.id:
        raise ValidationFailed("Tool does not belong to this agent")
    requires = _validate_tool(ctx, body)
    t.name, t.tool_key, t.config, t.requires_approval = body.name, body.tool_key, body.config, requires
    audit(ctx.db, "agent.tool_updated", ctx=ctx, resource_type="agent", resource_id=agent.id, meta={"tool": body.name, "requires_approval": requires})
    ctx.db.commit()
    return tool_view(t)


@router.delete("/agents/{agent_id}/tools/{tool_id}", response_model=schemas.Ok)
def remove_tool(agent_id: str, tool_id: str, ctx: Ctx = Depends(org_ctx("agents.write"))) -> dict:
    agent = owned(ctx.db, AIAgent, agent_id, ctx.org_id, "Agent")
    t = owned(ctx.db, AIAgentTool, tool_id, ctx.org_id, "Tool")
    if t.agent_id != agent.id:
        raise ValidationFailed("Tool does not belong to this agent")
    audit(ctx.db, "agent.tool_removed", ctx=ctx, resource_type="agent", resource_id=agent.id, meta={"tool": t.name})
    ctx.db.delete(t)
    ctx.db.commit()
    return {"ok": True}


class ChatBody(Body):
    message: str = Field(..., min_length=1, max_length=50000)
    conversation_id: str | None = None


def agent_exec_view(e: AIAgentExecution) -> dict:
    return {"id": str(e.id), "agent_id": str(e.agent_id) if e.agent_id else None, "kind": e.kind, "status": e.status,
            "conversation_id": str(e.conversation_id) if e.conversation_id else None, "provider": e.provider, "model": e.model,
            "input_tokens": e.input_tokens, "output_tokens": e.output_tokens, "cost_usd": float(e.cost_usd) if e.cost_usd is not None else None,
            "iterations": e.iterations, "output_text": e.output_text, "error": e.error, "trace": e.trace or [],
            "awaiting_approval_id": ((e.pending or {}).get("awaiting") or {}).get("approval_id") if e.status == "WAITING_APPROVAL" else None,
            "created_at": iso(e.created_at), "finished_at": iso(e.finished_at)}


@router.post("/agents/{agent_id}/chat", response_model=schemas.AgentExecutionOut)
def chat(agent_id: str, body: ChatBody, ctx: Ctx = Depends(org_ctx("agents.run"))) -> dict:
    agent = owned(ctx.db, AIAgent, agent_id, ctx.org_id, "Agent")
    if body.conversation_id:
        owned(ctx.db, AIConversation, body.conversation_id, ctx.org_id, "Conversation")
    try:
        aexec = agent_engine.run_agent(ctx.db, agent, body.message, conversation_id=body.conversation_id)
    except NodeError as exc:
        ctx.db.rollback()
        raise AppError(exc.message, code=exc.code, status_code=409) from exc
    ctx.db.commit()
    return agent_exec_view(aexec)


@router.get("/agents/{agent_id}/executions", response_model=schemas.Page[schemas.AgentExecutionOut])
def agent_executions(agent_id: str, paging: Paging = Depends(), ctx: Ctx = Depends(org_ctx("agents.read"))) -> dict:
    agent = owned(ctx.db, AIAgent, agent_id, ctx.org_id, "Agent")
    q = select(AIAgentExecution).where(AIAgentExecution.agent_id == agent.id).order_by(AIAgentExecution.created_at.desc())
    return paginate(ctx.db, q, paging, agent_exec_view)


@router.get("/agent-executions/{execution_id}", response_model=schemas.AgentExecutionOut)
def get_agent_execution(execution_id: str, ctx: Ctx = Depends(org_ctx("agents.read"))) -> dict:
    return agent_exec_view(owned(ctx.db, AIAgentExecution, execution_id, ctx.org_id, "Agent execution"))


@router.get("/agents/{agent_id}/conversations", response_model=schemas.Page[schemas.ConversationOut])
def conversations(agent_id: str, paging: Paging = Depends(), ctx: Ctx = Depends(org_ctx("agents.read"))) -> dict:
    agent = owned(ctx.db, AIAgent, agent_id, ctx.org_id, "Agent")
    q = select(AIConversation).where(AIConversation.agent_id == agent.id).order_by(AIConversation.updated_at.desc())
    return paginate(ctx.db, q, paging, lambda c: {"id": str(c.id), "title": c.title, "external_ref": c.external_ref, "status": c.status,
                                                  "created_at": iso(c.created_at), "updated_at": iso(c.updated_at)})


@router.get("/conversations/{conversation_id}/messages", response_model=schemas.Items[schemas.MessageOut])
def messages(conversation_id: str, ctx: Ctx = Depends(org_ctx("agents.read"))) -> dict:
    conv = owned(ctx.db, AIConversation, conversation_id, ctx.org_id, "Conversation")
    rows = ctx.db.scalars(select(AIMessage).where(AIMessage.conversation_id == conv.id).order_by(AIMessage.sequence).limit(500)).all()
    # Provider-native blocks (e.g. model reasoning metadata) stay server-side.
    return {"items": [{"id": str(m.id), "role": m.role, "text": (m.content or {}).get("text", ""),
                       "tool_calls": (m.content or {}).get("tool_calls") or [], "tool_results": (m.content or {}).get("tool_results") or [],
                       "created_at": iso(m.created_at)} for m in rows]}


# ----------------------------------------------------------------------------- knowledge bases


class KbBody(Body):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=5000)


class DocBody(Body):
    title: str = Field(..., min_length=1, max_length=300)
    content: str = Field(..., min_length=1, max_length=knowledge.MAX_DOCUMENT_CHARS)
    source: str = Field("", max_length=500)


def kb_view(kb: KnowledgeBase, docs: int = 0, chunks: int = 0) -> dict:
    return {"id": str(kb.id), "name": kb.name, "description": kb.description, "documents": docs, "chunks": chunks, "created_at": iso(kb.created_at)}


@router.get("/knowledge-bases", response_model=schemas.Items[schemas.KnowledgeBaseOut])
def list_kbs(ctx: Ctx = Depends(org_ctx("knowledge.read"))) -> dict:
    db = ctx.db
    kbs = db.scalars(select(KnowledgeBase).where(KnowledgeBase.organization_id == ctx.org_id).order_by(KnowledgeBase.name)).all()
    docs = dict(db.execute(select(KnowledgeDocument.knowledge_base_id, func.count()).where(KnowledgeDocument.organization_id == ctx.org_id)
                           .group_by(KnowledgeDocument.knowledge_base_id)).all())
    chunks = dict(db.execute(select(KnowledgeChunk.knowledge_base_id, func.count()).where(KnowledgeChunk.organization_id == ctx.org_id)
                             .group_by(KnowledgeChunk.knowledge_base_id)).all())
    return {"items": [kb_view(k, docs.get(k.id, 0), chunks.get(k.id, 0)) for k in kbs]}


@router.post("/knowledge-bases", response_model=schemas.KnowledgeBaseOut, status_code=201)
def create_kb(body: KbBody, ctx: Ctx = Depends(org_ctx("knowledge.write"))) -> dict:
    if ctx.db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.organization_id == ctx.org_id, KnowledgeBase.name == body.name)):
        raise Conflict("A knowledge base with this name already exists")
    kb = KnowledgeBase(organization_id=ctx.org_id, name=body.name, description=body.description)
    ctx.db.add(kb)
    ctx.db.commit()
    return kb_view(kb)


@router.delete("/knowledge-bases/{kb_id}", response_model=schemas.Ok)
def delete_kb(kb_id: str, ctx: Ctx = Depends(org_ctx("knowledge.write"))) -> dict:
    kb = owned(ctx.db, KnowledgeBase, kb_id, ctx.org_id, "Knowledge base")
    ctx.db.delete(kb)  # documents and chunks cascade
    audit(ctx.db, "knowledge_base.deleted", ctx=ctx, resource_type="knowledge_base", resource_id=kb.id, meta={"name": kb.name})
    ctx.db.commit()
    return {"ok": True}


@router.get("/knowledge-bases/{kb_id}/documents", response_model=schemas.Page[schemas.KnowledgeDocumentOut])
def list_docs(kb_id: str, paging: Paging = Depends(), ctx: Ctx = Depends(org_ctx("knowledge.read"))) -> dict:
    kb = owned(ctx.db, KnowledgeBase, kb_id, ctx.org_id, "Knowledge base")
    q = select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb.id).order_by(KnowledgeDocument.created_at.desc())
    return paginate(ctx.db, q, paging, lambda d: {"id": str(d.id), "title": d.title, "source": d.source, "characters": len(d.content),
                                                  "created_at": iso(d.created_at)})


@router.post("/knowledge-bases/{kb_id}/documents", response_model=schemas.DocumentCreated, status_code=201)
def add_doc(kb_id: str, body: DocBody, ctx: Ctx = Depends(org_ctx("knowledge.write"))) -> dict:
    kb = owned(ctx.db, KnowledgeBase, kb_id, ctx.org_id, "Knowledge base")
    doc = knowledge.add_document(ctx.db, kb, body.title, body.content, body.source)
    ctx.db.commit()
    return {"id": str(doc.id), "title": doc.title}


@router.delete("/knowledge-bases/{kb_id}/documents/{doc_id}", response_model=schemas.Ok)
def delete_doc(kb_id: str, doc_id: str, ctx: Ctx = Depends(org_ctx("knowledge.write"))) -> dict:
    kb = owned(ctx.db, KnowledgeBase, kb_id, ctx.org_id, "Knowledge base")
    doc = owned(ctx.db, KnowledgeDocument, doc_id, ctx.org_id, "Document")
    if doc.knowledge_base_id != kb.id:
        raise ValidationFailed("Document does not belong to this knowledge base")
    knowledge.delete_document(ctx.db, doc)
    ctx.db.commit()
    return {"ok": True}


class SearchBody(Body):
    query: str = Field(..., min_length=1, max_length=2000)
    limit: int = Field(5, ge=1, le=20)


@router.post("/knowledge-bases/{kb_id}/search", response_model=schemas.SearchResults)
def search_kb(kb_id: str, body: SearchBody, ctx: Ctx = Depends(org_ctx("knowledge.read"))) -> dict:
    kb = owned(ctx.db, KnowledgeBase, kb_id, ctx.org_id, "Knowledge base")
    return {"results": knowledge.search(ctx.db, ctx.org_id, [kb.id], body.query, body.limit)}
