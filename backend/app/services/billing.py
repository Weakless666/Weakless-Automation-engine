"""Subscription-ready billing behind a provider abstraction.

``ManualProvider`` (default) never charges anyone: plan changes are applied by
authorized staff, or requested by clients and approved by staff. ``StripeProvider``
is used only when ``WAE_STRIPE_SECRET_KEY`` is configured *and* the target plan
has a Stripe price id; card details are entered on Stripe-hosted Checkout and
never touch this system.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import http
from app.core.errors import AppError, Conflict, NotFound
from app.db import utcnow
from app.models import BillingEvent, Organization, Subscription, SubscriptionPlan
from app.services.notifications import notify


def log_event(db: Session, organization_id, type: str, data: dict, *, provider: str = "manual", provider_event_id: str | None = None,
              amount_cents: int | None = None, currency: str | None = None) -> BillingEvent:
    ev = BillingEvent(organization_id=organization_id, type=type, provider=provider, provider_event_id=provider_event_id,
                      amount_cents=amount_cents, currency=currency, data=data)
    db.add(ev)
    db.flush()
    return ev


def get_plan(db: Session, plan_id_or_key) -> SubscriptionPlan:
    plan = None
    try:
        plan = db.get(SubscriptionPlan, plan_id_or_key if isinstance(plan_id_or_key, uuid.UUID) else uuid.UUID(str(plan_id_or_key)))
    except ValueError:
        plan = db.scalar(select(SubscriptionPlan).where(SubscriptionPlan.key == str(plan_id_or_key)))
    if not plan or not plan.is_active:
        raise NotFound("Plan not found")
    return plan


def stripe_enabled() -> bool:
    return bool(get_settings().stripe_secret_key)


def apply_plan(db: Session, organization_id: uuid.UUID, plan: SubscriptionPlan, *, actor: str, provider: str = "manual",
               immediate: bool = True) -> Subscription:
    """Set (or schedule) an organization's plan. Upgrades apply now; downgrades at period end."""
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == organization_id))
    now = utcnow()
    if not sub:
        sub = Subscription(organization_id=organization_id, plan_id=plan.id, status="active", current_period_start=now,
                           current_period_end=now + timedelta(days=30), provider=provider)
        db.add(sub)
        db.flush()
        log_event(db, organization_id, "subscription.created", {"plan": plan.key, "actor": actor}, provider=provider)
    else:
        old = db.get(SubscriptionPlan, sub.plan_id)
        if old and old.id == plan.id and sub.status != "canceled":
            sub.pending_plan_id, sub.cancel_at_period_end = None, False
            db.flush()
            return sub
        downgrade = bool(old and plan.price_monthly_cents < old.price_monthly_cents)
        if downgrade and not immediate and sub.status != "canceled":
            sub.pending_plan_id = plan.id
            log_event(db, organization_id, "subscription.downgrade_scheduled",
                      {"from": old.key, "to": plan.key, "effective": sub.current_period_end.isoformat(), "actor": actor}, provider=provider)
        else:
            sub.plan_id, sub.pending_plan_id, sub.status, sub.cancel_at_period_end = plan.id, None, "active", False
            sub.provider = provider
            if sub.current_period_end <= now:
                sub.current_period_start, sub.current_period_end = now, now + timedelta(days=30)
            log_event(db, organization_id, "subscription.plan_changed",
                      {"from": old.key if old else None, "to": plan.key, "actor": actor}, provider=provider)
        db.flush()
    notify(db, "subscription.event", organization_id, title=f"Subscription updated: {plan.name}",
           body="The plan change is scheduled for the end of the billing period." if sub.pending_plan_id else "The new plan is active now.",
           data={"plan": plan.key})
    return sub


def request_plan_change(db: Session, org: Organization, plan: SubscriptionPlan, *, user_email: str, is_staff: bool) -> dict:
    """Entry point for the API. Returns what happened so the UI can say so truthfully."""
    if is_staff:
        sub = apply_plan(db, org.id, plan, actor=user_email, immediate=False)
        return {"result": "scheduled" if sub.pending_plan_id else "applied", "checkout_url": None}
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == org.id))
    current = db.get(SubscriptionPlan, sub.plan_id) if sub else None
    if current and plan.price_monthly_cents <= current.price_monthly_cents:
        # Downgrades never cost money: schedule without involving a payment provider.
        sub = apply_plan(db, org.id, plan, actor=user_email, immediate=False)
        return {"result": "scheduled" if sub.pending_plan_id else "applied", "checkout_url": None}
    if stripe_enabled() and plan.provider_price_id:
        return {"result": "checkout", "checkout_url": stripe_checkout_url(db, org, plan, user_email)}
    # No approved payment flow: record a request for staff instead of charging or silently upgrading.
    log_event(db, org.id, "subscription.change_requested", {"to": plan.key, "requested_by": user_email})
    notify(db, "subscription.event", org.id, title=f"Plan change requested: {plan.name}",
           body=f"{user_email} requested the {plan.name} plan for {org.name}.", severity="warning", data={"plan": plan.key})
    return {"result": "requested", "checkout_url": None}


def cancel_subscription(db: Session, organization_id: uuid.UUID, actor: str) -> Subscription:
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == organization_id))
    if not sub:
        raise NotFound("No subscription")
    if sub.status == "canceled":
        raise Conflict("Subscription is already canceled")
    sub.cancel_at_period_end = True
    log_event(db, organization_id, "subscription.cancel_scheduled", {"effective": sub.current_period_end.isoformat(), "actor": actor}, provider=sub.provider)
    db.flush()
    return sub


def roll_periods(db: Session) -> int:
    """Advance manually-billed subscriptions whose period ended (pending downgrades, cancellations)."""
    now = utcnow()
    subs = db.scalars(select(Subscription).where(Subscription.provider == "manual", Subscription.status.in_(["active", "trialing"]),
                                                 Subscription.current_period_end <= now)).all()
    for sub in subs:
        if sub.cancel_at_period_end:
            sub.status = "canceled"
            log_event(db, sub.organization_id, "subscription.canceled", {})
            continue
        if sub.pending_plan_id:
            sub.plan_id, sub.pending_plan_id = sub.pending_plan_id, None
            log_event(db, sub.organization_id, "subscription.plan_changed", {"reason": "scheduled downgrade"})
        sub.current_period_start = sub.current_period_end
        while sub.current_period_end <= now:
            sub.current_period_end = sub.current_period_end + timedelta(days=30)
    db.commit()
    return len(subs)


# ----------------------------------------------------------------------------- Stripe


def _stripe(method: str, path: str, data: dict | None = None) -> dict:
    resp = http.request(method, f"https://api.stripe.com{path}", data=data,
                        headers={"Authorization": f"Bearer {get_settings().stripe_secret_key}"}, timeout=20)
    if resp.status_code >= 400:
        try:
            message = resp.json().get("error", {}).get("message", "")
        except ValueError:
            message = ""
        raise AppError(f"Stripe error: {message or resp.status_code}", code="billing_provider_error", status_code=502)
    return resp.json()


def stripe_checkout_url(db: Session, org: Organization, plan: SubscriptionPlan, email: str) -> str:
    s = get_settings()
    session = _stripe("POST", "/v1/checkout/sessions", {
        "mode": "subscription", "line_items[0][price]": plan.provider_price_id, "line_items[0][quantity]": "1",
        "client_reference_id": str(org.id), "customer_email": email,
        "metadata[organization_id]": str(org.id), "metadata[plan_id]": str(plan.id),
        "subscription_data[metadata][organization_id]": str(org.id), "subscription_data[metadata][plan_id]": str(plan.id),
        "success_url": f"{s.frontend_url}/billing?checkout=success", "cancel_url": f"{s.frontend_url}/billing?checkout=cancelled",
    })
    log_event(db, org.id, "checkout.started", {"plan": plan.key, "session": session.get("id")}, provider="stripe")
    return session["url"]


def handle_stripe_webhook(db: Session, body: bytes, signature_header: str) -> str:
    """Verify and apply a Stripe event. Idempotent on the Stripe event id."""
    from app.services.webhooks import Rejected, verify_signature

    secret = get_settings().stripe_webhook_secret
    if not secret:
        raise AppError("Stripe webhooks are not configured", code="not_configured", status_code=503)
    try:
        verify_signature("stripe", secret, {"stripe-signature": signature_header}, body)
    except Rejected as rej:
        raise AppError(f"Invalid Stripe signature: {rej.reason}", code="invalid_signature", status_code=400) from rej
    event = json.loads(body)
    if db.scalar(select(BillingEvent.id).where(BillingEvent.provider_event_id == event.get("id"))):
        return "duplicate"
    obj = (event.get("data") or {}).get("object") or {}
    etype = event.get("type", "")
    meta = obj.get("metadata") or {}
    org_id = None
    try:
        org_id = uuid.UUID(meta.get("organization_id") or obj.get("client_reference_id") or "")
    except ValueError:
        sub = db.scalar(select(Subscription).where(Subscription.provider_subscription_id == (obj.get("subscription") or obj.get("id") or "-")))
        org_id = sub.organization_id if sub else None
    if org_id and not db.get(Organization, org_id):
        org_id = None
    amount = obj.get("amount_paid") or obj.get("amount_total")
    log_event(db, org_id, f"stripe.{etype}", {"id": obj.get("id"), "status": obj.get("status")}, provider="stripe",
              provider_event_id=event.get("id"), amount_cents=amount if isinstance(amount, int) else None, currency=obj.get("currency"))
    if not org_id:
        return "unmatched"
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == org_id))
    if etype == "checkout.session.completed" and meta.get("plan_id"):
        plan = get_plan(db, meta["plan_id"])
        sub = apply_plan(db, org_id, plan, actor="stripe", provider="stripe")
        sub.provider_customer_id, sub.provider_subscription_id = obj.get("customer"), obj.get("subscription")
    elif etype in ("customer.subscription.updated", "customer.subscription.deleted") and sub:
        status = obj.get("status")
        sub.status = {"active": "active", "trialing": "trialing", "past_due": "past_due", "unpaid": "past_due"}.get(status, "canceled")
        sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
        if obj.get("current_period_end"):
            sub.current_period_end = datetime.fromtimestamp(int(obj["current_period_end"]), tz=timezone.utc)
        if obj.get("current_period_start"):
            sub.current_period_start = datetime.fromtimestamp(int(obj["current_period_start"]), tz=timezone.utc)
    elif etype == "invoice.payment_failed" and sub:
        sub.status = "past_due"
        notify(db, "subscription.event", org_id, title="Payment failed", body="The latest subscription payment did not go through.", severity="error")
    db.flush()
    return "processed"
