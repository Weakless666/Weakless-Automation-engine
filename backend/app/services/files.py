"""Stored files: uploads and files produced or consumed by workflows.

Workflow data carries *references* (``{file_id, name, content_type, size}``), never the bytes, so
step logs and execution state stay small. Limits: ``WAE_FILE_MAX_MB`` per file and
``WAE_ORG_STORAGE_MB`` per organization. Files made by workflows expire under the retention
policy (``files_days``); uploaded files stay until someone deletes them.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, undefer

from app.config import get_settings
from app.core.errors import NotFound, QuotaExceeded, ValidationFailed
from app.db import utcnow
from app.models import StoredFile


def clean_name(name: str) -> str:
    """A safe display/download name: no path parts or control characters."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).replace("\\", "/").split("/")[-1].strip().strip(".")
    return (name or "file")[:300]


def ref(f: StoredFile) -> dict:
    """What workflow steps pass around instead of the content."""
    return {"file_id": str(f.id), "name": f.name, "content_type": f.content_type, "size": f.size}


def view(f: StoredFile) -> dict:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {**ref(f), "id": str(f.id), "sha256": f.sha256, "source": f.source, "execution_id": str(f.execution_id) if f.execution_id else None,
            "expires_at": iso(f.expires_at), "created_at": iso(f.created_at)}


def storage_used(db: Session, organization_id: uuid.UUID) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(StoredFile.size), 0)).where(StoredFile.organization_id == organization_id)) or 0)


def workflow_file_expiry(db: Session) -> datetime | None:
    from app.services.retention import get_policy

    days = get_policy(db).get("files_days")
    return utcnow() + timedelta(days=days) if days else None


def save_file(db: Session, organization_id: uuid.UUID, name: str, content: bytes, *, content_type: str = "", source: str = "upload",
              execution_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None, expires_at: datetime | None = None) -> StoredFile:
    s = get_settings()
    if not content:
        raise ValidationFailed("The file is empty")
    if len(content) > s.file_max_mb * 1024 * 1024:
        raise ValidationFailed(f"Files can be at most {s.file_max_mb} MB")
    if storage_used(db, organization_id) + len(content) > s.org_storage_mb * 1024 * 1024:
        raise QuotaExceeded(f"The organization's file storage ({s.org_storage_mb} MB) is full. Delete files that are no longer needed.")
    ctype = (content_type or "application/octet-stream").split(";")[0].strip().lower()[:200] or "application/octet-stream"
    f = StoredFile(organization_id=organization_id, name=clean_name(name), content_type=ctype, size=len(content),
                   sha256=hashlib.sha256(content).hexdigest(), source=source, execution_id=execution_id, created_by=user_id,
                   expires_at=expires_at, data=bytes(content))
    db.add(f)
    db.flush()
    return f


def get_file(db: Session, organization_id: uuid.UUID, file_id, *, with_data: bool = False) -> StoredFile:
    """Tenant-checked lookup. ``file_id`` may be an id or a file reference from an earlier step."""
    if isinstance(file_id, dict):
        file_id = file_id.get("file_id") or file_id.get("id")
    try:
        fid = file_id if isinstance(file_id, uuid.UUID) else uuid.UUID(str(file_id))
    except (ValueError, TypeError):
        raise NotFound("File not found")
    q = select(StoredFile).where(StoredFile.id == fid, StoredFile.organization_id == organization_id)
    if with_data:
        q = q.options(undefer(StoredFile.data))
    f = db.scalar(q)
    if f is None or (f.expires_at and f.expires_at <= utcnow()):
        raise NotFound("File not found")
    return f


def purge_expired(db: Session, now: datetime, batch: int = 200) -> int:
    from sqlalchemy import delete

    total = 0
    for _ in range(20):
        ids = list(db.scalars(select(StoredFile.id).where(StoredFile.expires_at.is_not(None), StoredFile.expires_at < now).limit(batch)))
        if not ids:
            break
        db.execute(delete(StoredFile).where(StoredFile.id.in_(ids)).execution_options(synchronize_session=False))
        db.commit()
        total += len(ids)
    return total
