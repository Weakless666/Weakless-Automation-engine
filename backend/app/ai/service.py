"""Provider resolution, fallback, usage metering and cost estimation for LLM calls."""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.providers import LLMRequest, LLMResult, get_provider
from app.config import get_settings
from app.core.errors import NodeError, QuotaExceeded
from app.db import utcnow
from app.models import AIAgentExecution, Integration, SystemSetting
from app.services.usage import check_quota, record_usage

log = logging.getLogger("wae.ai")

# USD per million tokens. Anthropic list prices as of 2026-10; other providers are
# intentionally unpriced until an administrator enters their contract prices
# (Settings > AI pricing). Unpriced models report cost as unknown, never as zero.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}
FALLBACK_CODES = {"ai_not_configured", "auth_failed", "ai_rate_limited", "ai_provider_error", "network_error", "timeout", "ai_model_not_found"}


def get_setting(db: Session, key: str, default=None):
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    return row.value if row else default


def model_pricing(db: Session) -> dict:
    return {**DEFAULT_PRICING, **(get_setting(db, "ai.model_pricing", {}) or {})}


def estimate_cost(db: Session, model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    price = model_pricing(db).get(model)
    if not price:
        return None
    cost = Decimal(str(price.get("input", 0))) * input_tokens / Decimal(1_000_000) + Decimal(str(price.get("output", 0))) * output_tokens / Decimal(1_000_000)
    return cost.quantize(Decimal("0.000001"))


def resolve_credentials(db: Session, organization_id: uuid.UUID, provider: str, integration_id=None) -> tuple[dict, dict]:
    """(secret, config) for a provider: explicit integration > the organization's
    connected integration > platform-level key from the environment."""
    from app.integrations.service import connection, get_integration

    if integration_id:
        integration = get_integration(db, organization_id, integration_id)
        if integration.provider_key != provider:
            raise NodeError("Integration does not match the selected AI provider", code="invalid_config")
        conn = connection(db, integration)
        return conn.secret, conn.config
    integration = db.scalar(select(Integration).where(
        Integration.organization_id == organization_id, Integration.provider_key == provider,
        Integration.status == "connected", Integration.deleted_at.is_(None)).order_by(Integration.created_at))
    if integration:
        conn = connection(db, integration)
        return conn.secret, conn.config
    s = get_settings()
    platform_key = {"anthropic": s.anthropic_api_key, "openai": s.openai_api_key, "gemini": s.gemini_api_key}.get(provider, "")
    if platform_key:
        return {"api_key": platform_key}, {}
    from app.ai import providers as _p

    if provider == "anthropic" and _p.AnthropicProvider.client_factory:
        return {}, {}
    if provider not in ("anthropic", "openai", "gemini", "openai_compatible"):
        return {}, {}  # custom registered provider manages its own credentials
    raise NodeError(f"No credentials for AI provider '{provider}'. Connect it under Integrations.", code="ai_not_configured")


def default_model(db: Session, organization_id: uuid.UUID) -> tuple[str, str]:
    from app.models import Organization

    org = db.get(Organization, organization_id)
    cfg = ((org.settings or {}) if org else {}).get("ai") or get_setting(db, "ai.default", {}) or {}
    return cfg.get("provider") or "anthropic", cfg.get("model") or "claude-opus-5-5"


def complete(
    db: Session,
    organization_id: uuid.UUID,
    *,
    provider: str | None = None,
    model: str | None = None,
    messages: list[dict],
    system: str = "",
    tools: list[dict] | None = None,
    json_schema: dict | None = None,
    max_tokens: int = 16000,
    temperature: float | None = None,
    options: dict | None = None,
    fallbacks: list[dict] | None = None,
    integration_id=None,
    workflow_execution_id: uuid.UUID | None = None,
    workflow_id: uuid.UUID | None = None,
    agent_execution: AIAgentExecution | None = None,
    secrets_out: set[str] | None = None,
) -> LLMResult:
    """Run one LLM request with provider fallback, metering and cost tracking."""
    try:
        check_quota(db, organization_id, "ai_tokens_per_month")
    except QuotaExceeded as exc:
        raise NodeError(exc.message, code="quota_exceeded") from exc
    if not provider or not model:
        d_provider, d_model = default_model(db, organization_id)
        provider, model = provider or d_provider, model or d_model
    chain = [{"provider": provider, "model": model, "integration_id": integration_id}] + [
        {"provider": f.get("provider"), "model": f.get("model"), "integration_id": None} for f in (fallbacks or []) if f.get("provider") and f.get("model")]
    attempts: list[dict] = []
    last: NodeError | None = None
    for entry in chain:
        try:
            secret, config = resolve_credentials(db, organization_id, entry["provider"], entry["integration_id"])
            if secrets_out is not None:
                secrets_out.update(str(v) for v in secret.values() if isinstance(v, str) and len(v) >= 6)
            opts = dict(options or {})
            if entry["provider"] == "anthropic":
                opts["refusal_fallback"] = bool(get_setting(db, "ai.anthropic_refusal_fallback", True))
            req = LLMRequest(model=entry["model"], messages=messages, system=system, tools=tools or [], json_schema=json_schema,
                             max_tokens=max_tokens, temperature=temperature, options=opts)
            result = get_provider(entry["provider"]).complete(req, secret, config)
        except NodeError as exc:
            attempts.append({"provider": entry["provider"], "model": entry["model"], "error": exc.code})
            last = exc
            if exc.code in FALLBACK_CODES:
                continue
            break
        attempts.append({"provider": entry["provider"], "model": entry["model"], "ok": True})
        cost = estimate_cost(db, entry["model"], result.input_tokens, result.output_tokens)
        meta = {"provider": entry["provider"], "model": entry["model"]}
        record_usage(db, organization_id, "ai_requests", 1, workflow_id=workflow_id, execution_id=workflow_execution_id, meta=meta)
        record_usage(db, organization_id, "ai_input_tokens", result.input_tokens, workflow_id=workflow_id, execution_id=workflow_execution_id, meta=meta)
        record_usage(db, organization_id, "ai_output_tokens", result.output_tokens, workflow_id=workflow_id, execution_id=workflow_execution_id, meta=meta)
        if cost:
            record_usage(db, organization_id, "ai_cost_usd", cost, workflow_id=workflow_id, execution_id=workflow_execution_id, meta=meta)
        if agent_execution is not None:
            agent_execution.provider, agent_execution.model = entry["provider"], entry["model"]
            agent_execution.input_tokens += result.input_tokens
            agent_execution.output_tokens += result.output_tokens
            if cost is not None:
                agent_execution.cost_usd = (agent_execution.cost_usd or Decimal(0)) + cost
            agent_execution.trace = [*(agent_execution.trace or []), {"type": "llm", "attempts": attempts, "stop_reason": result.stop_reason,
                                                                       "input_tokens": result.input_tokens, "output_tokens": result.output_tokens}]
        else:
            db.add(AIAgentExecution(
                organization_id=organization_id, workflow_execution_id=workflow_execution_id, kind="completion", status="COMPLETED",
                provider=entry["provider"], model=entry["model"], input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                cost_usd=cost, iterations=1, output_text=result.text[:20000], trace=[{"type": "llm", "attempts": attempts}], finished_at=utcnow()))
        db.flush()
        return result
    assert last is not None
    if agent_execution is not None:
        agent_execution.trace = [*(agent_execution.trace or []), {"type": "llm", "attempts": attempts}]
    else:
        db.add(AIAgentExecution(organization_id=organization_id, workflow_execution_id=workflow_execution_id, kind="completion",
                                status="FAILED", provider=provider, model=model, error=last.message[:1000],
                                trace=[{"type": "llm", "attempts": attempts}], finished_at=utcnow()))
        db.flush()
    raise last
