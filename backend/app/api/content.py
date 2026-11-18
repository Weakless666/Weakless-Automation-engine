"""Content calendar and AI teams."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import func, select

from app.api import schemas
from app.api.common import ApiRoute, Body, iso, parse_dt
from app.core.errors import Conflict
from app.db import utcnow
from app.deps import Ctx, org_ctx
from app.models import CONTENT_CHANNELS, AIAgent, AIAgentExecution, ContentItem
from app.services import content as svc
from app.services.audit import audit

router = APIRouter(route_class=ApiRoute, tags=["content"])
Channel = Literal["linkedin", "instagram", "facebook", "tiktok", "youtube", "x", "blog", "newsletter", "email", "other"]
assert set(Channel.__args__) == set(CONTENT_CHANNELS)


@router.get("/content", response_model=schemas.Items[schemas.ContentItemOut])
def list_content(start: str | None = None, end: str | None = None, status: str | None = None, channel: str | None = None,
                 ctx: Ctx = Depends(org_ctx("content.read"))) -> dict:
    """Calendar entries. With ``start``/``end`` (ISO dates): items scheduled in that range plus unscheduled
    ones; without: the 200 most recently changed."""
    q = select(ContentItem).where(ContentItem.organization_id == ctx.org_id)
    if status:
        q = q.where(ContentItem.status == status)
    if channel:
        q = q.where(ContentItem.channel == channel)
    s, e = parse_dt(start, "start"), parse_dt(end, "end")
    if s or e:
        cond = ContentItem.scheduled_for.is_not(None)
        if s:
            cond &= ContentItem.scheduled_for >= s
        if e:
            cond &= ContentItem.scheduled_for <= e
        q = q.where(cond | ContentItem.scheduled_for.is_(None)).order_by(ContentItem.scheduled_for.asc().nulls_last())
    else:
        q = q.order_by(ContentItem.updated_at.desc())
    return {"items": [svc.view(i) for i in ctx.db.scalars(q.limit(200))]}


class ContentBody(Body):
    title: str = Field(..., min_length=1, max_length=300)
    channel: Channel
    body: str = Field("", max_length=20000)
    media_notes: str = Field("", max_length=5000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    scheduled_for: datetime | None = None
    status: Literal["idea", "draft", "awaiting_approval"] = "draft"
    meta: dict[str, Any] = Field(default_factory=dict)
    media: list[str] = Field(default_factory=list, max_length=4, description="Ids of stored image or video files to post with the text")


class ContentPatch(Body):
    title: str | None = Field(None, min_length=1, max_length=300)
    channel: Channel | None = None
    body: str | None = Field(None, max_length=20000)
    media_notes: str | None = Field(None, max_length=5000)
    hashtags: list[str] | None = Field(None, max_length=30)
    scheduled_for: datetime | None = None
    status: Literal["idea", "draft", "awaiting_approval"] | None = None
    meta: dict[str, Any] | None = None
    media: list[str] | None = Field(None, max_length=4)


class DecisionBody(Body):
    note: str = Field("", max_length=2000)


class OutcomeBody(Body):
    status: Literal["published", "failed"] = "published"
    url: str | None = Field(None, max_length=1000)
    note: str = Field("", max_length=2000)


class PublishingBody(Body):
    channel: Channel
    integration_id: str | None = Field(None, description="The integration to publish through; null disconnects the channel")


@router.get("/content/publishing", response_model=schemas.Items[schemas.PublishingChannelOut])
def publishing_channels(ctx: Ctx = Depends(org_ctx("content.read"))) -> dict:
    """Every channel, whether the platform can publish to it natively, and the account it publishes through."""
    from app.services.publishing import channels

    return {"items": channels(ctx.db, ctx.org_id)}


@router.put("/content/publishing", response_model=schemas.Items[schemas.PublishingChannelOut])
def connect_channel(body: PublishingBody, ctx: Ctx = Depends(org_ctx("content.approve"))) -> dict:
    """Choose the connected account a channel publishes through. From then on approved items for that
    channel are posted automatically at their scheduled time."""
    from app.services.publishing import channels, set_connection

    set_connection(ctx.db, ctx.org_id, body.channel, body.integration_id)
    audit(ctx.db, "content.publishing_changed", ctx=ctx, resource_type="organization", resource_id=ctx.org_id,
          meta={"channel": body.channel, "integration_id": body.integration_id})
    ctx.db.commit()
    return {"items": channels(ctx.db, ctx.org_id)}


@router.post("/content", response_model=schemas.ContentItemOut, status_code=201)
def create_content(body: ContentBody, ctx: Ctx = Depends(org_ctx("content.write"))) -> dict:
    item = svc.create_item(ctx.db, ctx.org_id, title=body.title, channel=body.channel, body=body.body, media_notes=body.media_notes,
                           hashtags=body.hashtags, scheduled_for=body.scheduled_for, status=body.status, user_id=ctx.user.id, meta=body.meta,
                           media=body.media)
    audit(ctx.db, "content.created", ctx=ctx, resource_type="content_item", resource_id=item.id)
    ctx.db.commit()
    return svc.view(item)


@router.get("/content/{item_id}", response_model=schemas.ContentItemOut)
def get_content(item_id: str, ctx: Ctx = Depends(org_ctx("content.read"))) -> dict:
    return svc.view(svc.get_item(ctx.db, ctx.org_id, item_id))


@router.patch("/content/{item_id}", response_model=schemas.ContentItemOut)
def update_content(item_id: str, body: ContentPatch, ctx: Ctx = Depends(org_ctx("content.write"))) -> dict:
    item = svc.update_item(ctx.db, svc.get_item(ctx.db, ctx.org_id, item_id), body.model_dump(exclude_unset=True))
    audit(ctx.db, "content.updated", ctx=ctx, resource_type="content_item", resource_id=item.id)
    ctx.db.commit()
    return svc.view(item)


@router.post("/content/{item_id}/approve", response_model=schemas.ContentItemOut)
def approve_content(item_id: str, body: DecisionBody, ctx: Ctx = Depends(org_ctx("content.approve"))) -> dict:
    item = svc.decide(ctx.db, svc.get_item(ctx.db, ctx.org_id, item_id), approve=True, user_id=ctx.user.id, note=body.note)
    audit(ctx.db, "content.approved", ctx=ctx, resource_type="content_item", resource_id=item.id)
    ctx.db.commit()
    return svc.view(item)


@router.post("/content/{item_id}/reject", response_model=schemas.ContentItemOut)
def reject_content(item_id: str, body: DecisionBody, ctx: Ctx = Depends(org_ctx("content.approve"))) -> dict:
    item = svc.decide(ctx.db, svc.get_item(ctx.db, ctx.org_id, item_id), approve=False, user_id=ctx.user.id, note=body.note)
    audit(ctx.db, "content.rejected", ctx=ctx, resource_type="content_item", resource_id=item.id)
    ctx.db.commit()
    return svc.view(item)


@router.post("/content/{item_id}/publish", response_model=schemas.ContentItemOut)
def publish_content(item_id: str, ctx: Ctx = Depends(org_ctx("content.approve"))) -> dict:
    """Publish an approved (or previously failed) item now through the account connected for its channel.
    The response carries the outcome: ``published`` with the link, or ``failed`` with the reason."""
    from sqlalchemy import update

    from app.services.publishing import connected_integration, publish_item

    item = svc.get_item(ctx.db, ctx.org_id, item_id)
    if item.status not in ("approved", "failed"):
        raise Conflict("Only approved content can be published")
    if connected_integration(ctx.db, ctx.org_id, item.channel) is None:
        raise Conflict(f"No account is connected for {item.channel}. Connect one under Publishing first.")
    # Claim the item so the worker's scheduled release cannot post it a second time.
    previous = item.due_emitted_at
    if item.status == "approved" and previous and previous > utcnow() - timedelta(minutes=2):
        raise Conflict("This item is being published right now")
    claimed = ctx.db.execute(update(ContentItem).where(
        ContentItem.id == item.id, ContentItem.due_emitted_at.is_(None) if previous is None else ContentItem.due_emitted_at == previous)
        .values(due_emitted_at=utcnow()).execution_options(synchronize_session=False))
    ctx.db.commit()
    if claimed.rowcount != 1:
        raise Conflict("This item is being published right now")
    ctx.db.refresh(item)
    publish_item(ctx.db, item)
    audit(ctx.db, f"content.{item.status}", ctx=ctx, resource_type="content_item", resource_id=item.id, meta={"native": True})
    ctx.db.commit()
    return svc.view(item)


@router.post("/content/{item_id}/outcome", response_model=schemas.ContentItemOut)
def record_outcome(item_id: str, body: OutcomeBody, ctx: Ctx = Depends(org_ctx("content.write"))) -> dict:
    """Record that an approved item was published (for example by hand) or failed."""
    item = svc.mark(ctx.db, svc.get_item(ctx.db, ctx.org_id, item_id), status=body.status, url=body.url, note=body.note)
    audit(ctx.db, f"content.{body.status}", ctx=ctx, resource_type="content_item", resource_id=item.id)
    ctx.db.commit()
    return svc.view(item)


@router.delete("/content/{item_id}", response_model=schemas.Ok)
def delete_content(item_id: str, ctx: Ctx = Depends(org_ctx("content.write"))) -> dict:
    item = svc.get_item(ctx.db, ctx.org_id, item_id)
    if item.status == "published":
        raise Conflict("Published content stays in the calendar as a record")
    ctx.db.delete(item)
    audit(ctx.db, "content.deleted", ctx=ctx, resource_type="content_item", resource_id=item_id)
    ctx.db.commit()
    return {"ok": True}


# ----------------------------------------------------------------------------- teams


TEAM_NAMES = {"marketing": "AI Marketing Team"}


@router.get("/teams", response_model=schemas.Items[schemas.TeamOut], tags=["agents"])
def list_teams(ctx: Ctx = Depends(org_ctx("agents.read"))) -> dict:
    """Agents installed as a team, grouped by module, with a week of activity."""
    db, since = ctx.db, utcnow() - timedelta(days=7)
    agents = db.scalars(select(AIAgent).where(AIAgent.organization_id == ctx.org_id, AIAgent.team_key.is_not(None),
                                              AIAgent.deleted_at.is_(None)).order_by(AIAgent.created_at)).all()
    if not agents:
        return {"items": []}
    E = AIAgentExecution
    stats = {r[0]: r for r in db.execute(select(E.agent_id, func.count(), func.max(E.created_at),
                                                func.sum(E.input_tokens + E.output_tokens), func.sum(E.cost_usd))
                                         .where(E.organization_id == ctx.org_id, E.created_at >= since, E.agent_id.in_([a.id for a in agents]))
                                         .group_by(E.agent_id)).all()}
    delegated = dict(db.execute(select(E.agent_id, func.count()).where(E.organization_id == ctx.org_id, E.created_at >= since,
                                                                       E.parent_execution_id.is_not(None)).group_by(E.agent_id)).all())
    content = dict(db.execute(select(ContentItem.status, func.count()).where(ContentItem.organization_id == ctx.org_id,
                                                                             ContentItem.updated_at >= since).group_by(ContentItem.status)).all())
    teams: dict[str, dict] = {}
    for a in agents:
        team = teams.setdefault(a.team_key, {"key": a.team_key, "name": TEAM_NAMES.get(a.team_key, a.team_key.title()), "lead": None, "modules": {}})
        s = stats.get(a.id)
        member = {"id": str(a.id), "name": a.name, "description": a.description, "status": a.status, "module": a.team_module,
                  "runs_7d": int(s[1]) if s else 0, "delegated_7d": int(delegated.get(a.id, 0)), "last_run_at": iso(s[2]) if s else None,
                  "tokens_7d": int(s[3] or 0) if s else 0, "cost_usd_7d": float(s[4]) if s and s[4] is not None else 0.0}
        if a.team_lead:
            team["lead"] = member
        else:
            team["modules"].setdefault(a.team_module or "Team", []).append(member)
    out = []
    for team in teams.values():
        members = [m for ms in team["modules"].values() for m in ms] + ([team["lead"]] if team["lead"] else [])
        out.append({**team, "modules": [{"name": n, "agents": ms} for n, ms in team["modules"].items()],
                    "kpis": {"runs_7d": sum(m["runs_7d"] for m in members), "delegations_7d": sum(m["delegated_7d"] for m in members),
                             "cost_usd_7d": round(sum(m["cost_usd_7d"] for m in members), 4),
                             "content_awaiting_approval": int(content.get("awaiting_approval", 0)),
                             "content_approved": int(content.get("approved", 0)), "content_published": int(content.get("published", 0))}})
    return {"items": out}
