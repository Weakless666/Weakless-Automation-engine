"""Account links: password reset and invitations.

Tokens are random, single use, short lived, and stored only as a SHA-256 hash.
Issuing a new token of the same kind supersedes the previous unused one.

Rules that keep invitations from becoming an account-takeover path:
* an invitation only sets a password on an account that has none yet;
* for an account that already has a password, accepting requires that password;
* accepting activates the membership of the inviting organization only.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import AppError, Forbidden, ValidationFailed
from app.core.security import hash_password, random_token, sha256_hex, validate_password_strength, verify_password
from app.db import utcnow
from app.models import Organization, OrganizationMember, User, UserSession, UserToken

log = logging.getLogger("wae.accounts")

UNUSABLE_PASSWORD = "!unset"  # not a valid Argon2 hash: verification always fails
RESET_TTL = timedelta(hours=1)
INVITE_TTL = timedelta(days=7)
INVALID_LINK = "This link is invalid or has expired. Ask for a new one."


def has_password(user: User) -> bool:
    return not user.password_hash.startswith("!")


@dataclass
class IssuedLink:
    url: str
    expires_at: datetime
    email_sent: bool

    def view(self) -> dict:
        return {"url": self.url, "expires_at": self.expires_at.isoformat(), "email_sent": self.email_sent}


def _link(path: str, token: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/{path}?token={token}"


def _send(to: str, subject: str, body: str) -> bool:
    """Send through platform SMTP. Returns False (never raises) when it is not configured or fails."""
    from app.core.errors import NodeError
    from app.services.notifications import send_email, smtp_configured

    if not smtp_configured():
        return False
    try:
        send_email(to, subject, body)
        return True
    except NodeError as exc:
        log.warning("account email to %s failed: %s", to, exc.message)
        return False


def issue_token(db: Session, user: User, purpose: str, *, organization_id: uuid.UUID | None = None,
                created_by: uuid.UUID | None = None) -> tuple[str, UserToken]:
    now = utcnow()
    supersede = update(UserToken).where(UserToken.user_id == user.id, UserToken.purpose == purpose, UserToken.used_at.is_(None))
    if purpose == "invitation":
        supersede = supersede.where(UserToken.organization_id == organization_id if organization_id else UserToken.organization_id.is_(None))
    db.execute(supersede.values(used_at=now).execution_options(synchronize_session=False))
    raw = random_token(32)
    row = UserToken(user_id=user.id, organization_id=organization_id, purpose=purpose, token_hash=sha256_hex(raw),
                    expires_at=now + (RESET_TTL if purpose == "password_reset" else INVITE_TTL), created_by=created_by)
    db.add(row)
    db.flush()
    return raw, row


def find_token(db: Session, raw: str, purpose: str) -> tuple[UserToken, User]:
    """Look up a usable token without consuming it."""
    if not raw or len(raw) > 200:
        raise AppError(INVALID_LINK, code="invalid_token")
    row = db.scalar(select(UserToken).where(UserToken.token_hash == sha256_hex(raw)))
    if not row or row.purpose != purpose or row.used_at or row.expires_at <= utcnow():
        raise AppError(INVALID_LINK, code="invalid_token")
    user = db.get(User, row.user_id)
    if not user or not user.is_active or user.deleted_at:
        raise AppError(INVALID_LINK, code="invalid_token")
    return row, user


def _set_password(db: Session, user: User, password: str) -> None:
    problem = validate_password_strength(password)
    if problem:
        raise ValidationFailed(problem)
    user.password_hash = hash_password(password)
    user.failed_login_count, user.locked_until = 0, None
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))):
        s.revoked_at = utcnow()


# ----------------------------------------------------------------------------- password reset


def reset_link(db: Session, user: User, created_by: uuid.UUID | None = None) -> tuple[str, UserToken]:
    raw, row = issue_token(db, user, "password_reset", created_by=created_by)
    return _link("reset-password", raw), row


def request_password_reset(db: Session, email: str) -> None:
    """Email a reset link if the account exists. The caller always answers the same way."""
    user = db.scalar(select(User).where(User.email == email.lower().strip()))
    if not user or not user.is_active or user.deleted_at:
        return
    url, row = reset_link(db, user)
    _send(user.email, "Reset your Weakless password",
          f"Someone asked to reset the password for {user.email}.\n\nSet a new password here (the link works once and expires in one hour):\n{url}\n\n"
          "If you did not ask for this, you can ignore this email.")


def confirm_password_reset(db: Session, raw: str, new_password: str) -> User:
    row, user = find_token(db, raw, "password_reset")
    _set_password(db, user, new_password)
    row.used_at = utcnow()
    db.flush()
    return user


# ----------------------------------------------------------------------------- invitations


def invite(db: Session, user: User, *, organization: Organization | None, invited_by: User) -> IssuedLink:
    raw, row = issue_token(db, user, "invitation", organization_id=organization.id if organization else None, created_by=invited_by.id)
    url = _link("accept-invite", raw)
    where = organization.name if organization else "the Weakless team"
    inviter = invited_by.full_name or invited_by.email
    sent = _send(user.email, f"You're invited to {where} on Weakless",
                 f"{inviter} invited you to {where} on Weakless Automation Engine.\n\nAccept the invitation here (expires in 7 days):\n{url}\n")
    return IssuedLink(url=url, expires_at=row.expires_at, email_sent=sent)


def new_pending_user(db: Session, email: str, full_name: str, platform_role: str | None = None) -> User:
    user = User(email=email.lower().strip(), full_name=full_name or email.split("@")[0], password_hash=UNUSABLE_PASSWORD,
                platform_role=platform_role)
    db.add(user)
    db.flush()
    return user


def invitation_info(db: Session, raw: str) -> dict:
    row, user = find_token(db, raw, "invitation")
    org = db.get(Organization, row.organization_id) if row.organization_id else None
    inviter = db.get(User, row.created_by) if row.created_by else None
    member = None
    if org:
        member = db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == org.id, OrganizationMember.user_id == user.id))
        if not member or member.status != "invited" or org.deleted_at:
            raise AppError(INVALID_LINK, code="invalid_token")
    return {"email": user.email, "full_name": user.full_name, "organization_name": org.name if org else None,
            "role": member.role if member else user.platform_role, "invited_by": (inviter.full_name or inviter.email) if inviter else None,
            "needs_password": not has_password(user), "expires_at": row.expires_at.isoformat()}


def accept_invitation(db: Session, raw: str, password: str, full_name: str | None = None) -> User:
    row, user = find_token(db, raw, "invitation")
    member = None
    if row.organization_id:
        member = db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == row.organization_id,
                                                            OrganizationMember.user_id == user.id))
        if not member or member.status != "invited":
            raise AppError(INVALID_LINK, code="invalid_token")
    if has_password(user):
        # Existing account: prove it is yours; never overwrite the password from an invitation.
        if not verify_password(password, user.password_hash):
            raise Forbidden("Password is incorrect", code="invalid_credentials")
    else:
        _set_password(db, user, password)
        if full_name and full_name.strip():
            user.full_name = full_name.strip()[:200]
    if member:
        member.status = "active"
    row.used_at = utcnow()
    db.flush()
    return user
