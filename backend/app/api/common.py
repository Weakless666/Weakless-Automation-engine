"""Shared API helpers: pagination, tenant-safe lookups, serialization."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationFailed

T = TypeVar("T")


class Body(BaseModel):
    """Base for request bodies: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Paging:
    def __init__(self, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        self.limit, self.offset = limit, offset


def paginate(db: Session, query, paging: Paging, serialize: Callable[[Any], dict]) -> dict:
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    items = db.scalars(query.limit(paging.limit).offset(paging.offset)).all()
    return {"items": [serialize(i) for i in items], "total": total, "limit": paging.limit, "offset": paging.offset}


def parse_uuid(value: Any, what: str = "id") -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise NotFound(f"{what} not found")


def owned(db: Session, model: type[T], id: Any, organization_id: uuid.UUID, what: str = "Resource") -> T:
    """Fetch a tenant-owned row, verifying the tenant explicitly (IDOR guard that
    does not depend on Row Level Security). Soft-deleted rows are hidden."""
    obj = db.get(model, parse_uuid(id, what))
    if obj is None or getattr(obj, "organization_id", None) != organization_id or getattr(obj, "deleted_at", None):
        raise NotFound(f"{what} not found")
    return obj


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse_dt(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    try:
        from datetime import timezone

        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValidationFailed(f"'{name}' must be an ISO 8601 date")
