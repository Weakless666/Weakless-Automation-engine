"""AI agent engine: conversation state, tool-calling loop, limits and human approval.

The loop is a persisted state machine. Tool calls requested by the model are
stored in ``ai_agent_executions.pending`` and processed one by one; when a tool
requires approval the execution stops in ``WAITING_APPROVAL`` and is resumed by
the queue worker after a human decides. A manual loop (rather than an SDK tool
runner) is used on purpose: it is provider-neutral and must survive restarts.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.ai import service as ai_service
from app.ai.tools import TOOLS, ToolContext, tool_definition_for_model
from app.core.errors import AppError, NodeError
from app.core.redact import redact, truncate
from app.db import utcnow
from app.models import AIAgent, AIAgentExecution, AIAgentTool, AIConversation, AIMessage, ApprovalRequest

log = logging.getLogger("wae.agents")
HISTORY_MESSAGES = 40
APPROVAL_TTL_HOURS = 72

AGENT_TYPES = {
    "customer_support": "Customer Support Agent", "sales": "Sales Agent", "marketing": "Marketing Agent",
    "research": "Research Agent", "operations": "Operations Agent", "receptionist": "Receptionist Agent",
    "booking": "Booking Agent", "finance": "Finance Assistant", "document_processing": "Document Processing Agent",
    "custom": "Custom Business Agent",
}


def _history(db: Session, conv: AIConversation) -> list[dict]:
    rows = db.scalars(select(AIMessage).where(AIMessage.conversation_id == conv.id).order_by(AIMessage.sequence.desc()).limit(HISTORY_MESSAGES)).all()
    msgs = [{"role": r.role, **(r.content or {})} for r in reversed(rows)]
    while msgs and msgs[0]["role"] != "user":  # a window must not start mid tool-exchange
        msgs.pop(0)
    return msgs


def _append(db: Session, conv: AIConversation, role: str, content: dict) -> None:
    seq = (db.scalar(select(func.max(AIMessage.sequence)).where(AIMessage.conversation_id == conv.id)) or 0) + 1
    db.add(AIMessage(organization_id=conv.organization_id, conversation_id=conv.id, sequence=seq, role=role, content=content))
    db.flush()


def _check_limits(db: Session, agent: AIAgent) -> None:
    limits = agent.limits or {}
    since = utcnow() - timedelta(days=1)
    if limits.get("max_executions_per_day"):
        n = db.scalar(select(func.count()).select_from(AIAgentExecution).where(
            AIAgentExecution.agent_id == agent.id, AIAgentExecution.created_at >= since)) or 0
        if n >= int(limits["max_executions_per_day"]):
            raise NodeError("Agent reached its daily execution limit", code="agent_limit_reached")
    if limits.get("max_tokens_per_day"):
        used = db.scalar(select(func.coalesce(func.sum(AIAgentExecution.input_tokens + AIAgentExecution.output_tokens), 0)).where(
            AIAgentExecution.agent_id == agent.id, AIAgentExecution.created_at >= since)) or 0
        if used >= int(limits["max_tokens_per_day"]):
            raise NodeError("Agent reached its daily token limit", code="agent_limit_reached")


def _tools(db: Session, agent: AIAgent) -> dict[str, AIAgentTool]:
    return {t.name: t for t in db.scalars(select(AIAgentTool).where(AIAgentTool.agent_id == agent.id)) if t.tool_key in TOOLS}


def get_or_create_conversation(db: Session, agent: AIAgent, conversation_id=None, external_ref: str | None = None) -> AIConversation:
    conv = None
    if conversation_id:
        conv = db.get(AIConversation, conversation_id if isinstance(conversation_id, uuid.UUID) else uuid.UUID(str(conversation_id)))
        if not conv or conv.agent_id != agent.id or conv.organization_id != agent.organization_id:
            raise NodeError("Conversation not found", code="not_found")
    elif external_ref:
        conv = db.scalar(select(AIConversation).where(AIConversation.agent_id == agent.id, AIConversation.external_ref == external_ref,
                                                      AIConversation.status == "open"))
    if not conv:
        conv = AIConversation(organization_id=agent.organization_id, agent_id=agent.id, external_ref=external_ref)
        db.add(conv)
        db.flush()
    return conv


def run_agent(db: Session, agent: AIAgent, user_input: str, *, conversation_id=None, external_ref: str | None = None,
              workflow_execution_id: uuid.UUID | None = None, workflow_node_key: str | None = None,
              parent_execution_id: uuid.UUID | None = None) -> AIAgentExecution:
    """Run the agent on one user message. Returns the execution (COMPLETED, FAILED or WAITING_APPROVAL).
    ``parent_execution_id`` marks a task delegated by another agent (see the delegate_to_agent tool)."""
    if agent.status != "active" or agent.deleted_at:
        raise NodeError("Agent is disabled", code="agent_disabled")
    _check_limits(db, agent)
    conv = get_or_create_conversation(db, agent, conversation_id, external_ref)
    if not conv.title:
        conv.title = user_input[:80]
    aexec = AIAgentExecution(organization_id=agent.organization_id, agent_id=agent.id, conversation_id=conv.id,
                             workflow_execution_id=workflow_execution_id, workflow_node_key=workflow_node_key,
                             parent_execution_id=parent_execution_id,
                             kind="agent", status="RUNNING", provider=agent.provider, model=agent.model)
    db.add(aexec)
    _append(db, conv, "user", {"text": user_input})
    db.flush()
    return _drive(db, agent, aexec, conv)


def _system_prompt(agent: AIAgent) -> str:
    parts = [agent.system_instructions.strip() or f"You are {agent.name}, an assistant for a business."]
    if agent.team_lead:
        parts.append("You lead a team. Give each specialist one clear task with the context they need, combine their results, "
                     "and do the coordinating yourself. Do not ask a specialist to do another specialist's job.")
    parts.append("Only use the tools provided. If a tool call is rejected or fails, explain what happened instead of retrying blindly.")
    return "\n\n".join(parts)


def _finish(db: Session, aexec: AIAgentExecution, status: str, *, text: str | None = None, error: str | None = None) -> AIAgentExecution:
    aexec.status, aexec.finished_at, aexec.pending = status, utcnow(), None
    if text is not None:
        aexec.output_text = text
    if error:
        aexec.error = error[:1000]
    db.flush()
    # A workflow node paused on this agent: wake it up with the result.
    if aexec.workflow_execution_id and aexec.workflow_node_key and (aexec.trace or []) and any(t.get("type") == "paused" for t in aexec.trace):
        from app.engine.queue import enqueue

        ok = status == "COMPLETED"
        enqueue(db, aexec.organization_id, aexec.workflow_execution_id, payload={
            "resume_node": aexec.workflow_node_key,
            "output": agent_output(aexec) if ok else {"error": {"code": "agent_failed", "message": aexec.error or "Agent failed"}},
            "handles": ["out"] if ok else ["error"],
        })
    return aexec


def agent_output(aexec: AIAgentExecution) -> dict:
    return {"text": aexec.output_text or "", "agent_execution_id": str(aexec.id), "conversation_id": str(aexec.conversation_id),
            "status": aexec.status, "input_tokens": aexec.input_tokens, "output_tokens": aexec.output_tokens,
            "cost_usd": float(aexec.cost_usd) if aexec.cost_usd is not None else None}


def _drive(db: Session, agent: AIAgent, aexec: AIAgentExecution, conv: AIConversation) -> AIAgentExecution:
    tools = _tools(db, agent)
    tool_defs = [tool_definition_for_model(name, t.tool_key, t.config or {}) for name, t in tools.items()]
    tc = ToolContext(db=db, organization_id=agent.organization_id, agent_id=agent.id, execution_id=aexec.id)
    while True:
        pending = aexec.pending
        if pending:
            calls, results, decisions = pending["calls"], pending["results"], pending.get("decisions", {})
            while len(results) < len(calls):
                call = calls[len(results)]
                tool = tools.get(call["name"])
                decision = decisions.get(call["id"])
                if tool is None:
                    res = {"content": f"Tool '{call['name']}' is not available to this agent.", "is_error": True}
                elif tool.requires_approval and decision is None:
                    approval = _request_approval(db, agent, aexec, tool, call)
                    pending["awaiting"] = {"call_id": call["id"], "approval_id": str(approval.id)}
                    aexec.pending, aexec.status = pending, "WAITING_APPROVAL"
                    aexec.trace = [*(aexec.trace or []), {"type": "paused", "tool": call["name"], "approval_id": str(approval.id)}]
                    flag_modified(aexec, "pending")
                    db.flush()
                    return aexec
                elif decision is not None and not decision.get("approved"):
                    res = {"content": f"A human reviewer rejected this action. Note: {decision.get('note') or 'none'}", "is_error": True}
                else:
                    res = _execute_tool(tc, tool, call)
                results.append({"tool_call_id": call["id"], "name": call["name"], **res})
                aexec.trace = [*(aexec.trace or []), {"type": "tool", "name": call["name"], "tool_key": tool.tool_key if tool else None,
                                                       "input": truncate(redact(call.get("input"), tc.secrets), 4000),
                                                       "is_error": res.get("is_error", False),
                                                       "approved_by_human": bool(decision and decision.get("approved"))}]
                aexec.pending = {**pending, "results": results}
                flag_modified(aexec, "pending")
                db.flush()
            _append(db, conv, "tool", {"tool_results": results})
            aexec.pending = None
            db.flush()
        if aexec.iterations >= agent.max_iterations:
            return _finish(db, aexec, "FAILED", error=f"Agent stopped after {agent.max_iterations} iterations without a final answer")
        try:
            result = ai_service.complete(
                db, agent.organization_id, provider=agent.provider, model=agent.model, system=_system_prompt(agent),
                messages=_history(db, conv), tools=tool_defs, max_tokens=agent.max_tokens, temperature=agent.temperature,
                options=agent.provider_options or {}, fallbacks=agent.fallbacks or [], agent_execution=aexec,
                workflow_execution_id=aexec.workflow_execution_id, secrets_out=tc.secrets)
        except NodeError as exc:
            return _finish(db, aexec, "FAILED", error=exc.message)
        aexec.iterations += 1
        _append(db, conv, "assistant", result.assistant_message())
        if result.tool_calls:
            aexec.pending = {"calls": result.tool_calls, "results": [], "decisions": {}}
            db.flush()
            continue
        if result.stop_reason in ("max_tokens", "length", "MAX_TOKENS") and not result.text:
            return _finish(db, aexec, "FAILED", error="The model ran out of output tokens before answering. Increase the agent's token limit.")
        return _finish(db, aexec, "COMPLETED", text=result.text)


def _execute_tool(tc: ToolContext, tool: AIAgentTool, call: dict) -> dict:
    try:
        out = TOOLS[tool.tool_key].run(tc, tool.config or {}, call.get("input") or {})
        if isinstance(out, dict) and "is_error" in out and "content" in out:  # MCP result shape
            return {"content": str(redact(out["content"], tc.secrets))[:20000], "is_error": bool(out["is_error"])}
        return {"content": json.dumps(truncate(redact(out, tc.secrets), 20000), default=str), "is_error": False}
    except (NodeError, AppError) as exc:
        return {"content": f"Tool failed: {exc.message}", "is_error": True}
    except Exception as exc:  # noqa: BLE001
        log.exception("agent tool %s crashed", tool.tool_key)
        return {"content": f"Tool failed unexpectedly ({type(exc).__name__})", "is_error": True}


def _request_approval(db: Session, agent: AIAgent, aexec: AIAgentExecution, tool: AIAgentTool, call: dict) -> ApprovalRequest:
    from app.services.approvals import create_approval

    return create_approval(
        db, agent.organization_id, kind="agent_tool", title=f"{agent.name} wants to use '{tool.name}'",
        summary=json.dumps(redact(call.get("input") or {}), indent=2, default=str)[:4000],
        payload={"agent_id": str(agent.id), "tool": tool.name, "tool_key": tool.tool_key, "input": redact(call.get("input") or {})},
        agent_execution_id=aexec.id, expires_at=utcnow() + timedelta(hours=APPROVAL_TTL_HOURS))


def resume_after_approval(db: Session, payload: dict) -> str:
    """Queue handler: continue an agent execution once its approval was decided or expired."""
    aexec = db.get(AIAgentExecution, uuid.UUID(payload["agent_execution_id"]))
    if not aexec or aexec.status != "WAITING_APPROVAL" or not aexec.pending:
        return "skipped"
    awaiting = (aexec.pending or {}).get("awaiting") or {}
    if awaiting.get("approval_id") != payload.get("approval_id"):
        return "skipped"
    approval = db.get(ApprovalRequest, uuid.UUID(payload["approval_id"]))
    if not approval or approval.status == "PENDING":
        return "skipped"
    agent = db.get(AIAgent, aexec.agent_id) if aexec.agent_id else None
    conv = db.get(AIConversation, aexec.conversation_id) if aexec.conversation_id else None
    if not agent or not conv:
        _finish(db, aexec, "FAILED", error="Agent or conversation no longer exists")
        db.commit()
        return "FAILED"
    pending = dict(aexec.pending)
    note = approval.decision_note if approval.status != "EXPIRED" else "the approval request expired"
    pending.setdefault("decisions", {})[awaiting["call_id"]] = {"approved": approval.status == "APPROVED", "note": note}
    pending.pop("awaiting", None)
    aexec.pending, aexec.status = pending, "RUNNING"
    flag_modified(aexec, "pending")
    db.flush()
    _drive(db, agent, aexec, conv)
    db.commit()
    return aexec.status
