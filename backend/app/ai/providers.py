"""AI provider abstraction.

The rest of the application speaks one normalized format and never imports a
vendor SDK directly:

* messages: ``{"role": "user"|"assistant"|"tool", "text": str,
  "tool_calls": [{"id", "name", "input"}], "tool_results": [{"tool_call_id",
  "name", "content", "is_error"}], "native": {...}}``
* tools: ``{"name", "description", "input_schema"}``

Providers: Anthropic Claude (official SDK), OpenAI, any OpenAI-compatible
server (including local inference servers), and Google Gemini.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core import http
from app.core.errors import NodeError

# Models that accept the server-side refusal fallback parameter.
ANTHROPIC_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
ANTHROPIC_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    provider: str = ""
    model: str = ""
    native: dict | None = None  # provider-native assistant turn, replayed verbatim on the next request

    def assistant_message(self) -> dict:
        msg: dict = {"role": "assistant", "text": self.text, "tool_calls": self.tool_calls}
        if self.native:
            msg["native"] = self.native
        return msg


@dataclass
class LLMRequest:
    model: str
    messages: list[dict]
    system: str = ""
    tools: list[dict] = field(default_factory=list)
    json_schema: dict | None = None
    max_tokens: int = 16000
    temperature: float | None = None
    options: dict = field(default_factory=dict)
    timeout: float = 180.0


class Provider:
    key = ""

    def complete(self, req: LLMRequest, secret: dict, config: dict) -> LLMResult:  # pragma: no cover - interface
        raise NotImplementedError

    def test(self, secret: dict, config: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


# ----------------------------------------------------------------------------- Anthropic


class AnthropicProvider(Provider):
    key = "anthropic"
    client_factory = None  # tests replace this with a fake client factory
    refusal_fallback = True

    def _client(self, secret: dict, timeout: float = 180.0):
        if self.client_factory:
            return self.client_factory(secret)
        import anthropic

        return anthropic.Anthropic(api_key=secret.get("api_key"), timeout=timeout, max_retries=2)

    @staticmethod
    def _messages(messages: list[dict], model: str) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m.get("text") or ""})
            elif m["role"] == "assistant":
                native = m.get("native") or {}
                # Replay the provider's own blocks (thinking included) unchanged when the same model continues.
                if native.get("provider") == "anthropic" and native.get("model") == model and native.get("content"):
                    out.append({"role": "assistant", "content": native["content"]})
                    continue
                blocks: list[dict] = []
                if m.get("text"):
                    blocks.append({"type": "text", "text": m["text"]})
                for tc in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc.get("input") or {}})
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                # All results of one assistant turn go back in a single user message.
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["tool_call_id"], "content": _as_text(r.get("content")),
                     **({"is_error": True} if r.get("is_error") else {})}
                    for r in m.get("tool_results") or []
                ]})
        return out

    def complete(self, req: LLMRequest, secret: dict, config: dict) -> LLMResult:
        import anthropic

        if not secret.get("api_key") and not self.client_factory:
            raise NodeError("Anthropic API key is not configured", code="ai_not_configured")
        client = self._client(secret, req.timeout)
        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_tokens": min(req.max_tokens, 16000),  # non-streaming: stay under SDK HTTP timeouts
            "messages": self._messages(req.messages, req.model),
        }
        if req.system:
            kwargs["system"] = req.system
        if req.tools:
            kwargs["tools"] = [{"name": t["name"], "description": t.get("description", ""), "input_schema": t["input_schema"]} for t in req.tools]
        output_config: dict = {}
        if req.options.get("effort"):
            output_config["effort"] = req.options["effort"]
        if req.json_schema:
            output_config["format"] = {"type": "json_schema", "schema": req.json_schema}
        if output_config:
            kwargs["output_config"] = output_config
        # Sampling parameters are intentionally not sent: current Claude models reject them.
        try:
            if self.refusal_fallback and req.options.get("refusal_fallback", True) and req.model in ANTHROPIC_FALLBACK_MODELS:
                # Server-side refusal fallback: a policy decline is re-run on Anthropic's recommended model.
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = client.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            raise NodeError(f"Anthropic rejected the request: {exc.message}"[:500], code="ai_bad_request") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise NodeError("Anthropic API key is invalid or lacks permission", code="auth_failed") from exc
        except anthropic.NotFoundError as exc:
            raise NodeError(f"Anthropic model '{req.model}' was not found", code="ai_model_not_found") from exc
        except anthropic.RateLimitError as exc:
            raise NodeError("Anthropic rate limit reached", code="ai_rate_limited", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise NodeError(f"Anthropic API error ({exc.status_code})", code="ai_provider_error", retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:
            raise NodeError("Could not reach the Anthropic API", code="network_error", retryable=True) from exc

        if resp.stop_reason == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            raise NodeError("The model declined this request", code="ai_refusal", details={"category": category})
        text, calls, native = [], [], []
        for block in resp.content:
            native.append(block.to_dict() if hasattr(block, "to_dict") else dict(block))
            if block.type == "text":
                text.append(block.text)
            elif block.type == "tool_use":
                calls.append({"id": block.id, "name": block.name, "input": dict(block.input or {})})
        usage = resp.usage
        input_tokens = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        return LLMResult(text="".join(text), tool_calls=calls, input_tokens=input_tokens, output_tokens=usage.output_tokens or 0,
                         stop_reason=resp.stop_reason or "", provider=self.key, model=resp.model or req.model,
                         native={"provider": "anthropic", "model": req.model, "content": native})

    def test(self, secret: dict, config: dict) -> dict:
        import anthropic

        try:
            page = self._client(secret, 20.0).models.list(limit=5)
            return {"account": "anthropic", "models": [m.id for m in page.data]}
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise NodeError("Anthropic API key is invalid or lacks permission", code="auth_failed") from exc
        except anthropic.APIStatusError as exc:
            raise NodeError(f"Anthropic API error ({exc.status_code})", code="ai_provider_error", retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:
            raise NodeError("Could not reach the Anthropic API", code="network_error", retryable=True) from exc


# ----------------------------------------------------------------------------- OpenAI / compatible


class OpenAIProvider(Provider):
    key = "openai"
    default_base = "https://api.openai.com/v1"
    max_tokens_param = "max_completion_tokens"
    allow_private = False

    def _base(self, config: dict) -> str:
        return self.default_base

    def _headers(self, secret: dict) -> dict:
        return {"Authorization": f"Bearer {secret['api_key']}"} if secret.get("api_key") else {}

    @staticmethod
    def _messages(req: LLMRequest) -> list[dict]:
        out: list[dict] = []
        if req.system:
            out.append({"role": "system", "content": req.system})
        for m in req.messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m.get("text") or ""})
            elif m["role"] == "assistant":
                msg: dict = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": tc["id"], "type": "function",
                                          "function": {"name": tc["name"], "arguments": json.dumps(tc.get("input") or {})}}
                                         for tc in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                for r in m.get("tool_results") or []:
                    out.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": _as_text(r.get("content"))})
        return out

    def complete(self, req: LLMRequest, secret: dict, config: dict) -> LLMResult:
        if self.key == "openai" and not secret.get("api_key"):
            raise NodeError("OpenAI API key is not configured", code="ai_not_configured")
        body: dict[str, Any] = {"model": req.model, "messages": self._messages(req), self.max_tokens_param: req.max_tokens}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                               "parameters": t["input_schema"]}} for t in req.tools]
        if req.json_schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "schema": req.json_schema, "strict": True}}
        resp = http.request("POST", self._base(config).rstrip("/") + "/chat/completions", json=body, headers=self._headers(secret),
                            timeout=req.timeout, allow_private=self.allow_private or None)
        _raise_ai(resp, self.key)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": fn.get("arguments")}
            calls.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}", "name": fn.get("name", ""), "input": args})
        usage = data.get("usage") or {}
        return LLMResult(text=msg.get("content") or "", tool_calls=calls, input_tokens=int(usage.get("prompt_tokens") or 0),
                         output_tokens=int(usage.get("completion_tokens") or 0), stop_reason=choice.get("finish_reason") or "",
                         provider=self.key, model=data.get("model") or req.model)

    def test(self, secret: dict, config: dict) -> dict:
        resp = http.request("GET", self._base(config).rstrip("/") + "/models", headers=self._headers(secret), timeout=20,
                            allow_private=self.allow_private or None)
        _raise_ai(resp, self.key)
        models = [m.get("id") for m in (resp.json().get("data") or [])][:20]
        return {"account": self._base(config), "models": models}


class OpenAICompatibleProvider(OpenAIProvider):
    key = "openai_compatible"
    max_tokens_param = "max_tokens"

    def _base(self, config: dict) -> str:
        base = str(config.get("base_url") or "")
        if not base:
            raise NodeError("Base URL is not configured for the OpenAI-compatible provider", code="ai_not_configured")
        return base


# ----------------------------------------------------------------------------- Gemini


def _gemini_schema(schema: Any) -> Any:
    """Gemini accepts an OpenAPI-style subset of JSON Schema."""
    if isinstance(schema, dict):
        return {k: _gemini_schema(v) for k, v in schema.items() if k not in ("additionalProperties", "$schema", "title", "default")}
    if isinstance(schema, list):
        return [_gemini_schema(v) for v in schema]
    return schema


class GeminiProvider(Provider):
    key = "gemini"
    base = "https://generativelanguage.googleapis.com/v1beta"

    @staticmethod
    def _contents(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "parts": [{"text": m.get("text") or ""}]})
            elif m["role"] == "assistant":
                native = m.get("native") or {}
                if native.get("provider") == "gemini" and native.get("parts"):
                    out.append({"role": "model", "parts": native["parts"]})
                    continue
                parts: list[dict] = [{"text": m["text"]}] if m.get("text") else []
                parts += [{"functionCall": {"name": tc["name"], "args": tc.get("input") or {}}} for tc in m.get("tool_calls") or []]
                if parts:
                    out.append({"role": "model", "parts": parts})
            elif m["role"] == "tool":
                out.append({"role": "user", "parts": [
                    {"functionResponse": {"name": r.get("name", ""), "response": {"result": r.get("content")}}}
                    for r in m.get("tool_results") or []]})
        return out

    def complete(self, req: LLMRequest, secret: dict, config: dict) -> LLMResult:
        if not secret.get("api_key"):
            raise NodeError("Gemini API key is not configured", code="ai_not_configured")
        gen: dict[str, Any] = {"maxOutputTokens": req.max_tokens}
        if req.temperature is not None:
            gen["temperature"] = req.temperature
        if req.json_schema:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = _gemini_schema(req.json_schema)
        body: dict[str, Any] = {"contents": self._contents(req.messages), "generationConfig": gen}
        if req.system:
            body["systemInstruction"] = {"parts": [{"text": req.system}]}
        if req.tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t["name"], "description": t.get("description", ""), "parameters": _gemini_schema(t["input_schema"])} for t in req.tools]}]
        model = req.model if req.model.startswith("models/") else f"models/{req.model}"
        resp = http.request("POST", f"{self.base}/{model}:generateContent", json=body,
                            headers={"x-goog-api-key": secret["api_key"]}, timeout=req.timeout)
        _raise_ai(resp, self.key)
        data = resp.json()
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
        calls = [{"id": f"call_{uuid.uuid4().hex[:12]}", "name": p["functionCall"].get("name", ""), "input": p["functionCall"].get("args") or {}}
                 for p in parts if "functionCall" in p]
        if not parts and (data.get("promptFeedback") or {}).get("blockReason"):
            raise NodeError("Gemini blocked this request", code="ai_refusal", details={"reason": data["promptFeedback"]["blockReason"]})
        usage = data.get("usageMetadata") or {}
        return LLMResult(text=text, tool_calls=calls, input_tokens=int(usage.get("promptTokenCount") or 0),
                         output_tokens=int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0),
                         stop_reason=cand.get("finishReason") or "", provider=self.key, model=req.model,
                         native={"provider": "gemini", "parts": parts})

    def test(self, secret: dict, config: dict) -> dict:
        resp = http.request("GET", f"{self.base}/models", params={"pageSize": 20}, headers={"x-goog-api-key": secret.get("api_key", "")}, timeout=20)
        _raise_ai(resp, self.key)
        return {"account": "gemini", "models": [m.get("name", "").replace("models/", "") for m in resp.json().get("models") or []]}


# ----------------------------------------------------------------------------- helpers


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, default=str)


def _raise_ai(resp, provider: str) -> None:
    if resp.status_code < 400:
        return
    try:
        err = resp.json().get("error")
        message = err.get("message") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        message = ""
    if resp.status_code in (401, 403):
        raise NodeError(f"{provider} credentials are invalid or lack permission", code="auth_failed")
    if resp.status_code == 404:
        raise NodeError(f"{provider}: model or endpoint not found", code="ai_model_not_found")
    if resp.status_code == 429:
        raise NodeError(f"{provider} rate limit reached", code="ai_rate_limited", retryable=True)
    if resp.status_code >= 500:
        raise NodeError(f"{provider} server error ({resp.status_code})", code="ai_provider_error", retryable=True)
    raise NodeError(f"{provider} rejected the request: {message}"[:500], code="ai_bad_request")


_PROVIDERS: dict[str, Provider] = {
    "anthropic": AnthropicProvider(),
    "openai": OpenAIProvider(),
    "openai_compatible": OpenAICompatibleProvider(),
    "gemini": GeminiProvider(),
}


def get_provider(key: str) -> Provider:
    p = _PROVIDERS.get(key)
    if not p:
        raise NodeError(f"Unknown AI provider '{key}'", code="ai_not_configured")
    return p


def register_provider(key: str, provider: Provider) -> None:
    """Extension point (also used by tests to install a deterministic provider)."""
    _PROVIDERS[key] = provider


def unregister_provider(key: str) -> None:
    _PROVIDERS.pop(key, None)
