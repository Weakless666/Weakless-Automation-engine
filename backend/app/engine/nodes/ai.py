"""AI nodes. All model access goes through the provider abstraction in ``app.ai``."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Literal

from pydantic import Field

from app.core.errors import NodeError
from app.engine.registry import NodeConfig, NodeContext, NodeResult, Wait, node


class AIConfig(NodeConfig):
    provider: Literal["", "anthropic", "openai", "gemini", "openai_compatible"] = Field("", description="Empty = organization default")
    model: str = Field("", description="Empty = organization default (claude-opus-5-5 unless changed)")
    integration_id: str | None = Field(None, description="Specific AI integration (optional)")
    max_tokens: int = Field(16000, ge=256, le=64000)


def _llm(ctx: NodeContext, cfg: AIConfig, *, system: str, prompt: str, json_schema: dict | None = None, tools: list[dict] | None = None):
    from app.ai.service import complete

    return complete(
        ctx.db, ctx.organization_id, provider=cfg.provider or None, model=cfg.model or None, system=system,
        messages=[{"role": "user", "text": prompt}], json_schema=json_schema, tools=tools, max_tokens=cfg.max_tokens,
        integration_id=cfg.integration_id or None, workflow_execution_id=ctx.execution.id, workflow_id=ctx.execution.workflow_id,
        secrets_out=ctx.secrets)


def _usage(r) -> dict:
    return {"provider": r.provider, "model": r.model, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens}


def _json(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise NodeError("The model did not return valid JSON", code="ai_invalid_output", retryable=True)
        try:
            data = json.loads(m.group(0))
        except ValueError as exc:
            raise NodeError("The model did not return valid JSON", code="ai_invalid_output", retryable=True) from exc
    if not isinstance(data, dict):
        raise NodeError("The model did not return a JSON object", code="ai_invalid_output", retryable=True)
    return data


def _schema(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_TEXT_OUT = {"text": {"type": "string"}, "model": {"type": "string"}, "input_tokens": {"type": "integer"}, "output_tokens": {"type": "integer"}}


class CompletionConfig(AIConfig):
    system: str = Field("", description="System instructions")
    prompt: str = Field(..., min_length=1)


def _completion(ctx: NodeContext, cfg: CompletionConfig) -> NodeResult:
    r = _llm(ctx, cfg, system=cfg.system, prompt=cfg.prompt)
    return NodeResult(output={"text": r.text, **_usage(r)})


node("ai.completion", "ai", "LLM Completion", "Send a prompt to a language model and use the reply.", CompletionConfig,
     output_schema=_TEXT_OUT, icon="sparkles")(_completion)
node("action.ai_response", "action", "Generate AI Response", "Generate a reply with AI, e.g. an answer to a customer message.", CompletionConfig,
     output_schema=_TEXT_OUT, icon="message-square-text")(_completion)


class GenerateImageConfig(NodeConfig):
    prompt: str = Field(..., min_length=3, max_length=8000, description="What the image should show, e.g. built from {{ trigger.data.media_notes }}")
    aspect: Literal["1:1", "4:5", "16:9", "9:16"] = Field("1:1", description="Square, portrait (feed), landscape or vertical (stories)")
    provider: Literal["", "openai", "gemini", "openai_compatible"] = Field("", description="Empty = the first connected provider that makes images")
    model: str = Field("", max_length=120, description="Empty = the provider's default image model")


@node("ai.generate_image", "ai", "Generate Image", "Create an image with AI and store it as a file you can post, attach or upload.",
      GenerateImageConfig, idempotent=False, output_schema={"file": {"type": "object"}, "model": {"type": "string"}}, icon="image")
def generate_image(ctx: NodeContext, cfg: GenerateImageConfig) -> NodeResult:
    from app.services.studio import generate_image_now

    return NodeResult(output=generate_image_now(ctx.db, ctx.organization_id, prompt=cfg.prompt, provider=cfg.provider or None, model=cfg.model or None,
                                                aspect=cfg.aspect, execution_id=ctx.execution.id, secrets_out=ctx.secrets))


class AgentNodeConfig(NodeConfig):
    agent_id: str = Field(..., description="AI agent to run")
    input: str = Field(..., min_length=1, description="Message for the agent")
    conversation_ref: str | None = Field(None, description="Stable id (e.g. customer phone) to keep one conversation per contact")


@node("ai.agent", "ai", "AI Agent", "Run one of the organization's AI agents, including its tools and knowledge.", AgentNodeConfig,
      idempotent=False, output_schema={**_TEXT_OUT, "agent_execution_id": {"type": "string"}, "conversation_id": {"type": "string"}}, icon="bot")
def agent_node(ctx: NodeContext, cfg: AgentNodeConfig) -> NodeResult:
    from app.ai.agents import agent_output, run_agent
    from app.models import AIAgent

    try:
        agent = ctx.db.get(AIAgent, uuid.UUID(cfg.agent_id))
    except ValueError:
        agent = None
    if not agent or agent.organization_id != ctx.organization_id or agent.deleted_at:
        raise NodeError("Agent not found in this organization", code="agent_not_found")
    aexec = run_agent(ctx.db, agent, cfg.input, external_ref=cfg.conversation_ref or None,
                      workflow_execution_id=ctx.execution.id, workflow_node_key=None if ctx.in_loop else ctx.node_id)
    if aexec.status == "WAITING_APPROVAL":
        if ctx.in_loop:
            raise NodeError("An agent that needs human approval cannot run inside a loop", code="invalid_config")
        return NodeResult(output={"agent_execution_id": str(aexec.id), "status": aexec.status},
                          wait=Wait(kind="agent", data={"agent_execution_id": str(aexec.id)}))
    if aexec.status != "COMPLETED":
        raise NodeError(aexec.error or "Agent failed", code="agent_failed")
    return NodeResult(output=agent_output(aexec))


class ClassifyConfig(AIConfig):
    text: str = Field(..., min_length=1)
    categories: list[str] = Field(default_factory=list, min_length=2)
    instructions: str = ""


@node("ai.classify", "ai", "AI Classification", "Assign text to exactly one of your categories.", ClassifyConfig,
      output_schema={"label": {"type": "string"}, "confidence": {"type": "number"}, "reason": {"type": "string"}}, icon="tags")
def classify_node(ctx: NodeContext, cfg: ClassifyConfig) -> NodeResult:
    schema = _schema({"label": {"type": "string", "enum": cfg.categories}, "confidence": {"type": "number"}, "reason": {"type": "string"}})
    system = ("Classify the text into exactly one of the allowed categories. Respond with JSON only: "
              '{"label": <category>, "confidence": <0..1>, "reason": <short>}. ' + cfg.instructions)
    r = _llm(ctx, cfg, system=system, prompt=f"Categories: {json.dumps(cfg.categories)}\n\nText:\n{cfg.text}", json_schema=schema)
    data = _json(r.text)
    label = next((c for c in cfg.categories if c.lower() == str(data.get("label", "")).strip().lower()), None)
    if label is None:
        raise NodeError("The model returned a category that is not in the list", code="ai_invalid_output", retryable=True)
    return NodeResult(output={"label": label, "confidence": data.get("confidence"), "reason": data.get("reason", ""), **_usage(r)})


class ExtractField(NodeConfig):
    name: str = Field(..., pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    type: Literal["string", "number", "boolean", "list"] = "string"
    description: str = ""


class ExtractConfig(AIConfig):
    text: str = Field(..., min_length=1)
    fields: list[ExtractField] = Field(default_factory=list, min_length=1)
    instructions: str = ""


@node("ai.extract", "ai", "AI Extraction", "Pull structured fields out of free text.", ExtractConfig,
      output_schema={"data": {"type": "object"}}, icon="scan-text")
def extract_node(ctx: NodeContext, cfg: ExtractConfig) -> NodeResult:
    types = {"string": {"type": ["string", "null"]}, "number": {"type": ["number", "null"]}, "boolean": {"type": ["boolean", "null"]},
             "list": {"type": "array", "items": {"type": "string"}}}
    props = {f.name: {**types[f.type], **({"description": f.description} if f.description else {})} for f in cfg.fields}
    system = ("Extract the requested fields from the text. Use null when a value is not present; never invent values. "
              "Respond with a JSON object containing exactly the requested fields. " + cfg.instructions)
    r = _llm(ctx, cfg, system=system, prompt=f"Fields: {json.dumps([f.model_dump() for f in cfg.fields])}\n\nText:\n{cfg.text}", json_schema=_schema(props))
    data = _json(r.text)
    return NodeResult(output={"data": {f.name: data.get(f.name) for f in cfg.fields}, **_usage(r)})


class SummarizeConfig(AIConfig):
    text: str = Field(..., min_length=1)
    max_words: int = Field(120, ge=10, le=2000)
    style: Literal["paragraph", "bullets"] = "paragraph"
    instructions: str = ""


@node("ai.summarize", "ai", "AI Summarization", "Summarize text.", SummarizeConfig, output_schema=_TEXT_OUT, icon="align-left")
def summarize_node(ctx: NodeContext, cfg: SummarizeConfig) -> NodeResult:
    system = (f"Summarize the user's text in at most {cfg.max_words} words as {'bullet points' if cfg.style == 'bullets' else 'one paragraph'}. "
              f"Output only the summary. {cfg.instructions}")
    r = _llm(ctx, cfg, system=system, prompt=cfg.text)
    return NodeResult(output={"text": r.text.strip(), **_usage(r)})


class TranslateConfig(AIConfig):
    text: str = Field(..., min_length=1)
    target_language: str = Field(..., min_length=2)
    instructions: str = ""


@node("ai.translate", "ai", "AI Translation", "Translate text into another language.", TranslateConfig, output_schema=_TEXT_OUT, icon="languages")
def translate_node(ctx: NodeContext, cfg: TranslateConfig) -> NodeResult:
    system = f"Translate the user's text into {cfg.target_language}. Preserve meaning, tone and formatting. Output only the translation. {cfg.instructions}"
    r = _llm(ctx, cfg, system=system, prompt=cfg.text)
    return NodeResult(output={"text": r.text.strip(), "target_language": cfg.target_language, **_usage(r)})


class DecisionConfig(AIConfig):
    question: str = Field(..., min_length=1, description="What should be decided?")
    context: str = Field("", description="Information the decision is based on")
    options: list[str] = Field(default_factory=list, min_length=2, description="One output per option")


@node("ai.decision", "ai", "AI Decision", "Let AI choose one of your options and follow that branch.", DecisionConfig,
      dynamic_handles={"field": "options", "key": None, "extra": []},
      output_schema={"choice": {"type": "string"}, "reason": {"type": "string"}}, icon="git-fork")
def decision_node(ctx: NodeContext, cfg: DecisionConfig) -> NodeResult:
    schema = _schema({"choice": {"type": "string", "enum": cfg.options}, "reason": {"type": "string"}})
    system = 'Decide between the allowed options. Respond with JSON only: {"choice": <option>, "reason": <short>}.'
    r = _llm(ctx, cfg, system=system, prompt=f"Question: {cfg.question}\nOptions: {json.dumps(cfg.options)}\n\nContext:\n{cfg.context}", json_schema=schema)
    data = _json(r.text)
    choice = next((o for o in cfg.options if o.lower() == str(data.get("choice", "")).strip().lower()), None)
    if choice is None:
        raise NodeError("The model chose an option that is not in the list", code="ai_invalid_output", retryable=True)
    return NodeResult(output={"choice": choice, "reason": data.get("reason", ""), **_usage(r)}, handles=[choice])


class ToolCallConfig(AIConfig):
    instruction: str = Field(..., min_length=1, description="What the model should accomplish with the tool")
    tool_key: str = Field(..., description="Tool from the controlled registry")
    tool_config: dict[str, Any] = Field(default_factory=dict, description="Scoped tool configuration (integration, allowed domains, ...)")


@node("ai.tool_call", "ai", "AI Tool Calling", "Let AI fill in the arguments of one controlled tool and run it.", ToolCallConfig,
      idempotent=False, high_impact=True,
      output_schema={"called": {"type": "boolean"}, "input": {"type": "object"}, "result": {}, "text": {"type": "string"}}, icon="wrench")
def tool_call_node(ctx: NodeContext, cfg: ToolCallConfig) -> NodeResult:
    from app.ai.tools import TOOLS, ToolContext, tool_definition_for_model
    from app.core.errors import AppError

    tool = TOOLS.get(cfg.tool_key)
    if not tool:
        raise NodeError(f"Unknown tool '{cfg.tool_key}'", code="invalid_config")
    try:
        tool.validate_config(cfg.tool_config)
    except AppError as exc:
        raise NodeError(exc.message, code="invalid_config") from exc
    definition = tool_definition_for_model(cfg.tool_key, cfg.tool_key, cfg.tool_config)
    r = _llm(ctx, cfg, system=f"Use the '{cfg.tool_key}' tool exactly once to accomplish the task. If it cannot be done with this tool, say why.",
             prompt=cfg.instruction, tools=[definition])
    call = next((c for c in r.tool_calls if c["name"] == cfg.tool_key), None)
    if not call:
        return NodeResult(output={"called": False, "text": r.text, **_usage(r)})
    tc = ToolContext(db=ctx.db, organization_id=ctx.organization_id, secrets=ctx.secrets)
    result = tool.run(tc, cfg.tool_config, call.get("input") or {})
    return NodeResult(output={"called": True, "input": call.get("input") or {}, "result": result, "text": r.text, **_usage(r)})


class KbSearchConfig(NodeConfig):
    knowledge_base_ids: list[str] = Field(default_factory=list, min_length=1)
    query: str = Field(..., min_length=1)
    limit: int = Field(5, ge=1, le=20)


@node("ai.kb_search", "ai", "Knowledge Base Search", "Find the most relevant passages in a knowledge base.", KbSearchConfig,
      output_schema={"results": {"type": "array"}, "context": {"type": "string"}}, icon="book-open")
def kb_search_node(ctx: NodeContext, cfg: KbSearchConfig) -> NodeResult:
    from app.ai.knowledge import search

    results = search(ctx.db, ctx.organization_id, cfg.knowledge_base_ids, cfg.query, cfg.limit)
    return NodeResult(output={"results": results, "count": len(results), "context": "\n\n".join(r["content"] for r in results)})
