"""Integration hub: provider catalog, connections, OAuth, health and revocation."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from pydantic import Field
from sqlalchemy import select

from app.api.common import Body, iso, owned
from app.config import get_settings
from app.core.errors import AppError, Conflict, NodeError
from app.db import new_session, set_tenant, utcnow
from app.deps import Ctx, org_ctx
from app.integrations import oauth, service
from app.integrations.providers import PROVIDERS
from app.models import Integration
from app.services.audit import audit

router = APIRouter(tags=["integrations"])


@router.get("/integrations/providers")
def providers(ctx: Ctx = Depends(org_ctx("integrations.read"))) -> dict:
    items = []
    for p in PROVIDERS.values():
        d = p.describe()
        # An OAuth provider without a platform OAuth app cannot be connected yet - say so up front.
        d["available"] = service.oauth_configured(p) if p.oauth else True
        d["unavailable_reason"] = None if d["available"] else f"Awaiting platform OAuth credentials (WAE_OAUTH_{p.oauth.group.upper()}_CLIENT_ID / _CLIENT_SECRET)"
        items.append(d)
    return {"items": items}


@router.get("/integrations")
def list_integrations(provider: str | None = None, ctx: Ctx = Depends(org_ctx("integrations.read"))) -> dict:
    q = select(Integration).where(Integration.organization_id == ctx.org_id, Integration.deleted_at.is_(None))
    if provider:
        q = q.where(Integration.provider_key.in_(provider.split(",")))
    rows = ctx.db.scalars(q.order_by(Integration.provider_key, Integration.name)).all()
    return {"items": [service.public_view(ctx.db, i) for i in rows]}


class IntegrationCreate(Body):
    provider_key: str
    name: str = Field("", max_length=200)
    values: dict[str, Any] = Field(default_factory=dict, description="Provider fields; secret fields are encrypted and never returned")


class IntegrationUpdate(Body):
    name: str | None = Field(None, max_length=200)
    values: dict[str, Any] | None = None


@router.post("/integrations", status_code=201)
def create_integration(body: IntegrationCreate, ctx: Ctx = Depends(org_ctx("integrations.manage"))) -> dict:
    p = service.provider_def(body.provider_key)
    name = body.name or p.name
    if ctx.db.scalar(select(Integration.id).where(Integration.organization_id == ctx.org_id, Integration.provider_key == p.key,
                                                  Integration.name == name, Integration.deleted_at.is_(None))):
        raise Conflict("An integration with this name already exists for this provider")
    integration = service.create_integration(ctx.db, ctx.org_id, p.key, name, body.values, ctx.user.id)
    audit(ctx.db, "integration.created", ctx=ctx, resource_type="integration", resource_id=integration.id,
          meta={"provider": p.key, "name": name, "status": integration.status})
    ctx.db.commit()
    return service.public_view(ctx.db, integration)


@router.get("/integrations/{integration_id}")
def get_integration(integration_id: str, ctx: Ctx = Depends(org_ctx("integrations.read"))) -> dict:
    return service.public_view(ctx.db, owned(ctx.db, Integration, integration_id, ctx.org_id, "Integration"))


@router.patch("/integrations/{integration_id}")
def update_integration(integration_id: str, body: IntegrationUpdate, ctx: Ctx = Depends(org_ctx("integrations.manage"))) -> dict:
    integration = owned(ctx.db, Integration, integration_id, ctx.org_id, "Integration")
    service.update_integration(ctx.db, integration, body.name, body.values)
    audit(ctx.db, "integration.updated", ctx=ctx, resource_type="integration", resource_id=integration.id,
          meta={"fields": sorted((body.values or {}).keys())})
    ctx.db.commit()
    return service.public_view(ctx.db, integration)


@router.post("/integrations/{integration_id}/test")
def test_integration(integration_id: str, ctx: Ctx = Depends(org_ctx("integrations.manage"))) -> dict:
    integration = owned(ctx.db, Integration, integration_id, ctx.org_id, "Integration")
    result = service.test_integration(ctx.db, integration)
    ctx.db.commit()
    return {"result": result, "integration": service.public_view(ctx.db, integration)}


@router.post("/integrations/{integration_id}/revoke")
def revoke(integration_id: str, ctx: Ctx = Depends(org_ctx("integrations.manage"))) -> dict:
    integration = owned(ctx.db, Integration, integration_id, ctx.org_id, "Integration")
    service.revoke_integration(ctx.db, integration)
    audit(ctx.db, "integration.revoked", ctx=ctx, resource_type="integration", resource_id=integration.id, meta={"provider": integration.provider_key})
    ctx.db.commit()
    return service.public_view(ctx.db, integration)


@router.delete("/integrations/{integration_id}")
def delete_integration(integration_id: str, ctx: Ctx = Depends(org_ctx("integrations.manage"))) -> dict:
    integration = owned(ctx.db, Integration, integration_id, ctx.org_id, "Integration")
    service.revoke_integration(ctx.db, integration)
    integration.deleted_at = utcnow()
    # Free the unique (organization, provider, name) slot for a future integration.
    integration.name = f"{integration.name} (deleted {integration.id.hex[:8]})"[:200]
    audit(ctx.db, "integration.deleted", ctx=ctx, resource_type="integration", resource_id=integration.id)
    ctx.db.commit()
    return {"ok": True}


@router.post("/integrations/{integration_id}/oauth/start")
def oauth_start(integration_id: str, ctx: Ctx = Depends(org_ctx("integrations.manage"))) -> dict:
    integration = owned(ctx.db, Integration, integration_id, ctx.org_id, "Integration")
    p = service.provider_def(integration.provider_key)
    if not p.oauth:
        raise AppError("This provider does not use OAuth", code="not_oauth", status_code=409)
    url = oauth.authorization_url(ctx.db, integration, p, ctx.user.id)
    ctx.db.commit()
    return {"authorization_url": url}


@router.get("/integrations/oauth/callback", include_in_schema=False)
def oauth_callback(state: str = Query("", max_length=200), code: str = Query("", max_length=4000), error: str = Query("", max_length=200)):
    """Provider redirect target. Unauthenticated by design: the single-use, hashed
    ``state`` created by an authorized user binds the request to one integration."""
    front = get_settings().frontend_url.rstrip("/")
    if error or not state or not code:
        return RedirectResponse(f"{front}/integrations?oauth=denied", status_code=302)
    with new_session(bypass_rls=True) as db:
        try:
            integration = oauth.complete_authorization(db, state, code)
            set_tenant(db, integration.organization_id)
            audit(db, "integration.oauth_completed", organization_id=integration.organization_id, resource_type="integration",
                  resource_id=integration.id, meta={"status": integration.status}, actor_email="oauth-callback")
            db.commit()
            return RedirectResponse(f"{front}/integrations?oauth={'connected' if integration.status == 'connected' else 'error'}", status_code=302)
        except AppError:
            db.commit()  # keep the recorded failure state
            return RedirectResponse(f"{front}/integrations?oauth=error", status_code=302)


@router.get("/integrations/{integration_id}/mcp-tools")
def mcp_tools(integration_id: str, ctx: Ctx = Depends(org_ctx("integrations.manage"))) -> dict:
    """Discover the tools of a connected MCP server (to allowlist individual tools on an agent)."""
    from app.ai.mcp import McpClient

    integration = owned(ctx.db, Integration, integration_id, ctx.org_id, "Integration")
    if integration.provider_key != "mcp_server":
        raise AppError("Integration is not an MCP server", code="not_mcp", status_code=409)
    conn = service.connection(ctx.db, integration)
    try:
        return {"items": McpClient(str(conn.get("url")), conn.get("api_key") or None).list_tools()}
    except NodeError as exc:
        raise AppError(exc.message, code=exc.code, status_code=502) from exc


@router.get("/integrations-health")
def health_summary(ctx: Ctx = Depends(org_ctx("integrations.read"))) -> dict:
    rows = ctx.db.scalars(select(Integration).where(Integration.organization_id == ctx.org_id, Integration.deleted_at.is_(None))).all()
    return {"items": [{"id": str(i.id), "name": i.name, "provider_key": i.provider_key, "status": i.status,
                       "last_checked_at": iso(i.last_checked_at), "last_ok_at": iso(i.last_ok_at), "last_error": i.last_error} for i in rows]}
