"""Content calendar: proposals from agents or people, human approval, and the hand-off to publishing.

When an approved item is due (at its scheduled time, or at once if it has none) one of two things
happens. If the organization connected an account for the item's channel, the worker publishes it
natively (see ``app.services.publishing``) and records the link or the failure. Otherwise the item
emits the ``content.due`` application event; a workflow subscribed to it publishes the item or sends it
to a person to post by hand, and reports the outcome with the "Update Content Item" step.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db import utcnow
from app.models import CONTENT_CHANNELS, ContentItem

EDITABLE = ("idea", "draft", "awaiting_approval", "rejected", "failed")


def view(item: ContentItem) -> dict:
    def iso(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt else None

    return {"id": str(item.id), "title": item.title, "channel": item.channel, "body": item.body, "media_notes": item.media_notes,
            "hashtags": item.hashtags or [], "scheduled_for": iso(item.scheduled_for), "status": item.status,
            "created_by_agent_id": str(item.created_by_agent_id) if item.created_by_agent_id else None,
            "decided_at": iso(item.decided_at), "decision_note": item.decision_note, "published_at": iso(item.published_at),
            "published_url": item.published_url, "meta": item.meta or {}, "media": item.media or [], "created_at": iso(item.created_at), "updated_at": iso(item.updated_at)}


def _channel(value: str) -> str:
    channel = (value or "").strip().lower()
    if channel not in CONTENT_CHANNELS:
        raise ValidationFailed(f"Channel must be one of: {', '.join(CONTENT_CHANNELS)}")
    return channel


def media_refs(db: Session, organization_id: uuid.UUID, files: list | None) -> list[dict]:
    """File references for a post's media: images and videos of this organization, at most four."""
    from app.core.errors import AppError
    from app.services.files import get_file, ref

    out: list[dict] = []
    for f in files or []:
        if f in (None, ""):
            continue
        try:
            stored = get_file(db, organization_id, f)
        except AppError:
            raise ValidationFailed("A media file was not found (it may have expired)") from None
        if not stored.content_type.startswith(("image/", "video/")):
            raise ValidationFailed(f"'{stored.name}' is not an image or a video")
        if stored.expires_at is not None:  # media of a planned post must outlive the retention sweep
            stored.expires_at = None
        out.append(ref(stored))
    if len(out) > 4:
        raise ValidationFailed("A post can carry at most four media files")
    return out


def create_item(db: Session, organization_id: uuid.UUID, *, title: str, channel: str, body: str = "", media_notes: str = "", media: list | None = None,
                hashtags: list[str] | None = None, scheduled_for: datetime | None = None, status: str = "awaiting_approval",
                agent_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None, meta: dict | None = None) -> ContentItem:
    if not title.strip():
        raise ValidationFailed("A content item needs a title")
    if status not in ("idea", "draft", "awaiting_approval"):
        raise ValidationFailed("New content starts as an idea, a draft or awaiting approval")
    item = ContentItem(organization_id=organization_id, title=title.strip()[:300], channel=_channel(channel), body=body[:20000],
                       media_notes=media_notes[:5000], hashtags=[str(h).strip().lstrip("#")[:60] for h in (hashtags or []) if str(h).strip()][:30],
                       scheduled_for=scheduled_for, status=status, created_by_agent_id=agent_id, created_by=user_id, meta=meta or {},
                       media=media_refs(db, organization_id, media))
    db.add(item)
    db.flush()
    return item


def get_item(db: Session, organization_id: uuid.UUID, item_id) -> ContentItem:
    try:
        iid = item_id if isinstance(item_id, uuid.UUID) else uuid.UUID(str(item_id))
    except ValueError:
        raise NotFound("Content item not found")
    item = db.get(ContentItem, iid)
    if not item or item.organization_id != organization_id:
        raise NotFound("Content item not found")
    return item


def update_item(db: Session, item: ContentItem, changes: dict) -> ContentItem:
    if item.status not in EDITABLE:
        raise Conflict(f"A {item.status.replace('_', ' ')} item cannot be edited; reject or reschedule it first")
    for key in ("title", "body", "media_notes", "hashtags", "scheduled_for", "meta"):
        if key in changes and changes[key] is not None:
            setattr(item, key, changes[key])
    if changes.get("media") is not None:
        item.media = media_refs(db, item.organization_id, changes["media"])
    if changes.get("channel"):
        item.channel = _channel(changes["channel"])
    if changes.get("status") in ("idea", "draft", "awaiting_approval"):
        item.status = changes["status"]
    db.flush()
    return item


def decide(db: Session, item: ContentItem, *, approve: bool, user_id: uuid.UUID | None, note: str = "") -> ContentItem:
    if item.status not in ("awaiting_approval", "draft", "idea"):
        raise Conflict(f"Only items waiting for a decision can be approved or rejected (this one is {item.status.replace('_', ' ')})")
    item.status = "approved" if approve else "rejected"
    item.decided_by, item.decided_at, item.decision_note = user_id, utcnow(), note[:2000]
    item.due_emitted_at = None
    db.flush()
    if approve and (item.scheduled_for is None or item.scheduled_for <= utcnow()):
        from app.services.publishing import connected_integration

        # With an account connected the worker publishes it within seconds (a network call does not belong
        # in this request); without one, hand it to the publishing workflows now.
        if connected_integration(db, item.organization_id, item.channel) is None:
            emit_due(db, item)
    return item


def mark(db: Session, item: ContentItem, *, status: str, url: str | None = None, note: str = "") -> ContentItem:
    """Record the publishing outcome (from a workflow step or a person who posted by hand)."""
    if status not in ("published", "failed"):
        raise ValidationFailed("Status must be 'published' or 'failed'")
    if item.status not in ("approved", "failed", "published"):
        raise Conflict("Only approved content can be marked as published or failed")
    item.status = status
    if status == "published":
        item.published_at, item.published_url = utcnow(), (url or None)
    if note:
        item.decision_note = note[:2000]
    db.flush()
    return item


def emit_due(db: Session, item: ContentItem) -> None:
    from app.services.events import emit_app_event

    item.due_emitted_at = utcnow()
    db.flush()
    emit_app_event(db, item.organization_id, "content.due", view(item), source="system")


def release_due_content(db: Session, limit: int = 100) -> int:
    """Worker maintenance: hand approved items whose time has come to the publishing workflows.
    A compare-and-swap on ``due_emitted_at`` keeps two workers from releasing the same item."""
    from app.db import set_tenant

    now = utcnow()
    due = db.scalars(select(ContentItem).where(ContentItem.status == "approved", ContentItem.due_emitted_at.is_(None),
                                               ContentItem.scheduled_for.is_(None) | (ContentItem.scheduled_for <= now))
                     .order_by(ContentItem.scheduled_for).limit(limit)).all()
    released = 0
    for item in due:
        set_tenant(db, None, bypass_rls=True)
        claimed = db.execute(update(ContentItem).where(ContentItem.id == item.id, ContentItem.due_emitted_at.is_(None))
                             .values(due_emitted_at=now).execution_options(synchronize_session=False))
        db.commit()
        if claimed.rowcount != 1:
            continue
        db.refresh(item)
        from app.services.events import emit_app_event
        from app.services.publishing import connected_integration, publish_item

        set_tenant(db, item.organization_id)
        if connected_integration(db, item.organization_id, item.channel) is not None:
            publish_item(db, item)  # records published / failed itself
        else:
            emit_app_event(db, item.organization_id, "content.due", view(item), source="system")
        db.commit()
        released += 1
    set_tenant(db, None, bypass_rls=True)
    return released
