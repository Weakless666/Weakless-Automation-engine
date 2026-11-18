"""Native publishing: an approved content item goes straight to the social network through a
connected integration, and the outcome (link or reason for failure) is written back to the calendar.

Each organization connects a channel to one of its integrations (``organizations.settings["publishing"]``).
Channels without a connection keep the hand-off: the ``content.due`` event starts a workflow.
Publishing is never retried automatically - a post that may already be live must not be sent twice;
a failed item can be published again by a person.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.errors import AppError, Conflict, NodeError, ValidationFailed
from app.models import CONTENT_CHANNELS, ContentItem, Integration, Organization

log = logging.getLogger("wae.publishing")

# channel -> providers that can publish to it natively
CHANNEL_PROVIDERS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin",),
    "facebook": ("meta",),
    "instagram": ("meta",),
    "x": ("x",),
    "other": ("telegram",),
}
SOCIAL_PROVIDERS = ("linkedin", "meta", "x", "telegram")
MAX_MEDIA = 4


def _connections(db: Session, organization_id: uuid.UUID) -> dict:
    # Read the column, not a cached object: a long-lived session must see a connection made a moment ago.
    settings = db.scalar(select(Organization.settings).where(Organization.id == organization_id)) or {}
    return dict(settings.get("publishing") or {})


def connected_integration(db: Session, organization_id: uuid.UUID, channel: str) -> Integration | None:
    """The integration this channel publishes through, if one is set and still exists."""
    conns = _connections(db, organization_id)
    iid = (conns.get(channel) or {}).get("integration_id")
    if not iid:
        return None
    try:
        integration = db.get(Integration, uuid.UUID(str(iid)))
    except ValueError:
        return None
    if not integration or integration.organization_id != organization_id or integration.deleted_at:
        return None
    return integration


def channels(db: Session, organization_id: uuid.UUID) -> list[dict]:
    out = []
    for channel in CONTENT_CHANNELS:
        providers = CHANNEL_PROVIDERS.get(channel, ())
        integration = connected_integration(db, organization_id, channel) if providers else None
        out.append({"channel": channel, "native": bool(providers), "providers": list(providers),
                    "integration_id": str(integration.id) if integration else None,
                    "integration_name": integration.name if integration else None,
                    "integration_status": integration.status if integration else None})
    return out


def set_connection(db: Session, organization_id: uuid.UUID, channel: str, integration_id: str | None) -> None:
    providers = CHANNEL_PROVIDERS.get(channel)
    if not providers:
        raise ValidationFailed(f"'{channel}' has no native publishing; it is handed to a workflow or posted by hand")
    conns = _connections(db, organization_id)
    org = db.get(Organization, organization_id, populate_existing=True)
    if integration_id:
        from app.integrations.service import get_integration

        try:
            integration = get_integration(db, organization_id, integration_id)
        except NodeError as exc:
            raise ValidationFailed(exc.message) from exc
        if integration.provider_key not in providers:
            raise ValidationFailed(f"{channel.title()} publishes through {' or '.join(providers)}, not {integration.provider_key}")
        conns[channel] = {"integration_id": str(integration.id)}
    else:
        conns.pop(channel, None)
    org.settings = {**(org.settings or {}), "publishing": conns}
    flag_modified(org, "settings")
    db.flush()


def post_text(item: ContentItem) -> str:
    tags = " ".join("#" + str(h).lstrip("#").replace(" ", "") for h in (item.hashtags or []) if str(h).strip())
    body = (item.body or "").strip()
    if tags and not all(t.lower() in body.lower() for t in tags.split()):
        body = f"{body}\n\n{tags}".strip()
    return body or item.title


def load_media(db: Session, organization_id: uuid.UUID, refs: list) -> list[dict]:
    """Stored files as publishable media: bytes for uploads, plus a signed public link for networks that fetch."""
    from app.services.files import get_file, public_url

    out = []
    for r in (refs or [])[:MAX_MEDIA]:
        if r in (None, ""):
            continue
        try:
            f = get_file(db, organization_id, r, with_data=True)
        except AppError:
            raise NodeError("A media file of this post no longer exists (it may have expired)", code="file_not_found") from None
        out.append({"name": f.name, "content_type": f.content_type, "content": f.data, "url": public_url(f)})
    return out


def publish_item(db: Session, item: ContentItem) -> ContentItem:
    """Publish one approved (or previously failed) item now and record what happened. Never raises for
    provider failures: the item becomes ``failed`` with the reason."""
    from app.integrations.service import run_action
    from app.services.content import mark
    from app.services.events import emit_app_event

    if item.status not in ("approved", "failed"):
        raise Conflict("Only approved content can be published")
    integration = connected_integration(db, item.organization_id, item.channel)
    if integration is None:
        raise Conflict(f"No account is connected for {item.channel}. Connect one under Content > Publishing, or mark the item as published by hand.")
    try:
        result = run_action(db, item.organization_id, integration.id, "publish_post", {
            "channel": item.channel, "title": item.title, "text": post_text(item),
            "link": str((item.meta or {}).get("link") or ""), "media": load_media(db, item.organization_id, item.media)})
    except (NodeError, AppError) as exc:
        detail = getattr(exc, "details", None)
        reason = exc.message + (f" ({str(detail)[:300]})" if detail else "")
        mark(db, item, status="failed", note=f"Publishing to {integration.name} failed: {reason}")
        item.meta = {**(item.meta or {}), "publish": {"integration_id": str(integration.id), "error": exc.code}}
        db.flush()
        _notify_failure(db, item, reason)
        emit_app_event(db, item.organization_id, "content.failed", _event(item), source="system")
        return item
    mark(db, item, status="published", url=result.get("url") or None, note=f"Published through {integration.name}")
    item.meta = {**(item.meta or {}), "publish": {"integration_id": str(integration.id), "provider": integration.provider_key,
                                                  "post_id": result.get("post_id") or ""}}
    db.flush()
    emit_app_event(db, item.organization_id, "content.published", _event(item), source="system")
    return item


def _event(item: ContentItem) -> dict:
    from app.services.content import view

    return view(item)


def _notify_failure(db: Session, item: ContentItem, reason: str) -> None:
    from app.services.notifications import notify

    try:
        notify(db, "content.publish_failed", item.organization_id, title=f"Post was not published: {item.title}", body=reason[:500],
               severity="error", data={"content_item_id": str(item.id), "channel": item.channel})
    except Exception:  # noqa: BLE001 - a notification problem must not hide the publishing outcome
        log.exception("could not notify about a failed post")
