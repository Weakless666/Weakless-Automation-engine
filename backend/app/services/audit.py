"""Audit trail for sensitive operations."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.redact import redact
from app.models import AuditLog


def audit(
    db: Session,
    action: str,
    *,
    ctx: Any = None,
    organization_id: uuid.UUID | None = None,
    resource_type: str = "",
    resource_id: Any = "",
    meta: dict | None = None,
    actor_email: str = "",
) -> AuditLog:
    """Record an audit entry in the caller's transaction (flushes, does not commit)."""
    org_id = organization_id
    if org_id is None and ctx is not None and getattr(ctx, "org", None) is not None:
        org_id = ctx.org.id
    entry = AuditLog(
        organization_id=org_id,
        actor_user_id=ctx.user.id if ctx is not None else None,
        actor_email=ctx.user.email if ctx is not None else actor_email,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id else "",
        meta=redact(meta or {}),
        ip=getattr(ctx, "ip", None),
        user_agent=getattr(ctx, "user_agent", None),
    )
    db.add(entry)
    db.flush()
    return entry
