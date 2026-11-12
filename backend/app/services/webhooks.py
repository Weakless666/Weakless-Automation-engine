"""Inbound event ingestion: signature verification, replay protection,
duplicate detection, routing to workflows, history and replay."""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import parse_qs

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import vault
from app.core.errors import AppError, QuotaExceeded
from app.core.ratelimit import shared_limiter as limiter  # shared across API instances
from app.core.redact import redact
from app.core.security import constant_time_equals, hmac_sha256_hex, sha256_hex
from app.db import set_tenant, utcnow
from app.models import WebhookEndpoint, WebhookEvent, Workflow, WorkflowVersion
from app.services.executions import start_execution
from app.services.workflows import endpoint_aad

log = logging.getLogger("wae.webhooks")
TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 1024 * 1024
KEPT_HEADERS = ("content-type", "user-agent", "x-wae-event-id", "idempotency-key", "x-github-event", "x-shopify-topic")


class Rejected(Exception):
    def __init__(self, reason: str, status: int = 401):
        self.reason, self.status = reason, status


def endpoint_secrets(ep: WebhookEndpoint) -> dict:
    return vault.decrypt_json(ep.secret_envelope, endpoint_aad(ep)) if ep.secret_envelope else {}


def _fresh(ts: str) -> bool:
    try:
        return abs(time.time() - int(ts)) <= TOLERANCE_SECONDS
    except (TypeError, ValueError):
        return False


def verify_signature(scheme: str, secret: str | None, headers: dict, body: bytes) -> None:
    """Raise ``Rejected`` unless the request is authentic. ``headers`` keys are lower-case."""
    if scheme == "none":
        return
    if not secret:
        raise Rejected("signing secret is not configured for this endpoint", 503)
    if scheme == "shared_secret":
        if not constant_time_equals(headers.get("x-wae-secret", ""), secret):
            raise Rejected("invalid shared secret")
    elif scheme == "telegram":
        if not constant_time_equals(headers.get("x-telegram-bot-api-secret-token", ""), secret):
            raise Rejected("invalid secret token")
    elif scheme == "hmac_sha256":
        ts = headers.get("x-wae-timestamp", "")
        if not _fresh(ts):
            raise Rejected("missing or stale timestamp")
        expected = "sha256=" + hmac_sha256_hex(secret, ts.encode() + b"." + body)
        if not constant_time_equals(headers.get("x-wae-signature", ""), expected):
            raise Rejected("invalid signature")
    elif scheme == "stripe":
        parts = dict(p.split("=", 1) for p in headers.get("stripe-signature", "").split(",") if "=" in p)
        ts = parts.get("t", "")
        if not _fresh(ts):
            raise Rejected("missing or stale timestamp")
        expected = hmac_sha256_hex(secret, ts.encode() + b"." + body)
        candidates = [p.split("=", 1)[1] for p in headers.get("stripe-signature", "").split(",") if p.startswith("v1=")]
        if not any(constant_time_equals(c, expected) for c in candidates):
            raise Rejected("invalid signature")
    elif scheme == "meta":
        expected = "sha256=" + hmac_sha256_hex(secret, body)
        if not constant_time_equals(headers.get("x-hub-signature-256", ""), expected):
            raise Rejected("invalid signature")
    elif scheme == "slack":
        ts = headers.get("x-slack-request-timestamp", "")
        if not _fresh(ts):
            raise Rejected("missing or stale timestamp")
        expected = "v0=" + hmac_sha256_hex(secret, b"v0:" + ts.encode() + b":" + body)
        if not constant_time_equals(headers.get("x-slack-signature", ""), expected):
            raise Rejected("invalid signature")
    else:
        raise Rejected("unsupported signature scheme", 500)


def _parse(headers: dict, body: bytes) -> dict:
    ctype = headers.get("content-type", "")
    if not body:
        return {}
    if "application/x-www-form-urlencoded" in ctype:
        return {k: v[0] if len(v) == 1 else v for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
    try:
        data = json.loads(body)
    except ValueError:
        raise Rejected("body is not valid JSON", 400)
    return data if isinstance(data, dict) else {"data": data}


def _dedupe_key(ep: WebhookEndpoint, headers: dict, data: dict, body: bytes) -> str | None:
    explicit = headers.get("x-wae-event-id") or headers.get("idempotency-key")
    if explicit:
        return f"id:{explicit[:150]}"
    if ep.signature_scheme == "stripe" and data.get("id"):
        return f"stripe:{data['id']}"
    if ep.signature_scheme == "telegram" and data.get("update_id") is not None:
        return f"tg:{data['update_id']}"
    if ep.signature_scheme == "slack" and data.get("event_id"):
        return f"slack:{data['event_id']}"
    if ep.signature_scheme == "meta":
        try:
            return "wa:" + data["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
        except (KeyError, IndexError, TypeError):
            return None
    if ep.signature_scheme == "hmac_sha256":
        return "sig:" + sha256_hex(headers.get("x-wae-timestamp", "").encode() + b"." + body)
    return None


def _normalize_message(provider: str, data: dict) -> dict:
    """Common ``text`` / ``from`` fields for Incoming Message triggers."""
    try:
        if provider == "telegram":
            msg = data.get("message") or data.get("edited_message") or {}
            return {"text": msg.get("text") or "", "from": str((msg.get("chat") or {}).get("id") or "")}
        if provider == "whatsapp":
            msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
            return {"text": (msg.get("text") or {}).get("body") or "", "from": msg.get("from") or ""}
        if provider == "slack":
            ev = data.get("event") or {}
            return {"text": "" if ev.get("bot_id") else ev.get("text") or "", "from": ev.get("channel") or ""}
    except (KeyError, IndexError, TypeError):
        pass
    return {"text": "", "from": ""}


def _node_config(db: Session, ep: WebhookEndpoint, wf: Workflow) -> tuple[dict | None, str]:
    version = db.get(WorkflowVersion, wf.current_version_id) if wf.current_version_id else None
    for n in (version.definition.get("nodes", []) if version else []):
        if n["id"] == ep.node_key:
            return n.get("config") or {}, n.get("type", "")
    return None, ""


def ingest(db: Session, endpoint_key: str, *, method: str, headers: dict, body: bytes, query: dict) -> tuple[int, dict]:
    """Handle one inbound request. ``db`` starts with RLS bypass to find the endpoint,
    then is bound to the endpoint's tenant."""
    headers = {k.lower(): v for k, v in headers.items()}
    ep = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.endpoint_key == endpoint_key))
    if not ep or not ep.is_active:
        return 404, {"error": {"code": "not_found", "message": "Unknown endpoint"}}
    set_tenant(db, ep.organization_id)
    if not limiter.allow(f"hook:{ep.id}", ep.rate_limit_per_minute):
        return 429, {"error": {"code": "rate_limited", "message": "Too many requests"}}
    secrets = endpoint_secrets(ep)

    # Meta (WhatsApp) subscription handshake
    if method == "GET":
        if ep.signature_scheme == "meta" and query.get("hub.mode") == "subscribe":
            token = secrets.get("verify_token") or ""
            if token and constant_time_equals(str(query.get("hub.verify_token", "")), token):
                return 200, {"_raw": str(query.get("hub.challenge", ""))}
            return 403, {"error": {"code": "forbidden", "message": "Verification failed"}}
        return 405, {"error": {"code": "method_not_allowed", "message": "Use POST"}}

    def record(status: str, reason: str | None, data: dict | None = None, dedupe: str | None = None) -> WebhookEvent:
        ev = WebhookEvent(organization_id=ep.organization_id, endpoint_id=ep.id, source=ep.kind, dedupe_key=dedupe, status=status,
                          reason=reason, headers={k: v for k, v in headers.items() if k in KEPT_HEADERS},
                          payload=redact(data or {}))
        db.add(ev)
        db.flush()
        return ev

    try:
        if len(body) > MAX_BODY_BYTES:
            raise Rejected("payload too large", 413)
        verify_signature(ep.signature_scheme, secrets.get("secret"), headers, body)
        data = _parse(headers, body)
    except Rejected as rej:
        record("rejected", rej.reason)
        db.commit()
        return rej.status, {"error": {"code": "rejected", "message": rej.reason}}

    if ep.signature_scheme == "slack" and data.get("type") == "url_verification":
        return 200, {"challenge": data.get("challenge")}

    ep.last_received_at = utcnow()
    dedupe = _dedupe_key(ep, headers, data, body)
    try:
        event = record("received", None, data, dedupe)
    except IntegrityError:
        db.rollback()
        return 200, {"status": "duplicate"}

    wf = db.get(Workflow, ep.workflow_id)
    config, node_type = _node_config(db, ep, wf) if wf else (None, "")
    payload: dict = {"body": data, "query": query, "headers": dict(event.headers), "event_id": str(event.id)}

    def finish(status: str, reason: str | None, code: int, extra: dict | None = None) -> tuple[int, dict]:
        event.status, event.reason = status, reason
        db.commit()
        return code, {"status": status, "event_id": str(event.id), **({"reason": reason} if reason else {}), **(extra or {})}

    if not wf or wf.deleted_at or config is None:
        return finish("ignored", "workflow is not published with this trigger", 200)
    if not wf.is_active:
        return finish("ignored", "workflow is not active", 200)
    if node_type == "trigger.form":
        missing = [f for f in (config.get("required_fields") or []) if data.get(f) in (None, "")]
        if missing:
            return finish("rejected", f"missing fields: {', '.join(missing)}", 422)
    if node_type == "trigger.payment":
        event.event_type = str(data.get("type") or "")
        payload["event_type"] = event.event_type
        wanted = config.get("event_types") or []
        if wanted and event.event_type not in wanted:
            return finish("ignored", "event type not subscribed", 200)
    if node_type == "trigger.message":
        payload.update(_normalize_message(config.get("provider") or "telegram", data))
        if not payload["text"]:
            return finish("ignored", "no message text", 200)
    try:
        ex = start_execution(db, wf, trigger_type=node_type.replace("trigger.", "") or "webhook", trigger_node=ep.node_key,
                             payload=payload, idempotency_key=f"event:{event.id}")
    except QuotaExceeded as exc:
        return finish("failed", exc.message, 429)
    except AppError as exc:
        return finish("failed", exc.message, 200)
    event.execution_id = ex.id
    return finish("processed", None, 202, {"execution_id": str(ex.id)})


def replay_event(db: Session, event: WebhookEvent) -> WebhookEvent:
    """Re-run a stored event through its workflow (new event row, new execution)."""
    if not event.endpoint_id:
        raise AppError("Only endpoint events can be replayed", code="not_replayable", status_code=409)
    ep = db.get(WebhookEndpoint, event.endpoint_id)
    wf = db.get(Workflow, ep.workflow_id) if ep else None
    if not ep or not wf or wf.deleted_at:
        raise AppError("The workflow for this event no longer exists", code="not_replayable", status_code=409)
    config, node_type = _node_config(db, ep, wf)
    new = WebhookEvent(organization_id=event.organization_id, endpoint_id=ep.id, source="replay", event_type=event.event_type,
                       headers=event.headers, payload=event.payload, status="received", replay_of_id=event.id)
    db.add(new)
    db.flush()
    payload: dict = {"body": event.payload, "query": {}, "headers": dict(event.headers or {}), "event_id": str(new.id), "replayed": True}
    if node_type == "trigger.message":
        payload.update(_normalize_message((config or {}).get("provider") or "telegram", event.payload or {}))
    if node_type == "trigger.payment":
        payload["event_type"] = event.event_type
    ex = start_execution(db, wf, trigger_type=node_type.replace("trigger.", "") or "webhook", trigger_node=ep.node_key,
                         payload=payload, idempotency_key=f"event:{new.id}")
    new.execution_id, new.status = ex.id, "processed"
    db.flush()
    return new
