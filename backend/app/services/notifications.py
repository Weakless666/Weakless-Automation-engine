"""Notification infrastructure: in-app inbox, email, and organization channels
(webhook, Slack, Discord, Telegram).

Channel secrets (webhook URLs, bot tokens) are stored envelope-encrypted and
every delivery attempt is logged in ``notification_deliveries``.
"""
from __future__ import annotations

import json
import logging
import smtplib
import uuid
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import http, vault
from app.core.errors import NodeError
from app.core.security import hmac_sha256_hex
from app.models import (
    Notification, NotificationChannel, NotificationDelivery, NotificationPreference, OrganizationMember, User,
)

log = logging.getLogger("wae.notifications")

EVENT_TYPES: dict[str, dict] = {
    "workflow.completed": {"label": "Workflow completed", "in_app": False, "staff_roles": ()},
    "workflow.failed": {"label": "Workflow failed", "in_app": True, "staff_roles": ("SUPER_ADMIN", "ADMIN", "AUTOMATION_ENGINEER", "SUPPORT")},
    "integration.disconnected": {"label": "Integration disconnected", "in_app": True, "staff_roles": ("SUPER_ADMIN", "ADMIN", "AUTOMATION_ENGINEER")},
    "usage.threshold": {"label": "Usage threshold reached", "in_app": True, "staff_roles": ("SUPER_ADMIN", "ADMIN", "MANAGER", "SALES")},
    "subscription.event": {"label": "Subscription event", "in_app": True, "staff_roles": ("SUPER_ADMIN", "ADMIN", "MANAGER", "SALES")},
    "security.event": {"label": "Security event", "in_app": True, "staff_roles": ("SUPER_ADMIN", "ADMIN")},
    "approval.requested": {"label": "Approval requested", "in_app": True, "staff_roles": ("SUPER_ADMIN", "ADMIN", "MANAGER", "AUTOMATION_ENGINEER")},
    "approval.completed": {"label": "Approval completed", "in_app": True, "staff_roles": ("AUTOMATION_ENGINEER",)},
    "workflow.notification": {"label": "Workflow notification", "in_app": True, "staff_roles": ()},
}
CLIENT_RECIPIENT_ROLES = {
    "workflow.completed": ("CLIENT_ADMIN",),
    "workflow.failed": ("CLIENT_ADMIN",),
    "integration.disconnected": ("CLIENT_ADMIN",),
    "usage.threshold": ("CLIENT_ADMIN",),
    "subscription.event": ("CLIENT_ADMIN",),
    "security.event": ("CLIENT_ADMIN",),
    "approval.requested": ("CLIENT_ADMIN",),
    "approval.completed": ("CLIENT_ADMIN",),
    "workflow.notification": ("CLIENT_ADMIN",),
}


def smtp_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and s.smtp_from)


def send_email(to: str, subject: str, body: str, html: str | None = None) -> None:
    """Send through the platform SMTP server. Raises ``NodeError`` on failure."""
    s = get_settings()
    if not smtp_configured():
        raise NodeError("Platform SMTP is not configured (WAE_SMTP_HOST / WAE_SMTP_FROM)", code="smtp_not_configured")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise NodeError(f"SMTP delivery failed: {type(exc).__name__}", code="smtp_error", retryable=True) from exc


def _recipients(db: Session, event_type: str, organization_id: uuid.UUID | None) -> list[User]:
    spec = EVENT_TYPES.get(event_type, {})
    users: dict[uuid.UUID, User] = {}
    staff_roles = spec.get("staff_roles", ())
    if staff_roles:
        for u in db.scalars(select(User).where(User.platform_role.in_(staff_roles), User.is_active.is_(True), User.deleted_at.is_(None))):
            users[u.id] = u
    if organization_id:
        roles = CLIENT_RECIPIENT_ROLES.get(event_type, ())
        if roles:
            q = (
                select(User).join(OrganizationMember, OrganizationMember.user_id == User.id)
                .where(OrganizationMember.organization_id == organization_id, OrganizationMember.role.in_(roles),
                       OrganizationMember.status == "active", User.is_active.is_(True))
            )
            for u in db.scalars(q):
                users[u.id] = u
    return list(users.values())


def channel_secret(channel: NotificationChannel) -> dict:
    if not channel.secret_envelope:
        return {}
    return vault.decrypt_json(channel.secret_envelope, vault.aad_for(channel.organization_id, f"notification_channel:{channel.id}"))


def deliver_to_channel(channel: NotificationChannel, event_type: str, title: str, body: str, data: dict) -> None:
    """Send one notification to an organization channel. Raises ``NodeError`` on failure."""
    secret = channel_secret(channel)
    text = f"{title}\n{body}".strip()
    if channel.kind == "webhook":
        payload = {"event": event_type, "title": title, "body": body, "data": data, "organization_id": str(channel.organization_id)}
        raw = json.dumps(payload, default=str).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "WAE-Notifications/1"}
        if secret.get("signing_secret"):
            headers["X-WAE-Signature"] = "sha256=" + hmac_sha256_hex(secret["signing_secret"], raw)
        resp = http.request("POST", secret.get("url", ""), content=raw, headers=headers, timeout=10)
        http.raise_for_status(resp, "Webhook")
    elif channel.kind == "slack":
        resp = http.request("POST", secret.get("url", ""), json={"text": text}, timeout=10)
        http.raise_for_status(resp, "Slack")
    elif channel.kind == "discord":
        resp = http.request("POST", secret.get("url", ""), json={"content": text[:1900]}, timeout=10)
        http.raise_for_status(resp, "Discord")
    elif channel.kind == "telegram":
        url = f"https://api.telegram.org/bot{secret.get('bot_token', '')}/sendMessage"
        resp = http.request("POST", url, json={"chat_id": (channel.config or {}).get("chat_id"), "text": text[:4000]}, timeout=10)
        http.raise_for_status(resp, "Telegram")
    elif channel.kind == "email":
        send_email((channel.config or {}).get("address", ""), f"[WAE] {title}", body or title)
    else:  # pragma: no cover - guarded by a check constraint
        raise NodeError(f"Unknown channel kind {channel.kind}")


def notify(
    db: Session,
    event_type: str,
    organization_id: uuid.UUID | None,
    *,
    title: str,
    body: str = "",
    severity: str = "info",
    data: dict | None = None,
    user_ids: list[uuid.UUID] | None = None,
) -> int:
    """Create in-app notifications and fan out to email + organization channels.

    Runs inside the caller's transaction; channel failures are recorded, never raised.
    Returns the number of in-app notifications created.
    """
    data = data or {}
    spec = EVENT_TYPES.get(event_type, {"in_app": True})
    if user_ids is not None:
        recipients = list(db.scalars(select(User).where(User.id.in_(user_ids))))
    else:
        recipients = _recipients(db, event_type, organization_id)
    prefs = {
        p.user_id: p
        for p in db.scalars(select(NotificationPreference).where(
            NotificationPreference.event_type == event_type,
            NotificationPreference.user_id.in_([u.id for u in recipients] or [uuid.uuid4()]),
        ))
    }
    created = 0
    for user in recipients:
        pref = prefs.get(user.id)
        if pref.in_app if pref else spec.get("in_app", True):
            db.add(Notification(organization_id=organization_id, user_id=user.id, event_type=event_type,
                                severity=severity, title=title, body=body, data=data))
            created += 1
        if pref and pref.email:
            _log_delivery(db, organization_id, None, "email", event_type, lambda u=user: send_email(u.email, f"[WAE] {title}", body or title))
    if organization_id:
        channels = db.scalars(select(NotificationChannel).where(
            NotificationChannel.organization_id == organization_id, NotificationChannel.is_active.is_(True)))
        for ch in channels:
            if ch.events and event_type not in ch.events:
                continue
            _log_delivery(db, organization_id, ch.id, ch.kind, event_type,
                          lambda c=ch: deliver_to_channel(c, event_type, title, body, data))
    db.flush()
    return created


def _log_delivery(db: Session, organization_id, channel_id, kind: str, event_type: str, send) -> None:
    status, error = "sent", None
    try:
        send()
    except NodeError as exc:
        status = "skipped" if exc.code == "smtp_not_configured" else "failed"
        error = exc.message
    except Exception as exc:  # noqa: BLE001
        log.exception("notification delivery failed")
        status, error = "failed", type(exc).__name__
    db.add(NotificationDelivery(organization_id=organization_id, channel_id=channel_id, channel_kind=kind,
                                event_type=event_type, status=status, error=error))
