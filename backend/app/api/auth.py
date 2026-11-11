"""Authentication, sessions, staff users, roles and system settings."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import rbac
from app.api.common import Body, Paging, iso, paginate, parse_uuid
from app.config import get_settings
from app.core.errors import AppError, Conflict, Forbidden, NotFound, RateLimited, Unauthorized, ValidationFailed
from app.core.ratelimit import limiter
from app.core.security import (
    SESSION_COOKIE, create_session_token, hash_password, random_token, session_expiry, validate_password_strength, verify_password,
)
from app.db import get_db, utcnow
from app.deps import Ctx, client_ip, platform_ctx, user_ctx
from app.models import (
    PLATFORM_ROLES, LoginHistory, Organization, OrganizationMember, Permission, Role, RolePermission, SystemSetting, User, UserSession,
)
from app.services.audit import audit
from app.services.notifications import notify

router = APIRouter(tags=["auth"])
MAX_FAILED_LOGINS = 8
LOCK_MINUTES = 15
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


class LoginBody(Body):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=200)


def user_view(u: User) -> dict:
    return {"id": str(u.id), "email": u.email, "full_name": u.full_name, "platform_role": u.platform_role, "is_active": u.is_active,
            "last_login_at": iso(u.last_login_at), "created_at": iso(u.created_at)}


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    s = get_settings()
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")[:400]
    email = body.email.lower().strip()
    if not limiter.allow(f"login:{ip}", s.auth_rate_limit_per_minute) or not limiter.allow(f"login-email:{email}", s.auth_rate_limit_per_minute):
        raise RateLimited("Too many login attempts. Try again in a minute.")
    user = db.scalar(select(User).where(User.email == email))
    now = utcnow()

    def fail(reason: str) -> None:
        db.add(LoginHistory(user_id=user.id if user else None, email=email, success=False, reason=reason, ip=ip, user_agent=ua))
        db.commit()
        raise Unauthorized("Invalid email or password", code="invalid_credentials")

    if not user or user.deleted_at:
        verify_password(body.password, _DUMMY_HASH)  # keep response time independent of account existence
        fail("unknown_user")
    assert user is not None
    if user.locked_until and user.locked_until > now:
        fail("locked")
    if not verify_password(body.password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            user.failed_login_count = 0
            audit(db, "auth.account_locked", resource_type="user", resource_id=user.id, actor_email=email, meta={"ip": ip})
            notify(db, "security.event", None, title="Account temporarily locked", body=f"{email} was locked after repeated failed logins.",
                   severity="warning", data={"email": email})
        fail("bad_password")
    if not user.is_active:
        fail("disabled")
    user.failed_login_count, user.locked_until, user.last_login_at = 0, None, now
    sess = UserSession(user_id=user.id, ip=ip, user_agent=ua, csrf_token=random_token(24), expires_at=session_expiry())
    db.add(sess)
    db.add(LoginHistory(user_id=user.id, email=email, success=True, ip=ip, user_agent=ua))
    db.flush()
    token = create_session_token(user.id, sess.id, sess.expires_at)
    db.commit()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=s.cookie_secure, samesite="lax",
                        max_age=s.session_hours * 3600, path="/")
    # `token` supports non-browser API clients; the browser app relies on the HttpOnly cookie + CSRF header.
    return {"token": token, "csrf_token": sess.csrf_token, "expires_at": iso(sess.expires_at), "user": user_view(user)}


@router.post("/auth/logout")
def logout(response: Response, ctx: Ctx = Depends(user_ctx)) -> dict:
    ctx.session.revoked_at = utcnow()
    ctx.db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(ctx: Ctx = Depends(user_ctx)) -> dict:
    db = ctx.db
    if ctx.is_staff:
        orgs = db.scalars(select(Organization).where(Organization.deleted_at.is_(None)).order_by(Organization.name).limit(500)).all()
        org_rows = [{"id": str(o.id), "name": o.name, "slug": o.slug, "status": o.status, "mode": o.mode, "role": ctx.user.platform_role,
                     "permissions": sorted(ctx.permissions)} for o in orgs]
    else:
        rows = db.execute(select(Organization, OrganizationMember.role).join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
                          .where(OrganizationMember.user_id == ctx.user.id, OrganizationMember.status == "active",
                                 Organization.deleted_at.is_(None), Organization.status == "active").order_by(Organization.name)).all()
        org_rows = [{"id": str(o.id), "name": o.name, "slug": o.slug, "status": o.status, "mode": o.mode, "role": role,
                     "permissions": sorted(rbac.role_permissions(db, role))} for o, role in rows]
    return {"user": user_view(ctx.user), "is_staff": ctx.is_staff, "platform_permissions": sorted(ctx.permissions) if ctx.is_staff else [],
            "organizations": org_rows, "csrf_token": ctx.session.csrf_token}


class PasswordBody(Body):
    current_password: str
    new_password: str = Field(..., max_length=200)


@router.post("/auth/change-password")
def change_password(body: PasswordBody, ctx: Ctx = Depends(user_ctx)) -> dict:
    if not verify_password(body.current_password, ctx.user.password_hash):
        raise Forbidden("Current password is incorrect")
    problem = validate_password_strength(body.new_password)
    if problem:
        raise ValidationFailed(problem)
    ctx.user.password_hash = hash_password(body.new_password)
    # Revoke every other session.
    for s in ctx.db.scalars(select(UserSession).where(UserSession.user_id == ctx.user.id, UserSession.revoked_at.is_(None), UserSession.id != ctx.session.id)):
        s.revoked_at = utcnow()
    audit(ctx.db, "auth.password_changed", ctx=ctx, resource_type="user", resource_id=ctx.user.id)
    ctx.db.commit()
    return {"ok": True}


class ProfileBody(Body):
    full_name: str = Field(..., min_length=1, max_length=200)


@router.patch("/auth/me")
def update_profile(body: ProfileBody, ctx: Ctx = Depends(user_ctx)) -> dict:
    ctx.user.full_name = body.full_name
    ctx.db.commit()
    return user_view(ctx.user)


@router.get("/auth/sessions")
def sessions(ctx: Ctx = Depends(user_ctx)) -> dict:
    rows = ctx.db.scalars(select(UserSession).where(UserSession.user_id == ctx.user.id, UserSession.revoked_at.is_(None),
                                                    UserSession.expires_at > utcnow()).order_by(UserSession.created_at.desc())).all()
    return {"items": [{"id": str(s.id), "ip": s.ip, "user_agent": s.user_agent, "created_at": iso(s.created_at),
                       "last_seen_at": iso(s.last_seen_at), "expires_at": iso(s.expires_at), "current": s.id == ctx.session.id} for s in rows]}


@router.delete("/auth/sessions/{session_id}")
def revoke_session(session_id: str, ctx: Ctx = Depends(user_ctx)) -> dict:
    s = ctx.db.get(UserSession, parse_uuid(session_id, "Session"))
    if not s or s.user_id != ctx.user.id:
        raise NotFound("Session not found")
    s.revoked_at = utcnow()
    audit(ctx.db, "auth.session_revoked", ctx=ctx, resource_type="session", resource_id=s.id)
    ctx.db.commit()
    return {"ok": True}


@router.get("/auth/login-history")
def login_history(paging: Paging = Depends(), ctx: Ctx = Depends(user_ctx)) -> dict:
    q = select(LoginHistory).where(LoginHistory.user_id == ctx.user.id).order_by(LoginHistory.created_at.desc())
    return paginate(ctx.db, q, paging, lambda h: {"id": str(h.id), "success": h.success, "reason": h.reason, "ip": h.ip,
                                                  "user_agent": h.user_agent, "created_at": iso(h.created_at)})


# ----------------------------------------------------------------------------- staff users


class StaffCreate(Body):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., max_length=200)
    platform_role: str


class StaffUpdate(Body):
    full_name: str | None = Field(None, min_length=1, max_length=200)
    platform_role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(None, max_length=200)


def _guard_role(ctx: Ctx, role: str | None, target: User | None = None) -> None:
    if role is not None and role not in PLATFORM_ROLES:
        raise ValidationFailed(f"Role must be one of: {', '.join(PLATFORM_ROLES)}")
    # Only a SUPER_ADMIN may create, modify or promote to SUPER_ADMIN (no privilege escalation).
    touches_super = role == "SUPER_ADMIN" or (target is not None and target.platform_role == "SUPER_ADMIN")
    if touches_super and ctx.user.platform_role != "SUPER_ADMIN":
        raise Forbidden("Only a Super Admin can manage Super Admin accounts")


@router.get("/users")
def list_staff(paging: Paging = Depends(), ctx: Ctx = Depends(platform_ctx("platform.users.manage"))) -> dict:
    q = select(User).where(User.platform_role.is_not(None), User.deleted_at.is_(None)).order_by(User.email)
    return paginate(ctx.db, q, paging, user_view)


@router.post("/users", status_code=201)
def create_staff(body: StaffCreate, ctx: Ctx = Depends(platform_ctx("platform.users.manage"))) -> dict:
    _guard_role(ctx, body.platform_role)
    problem = validate_password_strength(body.password)
    if problem:
        raise ValidationFailed(problem)
    email = body.email.lower().strip()
    if ctx.db.scalar(select(User.id).where(User.email == email)):
        raise Conflict("A user with this email already exists")
    u = User(email=email, full_name=body.full_name, password_hash=hash_password(body.password), platform_role=body.platform_role)
    ctx.db.add(u)
    ctx.db.flush()
    audit(ctx.db, "user.created", ctx=ctx, resource_type="user", resource_id=u.id, meta={"email": email, "platform_role": body.platform_role})
    ctx.db.commit()
    return user_view(u)


@router.patch("/users/{user_id}")
def update_staff(user_id: str, body: StaffUpdate, ctx: Ctx = Depends(platform_ctx("platform.users.manage"))) -> dict:
    u = ctx.db.get(User, parse_uuid(user_id, "User"))
    if not u or u.deleted_at or not u.platform_role:
        raise NotFound("User not found")
    _guard_role(ctx, body.platform_role, u)
    if u.id == ctx.user.id and (body.is_active is False or (body.platform_role and body.platform_role != u.platform_role)):
        raise AppError("You cannot change your own role or disable yourself", code="self_modification", status_code=409)
    changes = {}
    if body.full_name is not None:
        u.full_name = body.full_name
    if body.platform_role is not None and body.platform_role != u.platform_role:
        changes["platform_role"] = [u.platform_role, body.platform_role]
        u.platform_role = body.platform_role
    if body.is_active is not None and body.is_active != u.is_active:
        changes["is_active"] = body.is_active
        u.is_active = body.is_active
    if body.password:
        problem = validate_password_strength(body.password)
        if problem:
            raise ValidationFailed(problem)
        u.password_hash = hash_password(body.password)
        changes["password_reset"] = True
    if changes.get("is_active") is False or changes.get("password_reset") or "platform_role" in changes:
        for s in ctx.db.scalars(select(UserSession).where(UserSession.user_id == u.id, UserSession.revoked_at.is_(None))):
            s.revoked_at = utcnow()
    audit(ctx.db, "user.updated", ctx=ctx, resource_type="user", resource_id=u.id, meta=changes)
    ctx.db.commit()
    return user_view(u)


# ----------------------------------------------------------------------------- roles & settings


@router.get("/roles")
def list_roles(ctx: Ctx = Depends(platform_ctx())) -> dict:
    db = ctx.db
    roles = db.scalars(select(Role).order_by(Role.scope.desc(), Role.key)).all()
    grants: dict = {}
    for rid, pkey in db.execute(select(RolePermission.role_id, Permission.key).join(Permission, Permission.id == RolePermission.permission_id)):
        grants.setdefault(rid, []).append(pkey)
    perms = db.scalars(select(Permission).order_by(Permission.key)).all()
    return {"roles": [{"key": r.key, "name": r.name, "scope": r.scope, "permissions": sorted(grants.get(r.id, []))} for r in roles],
            "permissions": [{"key": p.key, "description": p.description} for p in perms]}


class RoleGrants(Body):
    permissions: list[str]


@router.put("/roles/{role_key}")
def set_role_permissions(role_key: str, body: RoleGrants, ctx: Ctx = Depends(platform_ctx("platform.settings.manage"))) -> dict:
    db = ctx.db
    role = db.scalar(select(Role).where(Role.key == role_key))
    if not role:
        raise NotFound("Role not found")
    if role.key == "SUPER_ADMIN":
        raise AppError("Super Admin permissions cannot be reduced", code="protected_role", status_code=409)
    perms = {p.key: p for p in db.scalars(select(Permission))}
    unknown = set(body.permissions) - set(perms)
    if unknown:
        raise ValidationFailed(f"Unknown permissions: {', '.join(sorted(unknown))}")
    if role.scope == "organization" and any(p.startswith("platform.") for p in body.permissions):
        raise ValidationFailed("Client roles cannot be granted platform permissions")
    for rp in db.scalars(select(RolePermission).where(RolePermission.role_id == role.id)):
        db.delete(rp)
    db.flush()
    for key in set(body.permissions):
        db.add(RolePermission(role_id=role.id, permission_id=perms[key].id))
    audit(db, "role.permissions_changed", ctx=ctx, resource_type="role", resource_id=role.key, meta={"permissions": sorted(body.permissions)})
    db.commit()
    rbac.invalidate_cache()
    return {"key": role.key, "permissions": sorted(set(body.permissions))}


@router.get("/settings")
def list_settings(ctx: Ctx = Depends(platform_ctx("platform.settings.manage"))) -> dict:
    from app.ai.providers import ANTHROPIC_MODELS
    from app.ai.service import DEFAULT_PRICING
    from app.services.billing import stripe_enabled
    from app.services.notifications import smtp_configured

    s = get_settings()
    rows = ctx.db.scalars(select(SystemSetting).order_by(SystemSetting.key)).all()
    return {
        "items": [{"key": r.key, "value": r.value, "description": r.description, "updated_at": iso(r.updated_at)} for r in rows],
        "defaults": {"ai.model_pricing": DEFAULT_PRICING, "anthropic_models": ANTHROPIC_MODELS},
        # Presence only - secret values are never returned.
        "environment": {"env": s.env, "database": "postgresql" if s.is_postgres else "sqlite", "smtp_configured": smtp_configured(),
                        "stripe_configured": stripe_enabled(), "stripe_webhook_configured": bool(s.stripe_webhook_secret),
                        "sentry_configured": bool(s.sentry_dsn), "embedded_worker": s.embedded_worker,
                        "platform_ai_keys": {"anthropic": bool(s.anthropic_api_key), "openai": bool(s.openai_api_key), "gemini": bool(s.gemini_api_key)},
                        "oauth_clients": {g: bool(s.oauth_client(g)) for g in ("google", "microsoft", "salesforce", "linkedin")},
                        "vault_active_key_id": s.active_key_id, "vault_key_ids": sorted(s.key_ring())},
    }


class SettingBody(Body):
    value: object


EDITABLE_SETTINGS = {"billing.default_plan": str, "ai.default": dict, "ai.model_pricing": dict, "ai.anthropic_refusal_fallback": bool}


@router.put("/settings/{key}")
def put_setting(key: str, body: SettingBody, ctx: Ctx = Depends(platform_ctx("platform.settings.manage"))) -> dict:
    expected = EDITABLE_SETTINGS.get(key)
    if not expected:
        raise NotFound("Unknown setting")
    if not isinstance(body.value, expected):
        raise ValidationFailed(f"Setting '{key}' must be of type {expected.__name__}")
    if key == "ai.model_pricing":
        for model, price in body.value.items():  # type: ignore[union-attr]
            if not isinstance(price, dict) or not all(isinstance(price.get(k), (int, float)) and price[k] >= 0 for k in ("input", "output")):
                raise ValidationFailed(f"Price for '{model}' must be {{input: number, output: number}} in USD per million tokens")
    row = ctx.db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if not row:
        row = SystemSetting(key=key, value=body.value)
        ctx.db.add(row)
    row.value, row.updated_by = body.value, ctx.user.id
    audit(ctx.db, "setting.updated", ctx=ctx, resource_type="system_setting", resource_id=key)
    ctx.db.commit()
    return {"key": key, "value": row.value}
