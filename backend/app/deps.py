"""Authentication and authorization dependencies.

Tenant resolution never trusts the client: the ``X-Organization-Id`` header is
only a *request* for a tenant. It is honoured after verifying that the
authenticated user is Weakless staff or an active member of that organization.
Only then is the database session bound to the tenant (RLS on PostgreSQL).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Forbidden, NotFound, Unauthorized
from app.core.security import CSRF_HEADER, SESSION_COOKIE, constant_time_equals, decode_session_token
from app.db import get_db, set_tenant, utcnow
from app.models import Organization, OrganizationMember, User, UserSession
from app.rbac import is_platform_role, role_permissions

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Ctx:
    db: Session
    user: User
    session: UserSession
    ip: str | None = None
    user_agent: str | None = None
    org: Organization | None = None
    role: str = ""
    permissions: set[str] = field(default_factory=set)

    @property
    def org_id(self) -> uuid.UUID:
        assert self.org is not None
        return self.org.id

    @property
    def is_staff(self) -> bool:
        return is_platform_role(self.user.platform_role)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise Forbidden(f"Missing permission: {permission}")


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def authenticate(request: Request, db: Session = Depends(get_db)) -> Ctx:
    token = request.cookies.get(SESSION_COOKIE)
    via_cookie = bool(token)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise Unauthorized("Authentication required")
    claims = decode_session_token(token)
    if not claims:
        raise Unauthorized("Invalid or expired session")
    try:
        sid, uid = uuid.UUID(claims["sid"]), uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise Unauthorized("Invalid session")
    sess = db.get(UserSession, sid)
    now = utcnow()
    if not sess or sess.user_id != uid or sess.revoked_at or sess.expires_at <= now:
        raise Unauthorized("Session expired or revoked")
    user = db.get(User, uid)
    if not user or not user.is_active or user.deleted_at:
        raise Unauthorized("Account disabled")
    # CSRF: cookie-authenticated mutations must carry the per-session token in a custom header.
    if via_cookie and request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not constant_time_equals(sent, sess.csrf_token):
            raise Forbidden("CSRF token missing or invalid", code="csrf_failed")
    if not sess.last_seen_at or (now - sess.last_seen_at).total_seconds() > 60:
        sess.last_seen_at = now
        db.commit()
    ctx = Ctx(db=db, user=user, session=sess, ip=client_ip(request), user_agent=request.headers.get("user-agent", "")[:400])
    request.state.user_id = str(user.id)
    return ctx


def resolve_org(ctx: Ctx, org_id: uuid.UUID) -> Ctx:
    """Bind ``ctx`` to an organization after verifying access."""
    org = ctx.db.get(Organization, org_id)
    if not org or org.deleted_at:
        raise NotFound("Organization not found")
    if ctx.is_staff:
        role = ctx.user.platform_role or ""
    else:
        member = ctx.db.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.user_id == ctx.user.id,
                OrganizationMember.status == "active",
            )
        )
        if not member:
            # Same response as a missing organization: do not reveal that it exists.
            raise NotFound("Organization not found")
        if org.status != "active":
            raise Forbidden("This organization is suspended", code="organization_suspended")
        role = member.role
    ctx.org = org
    ctx.role = role
    ctx.permissions = set(role_permissions(ctx.db, role))
    set_tenant(ctx.db, org.id)
    return ctx


def org_ctx(permission: str | None = None) -> Callable[..., Ctx]:
    """Dependency: authenticated user acting inside the organization named by ``X-Organization-Id``."""

    def dep(request: Request, ctx: Ctx = Depends(authenticate)) -> Ctx:
        raw = request.headers.get("x-organization-id")
        if not raw:
            raise Forbidden("X-Organization-Id header is required", code="organization_required")
        try:
            org_id = uuid.UUID(raw)
        except ValueError:
            raise NotFound("Organization not found")
        resolve_org(ctx, org_id)
        if permission:
            ctx.require(permission)
        return ctx

    return dep


def platform_ctx(permission: str | None = None) -> Callable[..., Ctx]:
    """Dependency: Weakless staff acting across organizations."""

    def dep(ctx: Ctx = Depends(authenticate)) -> Ctx:
        if not ctx.is_staff:
            raise Forbidden("Platform access required")
        ctx.role = ctx.user.platform_role or ""
        ctx.permissions = set(role_permissions(ctx.db, ctx.role))
        if permission:
            ctx.require(permission)
        set_tenant(ctx.db, None, bypass_rls=True)
        return ctx

    return dep


def user_ctx(ctx: Ctx = Depends(authenticate)) -> Ctx:
    """Dependency: any authenticated user, no tenant binding (profile, own notifications)."""
    if ctx.is_staff:
        ctx.role = ctx.user.platform_role or ""
        ctx.permissions = set(role_permissions(ctx.db, ctx.role))
    return ctx


def optional_org_ctx(permission: str) -> Callable[..., Ctx]:
    """Organization-scoped when ``X-Organization-Id`` is sent; otherwise platform-wide
    (staff only). Used by analytics/executions views that support "all organizations"."""

    def dep(request: Request, ctx: Ctx = Depends(authenticate)) -> Ctx:
        raw = request.headers.get("x-organization-id")
        if raw:
            try:
                org_id = uuid.UUID(raw)
            except ValueError:
                raise NotFound("Organization not found")
            resolve_org(ctx, org_id)
            ctx.require(permission)
            return ctx
        if not ctx.is_staff:
            raise Forbidden("X-Organization-Id header is required", code="organization_required")
        ctx.role = ctx.user.platform_role or ""
        ctx.permissions = set(role_permissions(ctx.db, ctx.role))
        ctx.require("platform.analytics.read")
        set_tenant(ctx.db, None, bypass_rls=True)
        return ctx

    return dep
