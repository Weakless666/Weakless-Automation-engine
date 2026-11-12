"""Authentication, sessions, staff users, roles and system settings."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import rbac
from app.api import schemas
from app.api.common import ApiRoute
from app.api.common import Body, Paging, iso, paginate, parse_uuid
from app.config import get_settings
from app.core.errors import AppError, Conflict, Forbidden, NotFound, RateLimited, Unauthorized, ValidationFailed
from app.core.ratelimit import limiter
from app.core.security import (
    SESSION_COOKIE, create_session_token, hash_password, random_token, session_expiry, validate_password_strength, verify_password,
)
from app.db import get_db, new_session, utcnow
from app.deps import Ctx, client_ip, platform_ctx, user_ctx
from app.models import (
    PLATFORM_ROLES, LoginHistory, Organization, OrganizationMember, Permission, Role, RolePermission, SystemSetting, User, UserSession,
)
from app.services import accounts, mfa
from app.services.audit import audit
from app.services.notifications import notify

router = APIRouter(route_class=ApiRoute, tags=["auth"])
MAX_FAILED_LOGINS = 8
LOCK_MINUTES = 15
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


class LoginBody(Body):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=200)


def user_view(u: User) -> dict:
    return {"id": str(u.id), "email": u.email, "full_name": u.full_name, "platform_role": u.platform_role, "is_active": u.is_active,
            "invitation_pending": not accounts.has_password(u), "mfa_enabled": mfa.enabled(u),
            "last_login_at": iso(u.last_login_at), "created_at": iso(u.created_at)}


@router.post("/auth/login", response_model=schemas.LoginResult)
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
    if not accounts.has_password(user):
        verify_password(body.password, _DUMMY_HASH)  # invited, not accepted yet: same timing as a wrong password
        fail("invitation_pending")
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
    return _complete_login(db, user, ip, ua, response)


def _complete_login(db: Session, user: User, ip: str | None, ua: str, response: Response, reason: str | None = None) -> dict:
    """After the password (or an invitation) is verified: a session, or the two-factor step first.

    The failed-attempt counter is deliberately *not* reset here, so knowing the password does not
    buy unlimited guesses at the code."""
    if mfa.enabled(user):
        db.commit()
        return {"mfa_required": True, "mfa_token": mfa.challenge_token(user, "verify"), "email": user.email}
    if mfa.required_for(db, user):
        db.commit()
        return {"mfa_setup_required": True, "mfa_token": mfa.challenge_token(user, "setup"), "email": user.email}
    return _start_session(db, user, ip, ua, response, reason=reason)


def _start_session(db: Session, user: User, ip: str | None, ua: str, response: Response, reason: str | None = None) -> dict:
    s = get_settings()
    user.failed_login_count, user.locked_until, user.last_login_at = 0, None, utcnow()
    sess = UserSession(user_id=user.id, ip=ip, user_agent=ua, csrf_token=random_token(24), expires_at=session_expiry())
    db.add(sess)
    db.add(LoginHistory(user_id=user.id, email=user.email, success=True, reason=reason, ip=ip, user_agent=ua))
    db.flush()
    token = create_session_token(user.id, sess.id, sess.expires_at)
    db.commit()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=s.cookie_secure, samesite="lax",
                        max_age=s.session_hours * 3600, path="/")
    # `token` supports non-browser API clients; the browser app relies on the HttpOnly cookie + CSRF header.
    return {"token": token, "csrf_token": sess.csrf_token, "expires_at": iso(sess.expires_at), "user": user_view(user)}


def _public_limit(request: Request, bucket: str, email: str | None = None) -> None:
    s = get_settings()
    ip = client_ip(request)
    if not limiter.allow(f"{bucket}:{ip}", s.auth_rate_limit_per_minute) or (
            email and not limiter.allow(f"{bucket}-email:{email}", s.auth_rate_limit_per_minute)):
        raise RateLimited("Too many attempts. Try again in a minute.")


# ----------------------------------------------------------------------------- password reset


class ResetRequestBody(Body):
    email: EmailStr


@router.post("/auth/password-reset/request", response_model=schemas.ResetRequested, status_code=202)
def password_reset_request(body: ResetRequestBody, request: Request, db: Session = Depends(get_db)) -> dict:
    """Always answers the same way, whether or not the account exists."""
    email = body.email.lower().strip()
    _public_limit(request, "reset", email)
    accounts.request_password_reset(db, email)
    user = db.scalar(select(User).where(User.email == email))
    if user:
        audit(db, "auth.password_reset_requested", resource_type="user", resource_id=user.id, actor_email=email, meta={"ip": client_ip(request)})
    db.commit()
    from app.services.notifications import smtp_configured

    return {"status": "accepted", "email_delivery": smtp_configured()}


class ResetConfirmBody(Body):
    token: str = Field(..., min_length=10, max_length=200)
    new_password: str = Field(..., max_length=200)


@router.post("/auth/password-reset/confirm", response_model=schemas.Ok)
def password_reset_confirm(body: ResetConfirmBody, request: Request, db: Session = Depends(get_db)) -> dict:
    _public_limit(request, "reset-confirm")
    user = accounts.confirm_password_reset(db, body.token, body.new_password)
    audit(db, "auth.password_reset_completed", resource_type="user", resource_id=user.id, actor_email=user.email, meta={"ip": client_ip(request)})
    db.commit()
    return {"ok": True}


# ----------------------------------------------------------------------------- invitations


class TokenBody(Body):
    token: str = Field(..., min_length=10, max_length=200)


@router.post("/auth/invitations/inspect", response_model=schemas.InvitationInfo)
def invitation_inspect(body: TokenBody, request: Request, db: Session = Depends(get_db)) -> dict:
    _public_limit(request, "invite")
    return accounts.invitation_info(db, body.token)


class AcceptBody(Body):
    token: str = Field(..., min_length=10, max_length=200)
    password: str = Field(..., min_length=1, max_length=200)
    full_name: str | None = Field(None, max_length=200)


@router.post("/auth/invitations/accept", response_model=schemas.LoginResult)
def invitation_accept(body: AcceptBody, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    """Accept an invitation and start a session."""
    _public_limit(request, "invite")
    org_id = accounts.find_token(db, body.token, "invitation")[0].organization_id
    user = accounts.accept_invitation(db, body.token, body.password, body.full_name)
    audit(db, "auth.invitation_accepted", organization_id=org_id, resource_type="user", resource_id=user.id, actor_email=user.email,
          meta={"ip": client_ip(request)})
    return _complete_login(db, user, client_ip(request), request.headers.get("user-agent", "")[:400], response, reason="invitation")


# ----------------------------------------------------------------------------- two-factor sign-in


class MfaTokenBody(Body):
    mfa_token: str = Field(..., min_length=20, max_length=2000)


class MfaCodeBody(MfaTokenBody):
    code: str = Field(..., min_length=1, max_length=40)


def _mfa_failure(db: Session, user: User, request: Request) -> None:
    user.failed_login_count += 1
    db.add(LoginHistory(user_id=user.id, email=user.email, success=False, reason="bad_mfa_code", ip=client_ip(request),
                        user_agent=request.headers.get("user-agent", "")[:400]))
    if user.failed_login_count >= MAX_FAILED_LOGINS:
        user.locked_until = utcnow() + timedelta(minutes=LOCK_MINUTES)
        user.failed_login_count = 0
        audit(db, "auth.account_locked", resource_type="user", resource_id=user.id, actor_email=user.email, meta={"ip": client_ip(request), "step": "mfa"})
        notify(db, "security.event", None, title="Account temporarily locked", body=f"{user.email} was locked after repeated wrong authentication codes.",
               severity="warning", data={"email": user.email})
    db.commit()
    raise Unauthorized("That code is not valid. Use the newest code from your authenticator app.", code="invalid_mfa_code")


@router.post("/auth/mfa/verify", response_model=schemas.LoginResult)
def mfa_verify(body: MfaCodeBody, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    """Second step of sign-in: an authenticator code or a recovery code."""
    _public_limit(request, "mfa")
    user = mfa.user_from_challenge(db, body.mfa_token, "verify")
    if user.locked_until and user.locked_until > utcnow():
        raise Unauthorized("Too many attempts. Try again in a few minutes.", code="locked")
    method = mfa.check_code(db, user, body.code)
    if not method:
        _mfa_failure(db, user, request)
    if method == "recovery":
        left = len(user.mfa_recovery_hashes or [])
        audit(db, "auth.mfa_recovery_code_used", resource_type="user", resource_id=user.id, actor_email=user.email, meta={"remaining": left})
        notify(db, "security.event", None, title="A recovery code was used to sign in", severity="warning",
               body=f"{left} recovery codes remain. If this was not you, change your password and reset two-factor sign-in.", user_ids=[user.id])
    return _start_session(db, user, client_ip(request), request.headers.get("user-agent", "")[:400], response, reason=f"mfa_{method}")


@router.post("/auth/mfa/setup", response_model=schemas.MfaStart)
def mfa_setup_during_login(body: MfaTokenBody, request: Request, db: Session = Depends(get_db)) -> dict:
    """Required set-up at sign-in (staff without two-factor sign-in yet)."""
    _public_limit(request, "mfa")
    user = mfa.user_from_challenge(db, body.mfa_token, "setup")
    if mfa.enabled(user):
        raise Conflict("Two-factor sign-in is already set up. Sign in again.")
    started = mfa.begin_enrollment(db, user)
    db.commit()
    return started


@router.post("/auth/mfa/setup/confirm", response_model=schemas.SessionWithRecoveryCodes)
def mfa_setup_confirm_during_login(body: MfaCodeBody, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    _public_limit(request, "mfa")
    user = mfa.user_from_challenge(db, body.mfa_token, "setup")
    codes = mfa.confirm_enrollment(db, user, body.code)
    audit(db, "auth.mfa_enabled", resource_type="user", resource_id=user.id, actor_email=user.email, meta={"during": "sign-in"})
    result = _start_session(db, user, client_ip(request), request.headers.get("user-agent", "")[:400], response, reason="mfa_setup")
    return {**result, "recovery_codes": codes}


@router.get("/auth/mfa", response_model=schemas.MfaStatus)
def mfa_status(ctx: Ctx = Depends(user_ctx)) -> dict:
    return mfa.status(ctx.db, ctx.user)


@router.post("/auth/mfa/enroll", response_model=schemas.MfaStart)
def mfa_enroll(ctx: Ctx = Depends(user_ctx)) -> dict:
    if mfa.enabled(ctx.user):
        raise Conflict("Two-factor sign-in is already on")
    started = mfa.begin_enrollment(ctx.db, ctx.user)
    ctx.db.commit()
    return started


class CodeBody(Body):
    code: str = Field(..., min_length=1, max_length=40)


@router.post("/auth/mfa/enroll/confirm", response_model=schemas.MfaEnrolled)
def mfa_enroll_confirm(body: CodeBody, ctx: Ctx = Depends(user_ctx)) -> dict:
    codes = mfa.confirm_enrollment(ctx.db, ctx.user, body.code)
    audit(ctx.db, "auth.mfa_enabled", ctx=ctx, resource_type="user", resource_id=ctx.user.id)
    ctx.db.commit()
    return {"recovery_codes": codes, "status": mfa.status(ctx.db, ctx.user)}


def _verify_current_code(ctx: Ctx, code: str) -> None:
    if not mfa.check_code(ctx.db, ctx.user, code):
        ctx.db.commit()
        raise Forbidden("That code is not valid", code="invalid_mfa_code")


@router.post("/auth/mfa/recovery-codes", response_model=schemas.RecoveryCodesOut)
def mfa_new_recovery_codes(body: CodeBody, ctx: Ctx = Depends(user_ctx)) -> dict:
    """Replace all recovery codes (the old ones stop working). Requires a current code."""
    if not mfa.enabled(ctx.user):
        raise Conflict("Two-factor sign-in is not on")
    _verify_current_code(ctx, body.code)
    codes = mfa.regenerate_recovery_codes(ctx.db, ctx.user)
    audit(ctx.db, "auth.mfa_recovery_codes_regenerated", ctx=ctx, resource_type="user", resource_id=ctx.user.id)
    ctx.db.commit()
    return {"recovery_codes": codes}


class DisableMfaBody(Body):
    password: str = Field(..., min_length=1, max_length=200)
    code: str = Field(..., min_length=1, max_length=40)


@router.post("/auth/mfa/disable", response_model=schemas.MfaStatus)
def mfa_disable(body: DisableMfaBody, ctx: Ctx = Depends(user_ctx)) -> dict:
    if not mfa.enabled(ctx.user):
        raise Conflict("Two-factor sign-in is not on")
    if mfa.required_for(ctx.db, ctx.user):
        raise Forbidden("Two-factor sign-in is required for Weakless staff", code="mfa_required")
    if not verify_password(body.password, ctx.user.password_hash):
        raise Forbidden("Password is incorrect", code="invalid_credentials")
    _verify_current_code(ctx, body.code)
    mfa.disable(ctx.db, ctx.user)
    audit(ctx.db, "auth.mfa_disabled", ctx=ctx, resource_type="user", resource_id=ctx.user.id)
    ctx.db.commit()
    return mfa.status(ctx.db, ctx.user)


@router.post("/auth/logout", response_model=schemas.Ok)
def logout(response: Response, ctx: Ctx = Depends(user_ctx)) -> dict:
    ctx.session.revoked_at = utcnow()
    ctx.db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me", response_model=schemas.MeOut)
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
            "organizations": org_rows, "csrf_token": ctx.session.csrf_token, "mfa": mfa.status(db, ctx.user)}


class PasswordBody(Body):
    current_password: str
    new_password: str = Field(..., max_length=200)


@router.post("/auth/change-password", response_model=schemas.Ok)
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


@router.patch("/auth/me", response_model=schemas.UserOut)
def update_profile(body: ProfileBody, ctx: Ctx = Depends(user_ctx)) -> dict:
    ctx.user.full_name = body.full_name
    ctx.db.commit()
    return user_view(ctx.user)


@router.get("/auth/sessions", response_model=schemas.Items[schemas.SessionInfo])
def sessions(ctx: Ctx = Depends(user_ctx)) -> dict:
    rows = ctx.db.scalars(select(UserSession).where(UserSession.user_id == ctx.user.id, UserSession.revoked_at.is_(None),
                                                    UserSession.expires_at > utcnow()).order_by(UserSession.created_at.desc())).all()
    return {"items": [{"id": str(s.id), "ip": s.ip, "user_agent": s.user_agent, "created_at": iso(s.created_at),
                       "last_seen_at": iso(s.last_seen_at), "expires_at": iso(s.expires_at), "current": s.id == ctx.session.id} for s in rows]}


@router.delete("/auth/sessions/{session_id}", response_model=schemas.Ok)
def revoke_session(session_id: str, ctx: Ctx = Depends(user_ctx)) -> dict:
    s = ctx.db.get(UserSession, parse_uuid(session_id, "Session"))
    if not s or s.user_id != ctx.user.id:
        raise NotFound("Session not found")
    s.revoked_at = utcnow()
    audit(ctx.db, "auth.session_revoked", ctx=ctx, resource_type="session", resource_id=s.id)
    ctx.db.commit()
    return {"ok": True}


@router.get("/auth/login-history", response_model=schemas.Page[schemas.LoginEntry])
def login_history(paging: Paging = Depends(), ctx: Ctx = Depends(user_ctx)) -> dict:
    q = select(LoginHistory).where(LoginHistory.user_id == ctx.user.id).order_by(LoginHistory.created_at.desc())
    return paginate(ctx.db, q, paging, lambda h: {"id": str(h.id), "success": h.success, "reason": h.reason, "ip": h.ip,
                                                  "user_agent": h.user_agent, "created_at": iso(h.created_at)})


# ----------------------------------------------------------------------------- staff users


class StaffCreate(Body):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=200)
    password: str | None = Field(None, max_length=200, description="Omit to send an invitation instead")
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


@router.get("/users", response_model=schemas.Page[schemas.UserOut])
def list_staff(paging: Paging = Depends(), ctx: Ctx = Depends(platform_ctx("platform.users.manage"))) -> dict:
    q = select(User).where(User.platform_role.is_not(None), User.deleted_at.is_(None)).order_by(User.email)
    return paginate(ctx.db, q, paging, user_view)


@router.post("/users", response_model=schemas.StaffCreated, status_code=201)
def create_staff(body: StaffCreate, ctx: Ctx = Depends(platform_ctx("platform.users.manage"))) -> dict:
    _guard_role(ctx, body.platform_role)
    email = body.email.lower().strip()
    if ctx.db.scalar(select(User.id).where(User.email == email)):
        raise Conflict("A user with this email already exists")
    invitation = None
    if body.password:
        problem = validate_password_strength(body.password)
        if problem:
            raise ValidationFailed(problem)
        u = User(email=email, full_name=body.full_name, password_hash=hash_password(body.password), platform_role=body.platform_role)
        ctx.db.add(u)
        ctx.db.flush()
    else:
        u = accounts.new_pending_user(ctx.db, email, body.full_name, platform_role=body.platform_role)
        invitation = accounts.invite(ctx.db, u, organization=None, invited_by=ctx.user)
    audit(ctx.db, "user.created", ctx=ctx, resource_type="user", resource_id=u.id,
          meta={"email": email, "platform_role": body.platform_role, "invited": invitation is not None})
    ctx.db.commit()
    return {**user_view(u), "invitation": invitation.view() if invitation else None}


@router.post("/users/{user_id}/invitation", response_model=schemas.IssuedLinkOut)
def resend_staff_invitation(user_id: str, ctx: Ctx = Depends(platform_ctx("platform.users.manage"))) -> dict:
    u = ctx.db.get(User, parse_uuid(user_id, "User"))
    if not u or u.deleted_at or not u.platform_role:
        raise NotFound("User not found")
    _guard_role(ctx, None, u)
    if accounts.has_password(u):
        raise Conflict("This person has already accepted their invitation")
    invitation = accounts.invite(ctx.db, u, organization=None, invited_by=ctx.user)
    audit(ctx.db, "user.invitation_resent", ctx=ctx, resource_type="user", resource_id=u.id)
    ctx.db.commit()
    return invitation.view()


@router.post("/users/{user_id}/mfa-reset", response_model=schemas.UserOut)
def reset_user_mfa(user_id: str, ctx: Ctx = Depends(platform_ctx("platform.users.manage"))) -> dict:
    """For a lost phone: switch off someone's two-factor sign-in and sign them out everywhere.
    Staff who are required to use it set it up again at their next sign-in."""
    u = ctx.db.get(User, parse_uuid(user_id, "User"))
    if not u or u.deleted_at:
        raise NotFound("User not found")
    _guard_role(ctx, None, u)
    if u.id == ctx.user.id:
        raise AppError("Use your own security settings to change your two-factor sign-in", code="self_modification", status_code=409)
    if not mfa.enabled(u):
        raise Conflict("This person does not use two-factor sign-in")
    mfa.disable(ctx.db, u)
    for s in ctx.db.scalars(select(UserSession).where(UserSession.user_id == u.id, UserSession.revoked_at.is_(None))):
        s.revoked_at = utcnow()
    audit(ctx.db, "user.mfa_reset", ctx=ctx, resource_type="user", resource_id=u.id, meta={"email": u.email})
    notify(ctx.db, "security.event", None, title="Your two-factor sign-in was reset", severity="warning",
           body=f"{ctx.user.email} reset it. If you did not ask for this, contact your administrator.", user_ids=[u.id])
    ctx.db.commit()
    return user_view(u)


@router.post("/users/{user_id}/reset-link", response_model=schemas.IssuedLinkOut)
def issue_reset_link(user_id: str, ctx: Ctx = Depends(platform_ctx("platform.users.manage"))) -> dict:
    """Staff-issued password reset link for any account, for when platform email is not configured
    or the person cannot reach their inbox. Single use, one hour, audited."""
    u = ctx.db.get(User, parse_uuid(user_id, "User"))
    if not u or u.deleted_at:
        raise NotFound("User not found")
    _guard_role(ctx, None, u)
    if not u.is_active:
        raise Conflict("Enable the account before issuing a reset link")
    url, row = accounts.reset_link(ctx.db, u, created_by=ctx.user.id)
    audit(ctx.db, "user.reset_link_issued", ctx=ctx, resource_type="user", resource_id=u.id, meta={"email": u.email})
    ctx.db.commit()
    return {"url": url, "expires_at": iso(row.expires_at)}


@router.patch("/users/{user_id}", response_model=schemas.UserOut)
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
        # A password set by an admin replaces any outstanding invitation or reset link.
        from app.models import UserToken

        ctx.db.query(UserToken).filter(UserToken.user_id == u.id, UserToken.used_at.is_(None)).update({"used_at": utcnow()})
    if changes.get("is_active") is False or changes.get("password_reset") or "platform_role" in changes:
        for s in ctx.db.scalars(select(UserSession).where(UserSession.user_id == u.id, UserSession.revoked_at.is_(None))):
            s.revoked_at = utcnow()
    audit(ctx.db, "user.updated", ctx=ctx, resource_type="user", resource_id=u.id, meta=changes)
    ctx.db.commit()
    return user_view(u)


# ----------------------------------------------------------------------------- roles & settings


@router.get("/roles", response_model=schemas.RolesOut)
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


@router.put("/roles/{role_key}", response_model=schemas.RoleGrantsOut)
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


@router.get("/settings", response_model=schemas.SettingsOut)
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


EDITABLE_SETTINGS = {"billing.default_plan": str, "ai.default": dict, "ai.model_pricing": dict, "ai.anthropic_refusal_fallback": bool,
                     "security.staff_mfa_required": bool, "data.retention": dict}


@router.put("/settings/{key}", response_model=schemas.SettingOut)
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
    if key == "data.retention":
        from app.services.retention import validate_policy

        try:
            body.value = validate_policy(body.value)
        except ValueError as exc:
            raise ValidationFailed(str(exc))
    row = ctx.db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if not row:
        row = SystemSetting(key=key, value=body.value)
        ctx.db.add(row)
    row.value, row.updated_by = body.value, ctx.user.id
    audit(ctx.db, "setting.updated", ctx=ctx, resource_type="system_setting", resource_id=key)
    ctx.db.commit()
    return {"key": key, "value": row.value}


@router.post("/retention/run", response_model=schemas.RetentionRun)
def run_retention_now(ctx: Ctx = Depends(platform_ctx("platform.settings.manage"))) -> dict:
    """Apply the data retention policy immediately (workers also apply it hourly)."""
    from app.services.retention import get_policy, run_retention

    with new_session(bypass_rls=True) as db:  # the sweep spans every tenant
        policy = get_policy(db)
        result = run_retention(db)
    audit(ctx.db, "retention.run", ctx=ctx, resource_type="system_setting", resource_id="data.retention", meta=result)
    ctx.db.commit()
    return {"policy": policy, "result": result}
