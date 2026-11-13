"""Public inbound endpoints (webhooks, forms, resume links) and event management."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import Field
from sqlalchemy import select

from app.api import schemas
from app.api.common import ApiRoute
from app.api.common import Body, Paging, iso, owned, paginate, parse_uuid
from app.config import get_settings
from app.core.errors import AppError, ValidationFailed
from app.core.ratelimit import shared_limiter as limiter  # shared across API instances
from app.core.security import constant_time_equals, random_token
from app.db import new_session, set_tenant
from app.deps import Ctx, client_ip, org_ctx
from app.models import WebhookEndpoint, WebhookEvent, WorkflowExecution
from app.services import webhooks as hooks
from app.services import workflows as wfs
from app.services.audit import audit
from app.services.events import emit_app_event
from app.services.executions import resume_execution

public = APIRouter(route_class=ApiRoute, tags=["inbound"])
router = APIRouter(route_class=ApiRoute, tags=["webhooks"])


@public.post("/hooks/{endpoint_key}", response_model=schemas.HookResult, operation_id="receive_webhook")
@public.get("/hooks/{endpoint_key}", response_model=schemas.HookResult, operation_id="receive_webhook_get")
async def inbound(endpoint_key: str, request: Request):
    """Unique per-trigger URL. Authenticity is established by the endpoint's signature scheme."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > hooks.MAX_BODY_BYTES:
        return JSONResponse({"error": {"code": "payload_too_large", "message": "Payload too large"}}, status_code=413)
    body = await request.body()
    if len(body) > hooks.MAX_BODY_BYTES:
        return JSONResponse({"error": {"code": "payload_too_large", "message": "Payload too large"}}, status_code=413)
    if not limiter.allow(f"hook-ip:{client_ip(request)}", 600):
        return JSONResponse({"error": {"code": "rate_limited", "message": "Too many requests"}}, status_code=429)
    with new_session(bypass_rls=True) as db:
        status, payload = hooks.ingest(db, endpoint_key, method=request.method, headers=dict(request.headers), body=body,
                                       query=dict(request.query_params))
    if "_raw" in payload:
        return PlainTextResponse(payload["_raw"], status_code=status)
    wait = payload.pop("_wait", None)
    if wait:  # the trigger waits for a Respond to Webhook step (run off the event loop: it executes the workflow)
        reply = await run_in_threadpool(hooks.wait_for_response, wait["execution_id"], wait["timeout"])
        if reply:
            code, body_out, ctype = int(reply.get("status_code") or 200), reply.get("body"), reply.get("content_type") or "application/json"
            if ctype == "application/json":
                return JSONResponse(body_out, status_code=code)
            return Response(str(body_out), status_code=code, media_type=ctype)
    return JSONResponse(payload, status_code=status)


@public.post("/hooks/resume/{execution_id}/{token}", response_model=schemas.StatusOut, status_code=202, operation_id="resume_execution_link")
@public.get("/hooks/resume/{execution_id}/{token}", response_model=schemas.StatusOut, status_code=202, operation_id="resume_execution_link_get")
async def resume_link(execution_id: str, token: str, request: Request):
    """Resume an execution paused on a Wait node. The unguessable per-execution token is the credential."""
    if not limiter.allow(f"resume-ip:{client_ip(request)}", 60):
        return JSONResponse({"error": {"code": "rate_limited", "message": "Too many requests"}}, status_code=429)
    data: dict = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                data.update(body)
        except Exception:  # noqa: BLE001 - body is optional
            pass
    node_id = data.pop("node", None)
    with new_session(bypass_rls=True) as db:
        try:
            ex = db.get(WorkflowExecution, parse_uuid(execution_id, "Execution"))
        except AppError:
            ex = None
        if not ex or not ex.resume_token or not constant_time_equals(token, ex.resume_token):
            return JSONResponse({"error": {"code": "not_found", "message": "Unknown or expired link"}}, status_code=404)
        set_tenant(db, ex.organization_id)
        try:
            resume_execution(db, ex, data, node_id)
        except AppError as exc:
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)
        db.commit()
    return JSONResponse({"status": "resumed"}, status_code=202)


# ----------------------------------------------------------------------------- management


def endpoint_view(e: WebhookEndpoint) -> dict:
    base = get_settings().public_base_url.rstrip("/")
    return {"id": str(e.id), "workflow_id": str(e.workflow_id), "node_id": e.node_key, "kind": e.kind, "signature_scheme": e.signature_scheme,
            "url": f"{base}/api/v1/hooks/{e.endpoint_key}", "has_secret": bool(e.secret_envelope and "secret" in hooks.endpoint_secrets(e)),
            "is_active": e.is_active, "rate_limit_per_minute": e.rate_limit_per_minute, "last_received_at": iso(e.last_received_at)}


@router.get("/webhooks", response_model=schemas.Items[schemas.EndpointOut])
def list_endpoints(ctx: Ctx = Depends(org_ctx("webhooks.read"))) -> dict:
    rows = ctx.db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.organization_id == ctx.org_id).order_by(WebhookEndpoint.created_at.desc())).all()
    return {"items": [endpoint_view(e) for e in rows]}


@router.post("/webhooks/{endpoint_id}/rotate-secret", response_model=schemas.SecretRotated)
def rotate_secret(endpoint_id: str, ctx: Ctx = Depends(org_ctx("webhooks.manage"))) -> dict:
    """Generate a new signing secret. It is returned exactly once."""
    ep = owned(ctx.db, WebhookEndpoint, endpoint_id, ctx.org_id, "Endpoint")
    if ep.signature_scheme not in ("hmac_sha256", "shared_secret", "telegram"):
        raise AppError("This endpoint uses a provider-issued secret. Set it instead of rotating.", code="provider_secret", status_code=409)
    secret = random_token(32)
    wfs.set_endpoint_secret(ep, secret)
    audit(ctx.db, "webhook.secret_rotated", ctx=ctx, resource_type="webhook_endpoint", resource_id=ep.id)
    ctx.db.commit()
    return {"secret": secret, "endpoint": endpoint_view(ep)}


class SecretBody(Body):
    secret: str = Field(..., min_length=8, max_length=500)


@router.put("/webhooks/{endpoint_id}/secret", response_model=schemas.EndpointOut)
def set_secret(endpoint_id: str, body: SecretBody, ctx: Ctx = Depends(org_ctx("webhooks.manage"))) -> dict:
    """Store a provider-issued signing secret (Stripe ``whsec_...``, Meta app secret, Slack signing secret)."""
    ep = owned(ctx.db, WebhookEndpoint, endpoint_id, ctx.org_id, "Endpoint")
    wfs.set_endpoint_secret(ep, body.secret)
    audit(ctx.db, "webhook.secret_set", ctx=ctx, resource_type="webhook_endpoint", resource_id=ep.id)
    ctx.db.commit()
    return endpoint_view(ep)


@router.get("/webhooks/{endpoint_id}/verify-token", response_model=schemas.VerifyToken)
def verify_token(endpoint_id: str, ctx: Ctx = Depends(org_ctx("webhooks.manage"))) -> dict:
    """Verify token to enter in the Meta developer console when subscribing this URL."""
    ep = owned(ctx.db, WebhookEndpoint, endpoint_id, ctx.org_id, "Endpoint")
    token = hooks.endpoint_secrets(ep).get("verify_token")
    if not token:
        raise AppError("This endpoint has no verify token", code="not_applicable", status_code=409)
    audit(ctx.db, "webhook.verify_token_viewed", ctx=ctx, resource_type="webhook_endpoint", resource_id=ep.id)
    ctx.db.commit()
    return {"verify_token": token}


def event_view(e: WebhookEvent, full: bool = False) -> dict:
    out = {"id": str(e.id), "endpoint_id": str(e.endpoint_id) if e.endpoint_id else None, "source": e.source, "event_type": e.event_type,
           "status": e.status, "reason": e.reason, "execution_id": str(e.execution_id) if e.execution_id else None,
           "replay_of_id": str(e.replay_of_id) if e.replay_of_id else None, "received_at": iso(e.received_at)}
    if full:
        out["payload"], out["headers"] = e.payload, e.headers
    return out


@router.get("/events", response_model=schemas.Page[schemas.EventOut])
def list_events(status: str | None = None, source: str | None = None, endpoint_id: str | None = None, paging: Paging = Depends(),
                ctx: Ctx = Depends(org_ctx("webhooks.read"))) -> dict:
    q = select(WebhookEvent).where(WebhookEvent.organization_id == ctx.org_id)
    if status:
        q = q.where(WebhookEvent.status == status)
    if source:
        q = q.where(WebhookEvent.source == source)
    if endpoint_id:
        q = q.where(WebhookEvent.endpoint_id == parse_uuid(endpoint_id, "Endpoint"))
    return paginate(ctx.db, q.order_by(WebhookEvent.received_at.desc()), paging, event_view)


@router.get("/events/{event_id}", response_model=schemas.EventOut)
def get_event(event_id: str, ctx: Ctx = Depends(org_ctx("webhooks.read"))) -> dict:
    return event_view(owned(ctx.db, WebhookEvent, event_id, ctx.org_id, "Event"), full=True)


@router.post("/events/{event_id}/replay", response_model=schemas.EventOut, status_code=202)
def replay(event_id: str, ctx: Ctx = Depends(org_ctx("webhooks.manage"))) -> dict:
    event = owned(ctx.db, WebhookEvent, event_id, ctx.org_id, "Event")
    new = hooks.replay_event(ctx.db, event)
    audit(ctx.db, "event.replayed", ctx=ctx, resource_type="event", resource_id=event.id, meta={"new": str(new.id)})
    ctx.db.commit()
    return event_view(new)


class EmitBody(Body):
    event_type: str = Field(..., min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_.\-]+$")
    data: dict[str, Any] = Field(default_factory=dict)


@router.post("/events", response_model=schemas.EventOut, status_code=202)
def emit(body: EmitBody, ctx: Ctx = Depends(org_ctx("workflows.execute"))) -> dict:
    """Emit a custom application event; active workflows with a matching Application Event trigger start."""
    if body.event_type.startswith(("workflow.", "content.")):
        raise ValidationFailed("Event types starting with 'workflow.' or 'content.' are reserved for the platform")
    event = emit_app_event(ctx.db, ctx.org_id, body.event_type, body.data, source="app")
    ctx.db.commit()
    return event_view(event)
