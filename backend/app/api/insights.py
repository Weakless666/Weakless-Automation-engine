"""Analytics, usage, billing, notifications and audit logs."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import func, select

from app.api import schemas
from app.api.common import ApiRoute
from app.api.common import Body, Paging, iso, owned, paginate, parse_dt, parse_uuid
from app.core import vault
from app.core.errors import AppError, NodeError, NotFound, ValidationFailed
from app.db import new_session, utcnow
from app.deps import Ctx, optional_org_ctx, org_ctx, platform_ctx, user_ctx
from app.models import (
    AuditLog, BillingEvent, Notification, NotificationChannel, NotificationDelivery, NotificationPreference, Subscription,
    SubscriptionPlan, UsageCounter,
)
from app.services import analytics, billing
from app.services.audit import audit
from app.services.notifications import EVENT_TYPES, deliver_to_channel
from app.services.usage import current_period, quota_status, usage_totals

router = APIRouter(route_class=ApiRoute)
public = APIRouter(route_class=ApiRoute, tags=["billing"])


# ----------------------------------------------------------------------------- analytics


def _filters(ctx: Ctx, start: str | None, end: str | None, workflow_id: str | None = None, status: str | None = None) -> analytics.Filters:
    s, e = parse_dt(start, "start"), parse_dt(end, "end")
    if s and e and s > e:
        raise ValidationFailed("'start' must be before 'end'")
    return analytics.Filters(ctx.org.id if ctx.org else None, s, e, parse_uuid(workflow_id, "Workflow") if workflow_id else None, status)


@router.get("/analytics/overview", response_model=schemas.OverviewOut, tags=["analytics"])
def overview(start: str | None = None, end: str | None = None, workflow_id: str | None = None, status: str | None = None,
             ctx: Ctx = Depends(optional_org_ctx("analytics.read"))) -> dict:
    return analytics.overview(ctx.db, _filters(ctx, start, end, workflow_id, status), platform=ctx.is_staff)


@router.get("/analytics/timeseries", response_model=schemas.Items[schemas.DayPoint], tags=["analytics"])
def timeseries(start: str | None = None, end: str | None = None, workflow_id: str | None = None, status: str | None = None,
               ctx: Ctx = Depends(optional_org_ctx("analytics.read"))) -> dict:
    return {"items": analytics.timeseries(ctx.db, _filters(ctx, start, end, workflow_id, status))}


@router.get("/analytics/workflows", response_model=schemas.Items[schemas.WorkflowStats], tags=["analytics"])
def workflows_breakdown(start: str | None = None, end: str | None = None, ctx: Ctx = Depends(optional_org_ctx("analytics.read"))) -> dict:
    return {"items": analytics.by_workflow(ctx.db, _filters(ctx, start, end))}


@router.get("/analytics/ai", response_model=schemas.AiBreakdown, tags=["analytics"])
def ai_breakdown(start: str | None = None, end: str | None = None, ctx: Ctx = Depends(optional_org_ctx("analytics.read"))) -> dict:
    f = _filters(ctx, start, end)
    return {"summary": analytics.ai_summary(ctx.db, f), "by_model": analytics.ai_by_model(ctx.db, f)}


@router.get("/analytics/integrations", response_model=schemas.IntegrationBreakdown, tags=["analytics"])
def integrations_breakdown(ctx: Ctx = Depends(optional_org_ctx("analytics.read"))) -> dict:
    f = _filters(ctx, None, None)
    return {"summary": analytics.integration_health(ctx.db, f), "items": analytics.integrations_detail(ctx.db, f)}


@router.get("/analytics/system", response_model=schemas.SystemHealth, tags=["analytics"])
def system_health(ctx: Ctx = Depends(platform_ctx("platform.analytics.read"))) -> dict:
    from sqlalchemy import text

    from app.engine.queue import queue_stats

    db_ok = True
    try:
        ctx.db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    q = queue_stats(ctx.db)
    return {"database": "ok" if db_ok else "error", "queue": q,
            "queue_status": "ok" if q["oldest_ready_age_seconds"] < 60 and q["dead"] == 0 else ("degraded" if q["oldest_ready_age_seconds"] < 600 else "stalled"),
            "checked_at": iso(utcnow())}


# ----------------------------------------------------------------------------- usage & billing


def plan_view(p: SubscriptionPlan) -> dict:
    return {"id": str(p.id), "key": p.key, "name": p.name, "description": p.description, "price_monthly_cents": p.price_monthly_cents,
            "currency": p.currency, "limits": p.limits or {}, "features": p.features or [], "is_active": p.is_active, "is_public": p.is_public,
            "provider_price_id": p.provider_price_id, "sort_order": p.sort_order}


@router.get("/usage", response_model=schemas.UsageOut, tags=["usage"])
def usage(period: str | None = None, ctx: Ctx = Depends(org_ctx("billing.read"))) -> dict:
    period = period or current_period()
    return {"period": period, "totals": usage_totals(ctx.db, ctx.org_id, period), "quotas": quota_status(ctx.db, ctx.org_id)}


@router.get("/usage/history", response_model=schemas.Items[schemas.FlexibleRow], tags=["usage"])
def usage_history(months: int = 6, ctx: Ctx = Depends(org_ctx("billing.read"))) -> dict:
    """Monthly usage report: one row per period with every metered metric."""
    C = UsageCounter
    rows = ctx.db.execute(select(C.period, C.metric, C.quantity).where(C.organization_id == ctx.org_id).order_by(C.period.desc(), C.metric)).all()
    periods: dict[str, dict] = {}
    for period, metric, qty in rows:
        periods.setdefault(period, {"period": period})[metric] = float(qty)
    return {"items": list(periods.values())[: max(1, min(months, 36))]}


@router.get("/billing/plans", response_model=schemas.Items[schemas.PlanOut], tags=["billing"])
def list_plans(ctx: Ctx = Depends(user_ctx)) -> dict:
    q = select(SubscriptionPlan).order_by(SubscriptionPlan.sort_order)
    if not ctx.is_staff:
        q = q.where(SubscriptionPlan.is_active.is_(True), SubscriptionPlan.is_public.is_(True))
    plans = [plan_view(p) for p in ctx.db.scalars(q)]
    if not ctx.is_staff:
        for p in plans:
            p.pop("provider_price_id", None)
    return {"items": plans}


class PlanLimits(Body):
    executions_per_month: int | None = Field(None, ge=0)
    ai_tokens_per_month: int | None = Field(None, ge=0)
    integrations: int | None = Field(None, ge=0)
    workflows: int | None = Field(None, ge=0)
    concurrency: int | None = Field(None, ge=1, le=100)


class PlanBody(Body):
    key: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_\-]{1,58}$")
    name: str = Field(..., min_length=1, max_length=120)
    description: str = ""
    price_monthly_cents: int = Field(0, ge=0)
    currency: str = Field("usd", min_length=3, max_length=3)
    limits: PlanLimits = Field(default_factory=PlanLimits)
    features: list[str] = Field(default_factory=list)
    provider_price_id: str | None = Field(None, max_length=120)
    is_active: bool = True
    is_public: bool = True
    sort_order: int = 0


@router.post("/billing/plans", response_model=schemas.PlanOut, status_code=201, tags=["billing"])
def create_plan(body: PlanBody, ctx: Ctx = Depends(platform_ctx("platform.plans.manage"))) -> dict:
    if ctx.db.scalar(select(SubscriptionPlan.id).where(SubscriptionPlan.key == body.key)):
        raise AppError("A plan with this key already exists", code="conflict", status_code=409)
    p = SubscriptionPlan(**{**body.model_dump(), "limits": body.limits.model_dump(), "currency": body.currency.lower()})
    ctx.db.add(p)
    ctx.db.flush()
    audit(ctx.db, "plan.created", ctx=ctx, resource_type="plan", resource_id=p.id, meta={"key": p.key})
    ctx.db.commit()
    return plan_view(p)


@router.put("/billing/plans/{plan_id}", response_model=schemas.PlanOut, tags=["billing"])
def update_plan(plan_id: str, body: PlanBody, ctx: Ctx = Depends(platform_ctx("platform.plans.manage"))) -> dict:
    p = ctx.db.get(SubscriptionPlan, parse_uuid(plan_id, "Plan"))
    if not p:
        raise NotFound("Plan not found")
    data = {**body.model_dump(exclude={"key"}), "limits": body.limits.model_dump(), "currency": body.currency.lower()}
    for k, v in data.items():
        setattr(p, k, v)
    audit(ctx.db, "plan.updated", ctx=ctx, resource_type="plan", resource_id=p.id, meta={"key": p.key})
    ctx.db.commit()
    return plan_view(p)


@router.get("/billing/subscription", response_model=schemas.SubscriptionOut, tags=["billing"])
def subscription(ctx: Ctx = Depends(org_ctx("billing.read"))) -> dict:
    sub = ctx.db.scalar(select(Subscription).where(Subscription.organization_id == ctx.org_id))
    plan = ctx.db.get(SubscriptionPlan, sub.plan_id) if sub else None
    pending = ctx.db.get(SubscriptionPlan, sub.pending_plan_id) if sub and sub.pending_plan_id else None
    return {
        "subscription": None if not sub else {
            "status": sub.status, "provider": sub.provider, "current_period_start": iso(sub.current_period_start),
            "current_period_end": iso(sub.current_period_end), "cancel_at_period_end": sub.cancel_at_period_end,
            "pending_plan": plan_view(pending)["name"] if pending else None},
        "plan": plan_view(plan) if plan else None,
        "quotas": quota_status(ctx.db, ctx.org_id),
        # Tells the UI what a plan change will actually do, so it never promises a charge that cannot happen.
        "payment_provider": "stripe" if billing.stripe_enabled() else None,
    }


class ChangePlan(Body):
    plan: str = Field(..., description="Plan id or key")


@router.post("/billing/change-plan", response_model=schemas.PlanChangeOut, tags=["billing"])
def change_plan(body: ChangePlan, ctx: Ctx = Depends(org_ctx("billing.manage"))) -> dict:
    plan = billing.get_plan(ctx.db, body.plan)
    if not ctx.is_staff and not plan.is_public:
        raise NotFound("Plan not found")
    result = billing.request_plan_change(ctx.db, ctx.org, plan, user_email=ctx.user.email, is_staff=ctx.is_staff)
    audit(ctx.db, "billing.plan_change", ctx=ctx, resource_type="subscription", resource_id=ctx.org_id, meta={"plan": plan.key, "result": result["result"]})
    ctx.db.commit()
    return result


@router.post("/billing/cancel", response_model=schemas.CancelOut, tags=["billing"])
def cancel_subscription(ctx: Ctx = Depends(org_ctx("billing.manage"))) -> dict:
    sub = billing.cancel_subscription(ctx.db, ctx.org_id, ctx.user.email)
    audit(ctx.db, "billing.cancel_scheduled", ctx=ctx, resource_type="subscription", resource_id=ctx.org_id)
    ctx.db.commit()
    return {"cancel_at_period_end": sub.cancel_at_period_end, "effective": iso(sub.current_period_end)}


@router.get("/billing/events", response_model=schemas.Page[schemas.BillingEventOut], tags=["billing"])
def billing_events(paging: Paging = Depends(), ctx: Ctx = Depends(org_ctx("billing.read"))) -> dict:
    q = select(BillingEvent).where(BillingEvent.organization_id == ctx.org_id).order_by(BillingEvent.created_at.desc())
    return paginate(ctx.db, q, paging, lambda e: {"id": str(e.id), "type": e.type, "provider": e.provider, "amount_cents": e.amount_cents,
                                                  "currency": e.currency, "data": e.data, "created_at": iso(e.created_at)})


@public.post("/billing/webhooks/stripe", response_model=schemas.StatusOut, include_in_schema=False)
async def stripe_webhook(request: Request) -> dict:
    body = await request.body()
    with new_session(bypass_rls=True) as db:
        result = billing.handle_stripe_webhook(db, body, request.headers.get("stripe-signature", ""))
        db.commit()
    return {"status": result}


# ----------------------------------------------------------------------------- notifications


def notification_view(n: Notification) -> dict:
    return {"id": str(n.id), "organization_id": str(n.organization_id) if n.organization_id else None, "event_type": n.event_type,
            "severity": n.severity, "title": n.title, "body": n.body, "data": n.data, "read": n.read_at is not None, "created_at": iso(n.created_at)}


@router.get("/notifications", response_model=schemas.NotificationPage, tags=["notifications"])
def list_notifications(unread: bool = False, paging: Paging = Depends(), ctx: Ctx = Depends(user_ctx)) -> dict:
    q = select(Notification).where(Notification.user_id == ctx.user.id)
    if unread:
        q = q.where(Notification.read_at.is_(None))
    page = paginate(ctx.db, q.order_by(Notification.created_at.desc()), paging, notification_view)
    page["unread"] = ctx.db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == ctx.user.id, Notification.read_at.is_(None))) or 0
    return page


@router.post("/notifications/{notification_id}/read", response_model=schemas.NotificationOut, tags=["notifications"])
def mark_read(notification_id: str, ctx: Ctx = Depends(user_ctx)) -> dict:
    n = ctx.db.get(Notification, parse_uuid(notification_id, "Notification"))
    if not n or n.user_id != ctx.user.id:
        raise NotFound("Notification not found")
    n.read_at = n.read_at or utcnow()
    ctx.db.commit()
    return notification_view(n)


@router.post("/notifications/read-all", response_model=schemas.UpdatedCount, tags=["notifications"])
def mark_all_read(ctx: Ctx = Depends(user_ctx)) -> dict:
    rows = ctx.db.scalars(select(Notification).where(Notification.user_id == ctx.user.id, Notification.read_at.is_(None))).all()
    for n in rows:
        n.read_at = utcnow()
    ctx.db.commit()
    return {"updated": len(rows)}


@router.get("/notifications/preferences", response_model=schemas.Items[schemas.PreferenceOut], tags=["notifications"])
def get_preferences(ctx: Ctx = Depends(user_ctx)) -> dict:
    prefs = {p.event_type: p for p in ctx.db.scalars(select(NotificationPreference).where(NotificationPreference.user_id == ctx.user.id))}
    return {"items": [{"event_type": k, "label": v["label"], "in_app": prefs[k].in_app if k in prefs else v["in_app"],
                       "email": prefs[k].email if k in prefs else False} for k, v in EVENT_TYPES.items()]}


class Preference(Body):
    event_type: str
    in_app: bool
    email: bool


class Preferences(Body):
    items: list[Preference]


@router.put("/notifications/preferences", response_model=schemas.Items[schemas.PreferenceOut], tags=["notifications"])
def put_preferences(body: Preferences, ctx: Ctx = Depends(user_ctx)) -> dict:
    existing = {p.event_type: p for p in ctx.db.scalars(select(NotificationPreference).where(NotificationPreference.user_id == ctx.user.id))}
    for item in body.items:
        if item.event_type not in EVENT_TYPES:
            raise ValidationFailed(f"Unknown event type '{item.event_type}'")
        pref = existing.get(item.event_type)
        if not pref:
            pref = NotificationPreference(user_id=ctx.user.id, event_type=item.event_type)
            ctx.db.add(pref)
        pref.in_app, pref.email = item.in_app, item.email
    ctx.db.commit()
    return get_preferences(ctx)


CHANNEL_SECRETS = {"webhook": ["url", "signing_secret"], "slack": ["url"], "discord": ["url"], "telegram": ["bot_token"], "email": []}
CHANNEL_CONFIG = {"webhook": [], "slack": [], "discord": [], "telegram": ["chat_id"], "email": ["address"]}


def channel_view(c: NotificationChannel) -> dict:
    return {"id": str(c.id), "kind": c.kind, "name": c.name, "config": c.config or {}, "events": c.events or [], "is_active": c.is_active,
            "has_secret": bool(c.secret_envelope), "created_at": iso(c.created_at)}


class ChannelBody(Body):
    kind: Literal["webhook", "slack", "discord", "telegram", "email"]
    name: str = Field(..., min_length=1, max_length=200)
    values: dict[str, Any] = Field(default_factory=dict, description="url / signing_secret / bot_token / chat_id / address depending on kind")
    events: list[str] = Field(default_factory=list, description="Subscribed event types; empty = all")
    is_active: bool = True


def _apply_channel(c: NotificationChannel, body: ChannelBody, creating: bool) -> None:
    allowed = CHANNEL_SECRETS[body.kind] + CHANNEL_CONFIG[body.kind]
    unknown = set(body.values) - set(allowed)
    if unknown:
        raise ValidationFailed(f"Unknown field(s) for {body.kind}: {', '.join(sorted(unknown))}")
    for e in body.events:
        if e not in EVENT_TYPES:
            raise ValidationFailed(f"Unknown event type '{e}'")
    secret = {k: str(body.values[k]) for k in CHANNEL_SECRETS[body.kind] if body.values.get(k)}
    config = {k: str(body.values[k]) for k in CHANNEL_CONFIG[body.kind] if body.values.get(k)}
    required = {"webhook": ["url"], "slack": ["url"], "discord": ["url"], "telegram": ["bot_token", "chat_id"], "email": ["address"]}[body.kind]
    if creating:
        missing = [k for k in required if k not in secret and k not in config]
        if missing:
            raise ValidationFailed(f"Missing field(s): {', '.join(missing)}")
    if "url" in secret and not secret["url"].startswith("https://"):
        raise ValidationFailed("Channel URL must start with https://")
    c.kind, c.name, c.events, c.is_active = body.kind, body.name, body.events, body.is_active
    c.config = {**(c.config or {}), **config} if not creating else config
    aad = vault.aad_for(c.organization_id, f"notification_channel:{c.id}")
    if secret:
        current = vault.decrypt_json(c.secret_envelope, aad) if c.secret_envelope and not creating else {}
        c.secret_envelope = vault.encrypt_json({**current, **secret}, aad)


@router.get("/notification-channels", response_model=schemas.ChannelsOut, tags=["notifications"])
def list_channels(ctx: Ctx = Depends(org_ctx("notifications.manage"))) -> dict:
    rows = ctx.db.scalars(select(NotificationChannel).where(NotificationChannel.organization_id == ctx.org_id).order_by(NotificationChannel.name)).all()
    return {"items": [channel_view(c) for c in rows], "event_types": [{"key": k, "label": v["label"]} for k, v in EVENT_TYPES.items()]}


@router.post("/notification-channels", response_model=schemas.ChannelOut, status_code=201, tags=["notifications"])
def create_channel(body: ChannelBody, ctx: Ctx = Depends(org_ctx("notifications.manage"))) -> dict:
    import uuid as _uuid

    c = NotificationChannel(id=_uuid.uuid4(), organization_id=ctx.org_id, kind=body.kind, name=body.name)
    _apply_channel(c, body, creating=True)
    ctx.db.add(c)
    audit(ctx.db, "notification_channel.created", ctx=ctx, resource_type="notification_channel", resource_id=c.id, meta={"kind": c.kind})
    ctx.db.commit()
    return channel_view(c)


@router.put("/notification-channels/{channel_id}", response_model=schemas.ChannelOut, tags=["notifications"])
def update_channel(channel_id: str, body: ChannelBody, ctx: Ctx = Depends(org_ctx("notifications.manage"))) -> dict:
    c = owned(ctx.db, NotificationChannel, channel_id, ctx.org_id, "Channel")
    if body.kind != c.kind:
        raise ValidationFailed("A channel's kind cannot be changed")
    _apply_channel(c, body, creating=False)
    audit(ctx.db, "notification_channel.updated", ctx=ctx, resource_type="notification_channel", resource_id=c.id)
    ctx.db.commit()
    return channel_view(c)


@router.delete("/notification-channels/{channel_id}", response_model=schemas.Ok, tags=["notifications"])
def delete_channel(channel_id: str, ctx: Ctx = Depends(org_ctx("notifications.manage"))) -> dict:
    c = owned(ctx.db, NotificationChannel, channel_id, ctx.org_id, "Channel")
    ctx.db.delete(c)
    audit(ctx.db, "notification_channel.deleted", ctx=ctx, resource_type="notification_channel", resource_id=c.id)
    ctx.db.commit()
    return {"ok": True}


@router.post("/notification-channels/{channel_id}/test", response_model=schemas.DeliveryResult, tags=["notifications"])
def test_channel(channel_id: str, ctx: Ctx = Depends(org_ctx("notifications.manage"))) -> dict:
    c = owned(ctx.db, NotificationChannel, channel_id, ctx.org_id, "Channel")
    try:
        deliver_to_channel(c, "workflow.notification", "Test notification", f"This is a test from Weakless Automation Engine for {ctx.org.name}.", {"test": True})
        status, error = "sent", None
    except NodeError as exc:
        status, error = "failed", exc.message
    ctx.db.add(NotificationDelivery(organization_id=ctx.org_id, channel_id=c.id, channel_kind=c.kind, event_type="test", status=status, error=error))
    ctx.db.commit()
    return {"status": status, "error": error}


@router.get("/notification-deliveries", response_model=schemas.Page[schemas.DeliveryOut], tags=["notifications"])
def deliveries(paging: Paging = Depends(), ctx: Ctx = Depends(org_ctx("notifications.manage"))) -> dict:
    q = select(NotificationDelivery).where(NotificationDelivery.organization_id == ctx.org_id).order_by(NotificationDelivery.created_at.desc())
    return paginate(ctx.db, q, paging, lambda d: {"id": str(d.id), "channel_kind": d.channel_kind, "event_type": d.event_type,
                                                  "status": d.status, "error": d.error, "created_at": iso(d.created_at)})


# ----------------------------------------------------------------------------- audit


@router.get("/audit-logs", response_model=schemas.Page[schemas.AuditEntryOut], tags=["audit"])
def audit_logs(action: str | None = None, actor: str | None = None, start: str | None = None, end: str | None = None,
               paging: Paging = Depends(), ctx: Ctx = Depends(optional_org_ctx("audit.read"))) -> dict:
    q = select(AuditLog)
    if ctx.org:
        q = q.where(AuditLog.organization_id == ctx.org_id)
    else:
        ctx.require("platform.audit.read")
    if action:
        q = q.where(AuditLog.action.like(f"{action}%"))
    if actor:
        q = q.where(func.lower(AuditLog.actor_email).like(f"%{actor.lower()}%"))
    if (s := parse_dt(start, "start")):
        q = q.where(AuditLog.created_at >= s)
    if (e := parse_dt(end, "end")):
        q = q.where(AuditLog.created_at <= e)
    return paginate(ctx.db, q.order_by(AuditLog.created_at.desc()), paging, lambda a: {
        "id": str(a.id), "organization_id": str(a.organization_id) if a.organization_id else None, "actor_email": a.actor_email,
        "action": a.action, "resource_type": a.resource_type, "resource_id": a.resource_id, "meta": a.meta, "ip": a.ip,
        "created_at": iso(a.created_at)})
