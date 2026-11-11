"""Usage metering, plan limits and quota enforcement."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.errors import QuotaExceeded
from app.db import utcnow
from app.models import Integration, Subscription, SubscriptionPlan, UsageRecord, Workflow

METRICS = ("executions", "ai_requests", "ai_input_tokens", "ai_output_tokens", "ai_cost_usd")
ALERT_THRESHOLDS = (80, 100)
# quota key in plan.limits -> usage metrics that count against it
QUOTAS = {
    "executions_per_month": ("executions",),
    "ai_tokens_per_month": ("ai_input_tokens", "ai_output_tokens"),
}


def current_period() -> str:
    return utcnow().strftime("%Y-%m")


def get_subscription(db: Session, organization_id: uuid.UUID) -> tuple[Subscription | None, SubscriptionPlan | None]:
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == organization_id))
    if not sub:
        return None, None
    return sub, db.get(SubscriptionPlan, sub.plan_id)


def plan_limit(db: Session, organization_id: uuid.UUID, key: str):
    """Limit value from the organization's plan; ``None`` means unlimited."""
    sub, plan = get_subscription(db, organization_id)
    if not sub or not plan or sub.status == "canceled":
        return None
    return (plan.limits or {}).get(key)


def usage_totals(db: Session, organization_id: uuid.UUID, period: str | None = None) -> dict[str, float]:
    period = period or current_period()
    rows = db.execute(
        select(UsageRecord.metric, func.coalesce(func.sum(UsageRecord.quantity), 0))
        .where(UsageRecord.organization_id == organization_id, UsageRecord.period == period)
        .group_by(UsageRecord.metric)
    ).all()
    totals = {m: 0.0 for m in METRICS}
    for metric, qty in rows:
        totals[metric] = float(qty)
    return totals


def quota_status(db: Session, organization_id: uuid.UUID) -> dict[str, dict]:
    totals = usage_totals(db, organization_id)
    out = {}
    for key, metrics in QUOTAS.items():
        limit = plan_limit(db, organization_id, key)
        used = sum(totals[m] for m in metrics)
        out[key] = {
            "used": used,
            "limit": limit,
            "percent": round(used / limit * 100, 1) if limit else None,
            "exceeded": bool(limit is not None and used >= limit),
        }
    for key, model, extra in (("workflows", Workflow, Workflow.deleted_at.is_(None)), ("integrations", Integration, Integration.deleted_at.is_(None))):
        limit = plan_limit(db, organization_id, key)
        used = db.scalar(select(func.count()).select_from(model).where(model.organization_id == organization_id, extra)) or 0
        out[key] = {"used": used, "limit": limit, "percent": round(used / limit * 100, 1) if limit else None,
                    "exceeded": bool(limit is not None and used >= limit)}
    return out


def check_quota(db: Session, organization_id: uuid.UUID, key: str) -> None:
    """Raise ``QuotaExceeded`` when the plan quota ``key`` is used up."""
    limit = plan_limit(db, organization_id, key)
    if limit is None:
        return
    if key in QUOTAS:
        totals = usage_totals(db, organization_id)
        used = sum(totals[m] for m in QUOTAS[key])
    elif key == "workflows":
        used = db.scalar(select(func.count()).select_from(Workflow).where(Workflow.organization_id == organization_id, Workflow.deleted_at.is_(None))) or 0
    elif key == "integrations":
        used = db.scalar(select(func.count()).select_from(Integration).where(Integration.organization_id == organization_id, Integration.deleted_at.is_(None))) or 0
    else:
        return
    if used >= limit:
        raise QuotaExceeded(f"Plan limit reached for {key.replace('_', ' ')} ({int(used)}/{int(limit)})", details={"quota": key, "used": used, "limit": limit})


def record_usage(
    db: Session,
    organization_id: uuid.UUID,
    metric: str,
    quantity: float | int | Decimal,
    *,
    workflow_id: uuid.UUID | None = None,
    execution_id: uuid.UUID | None = None,
    meta: dict | None = None,
) -> None:
    if not quantity:
        return
    db.add(UsageRecord(
        organization_id=organization_id, metric=metric, quantity=Decimal(str(quantity)), period=current_period(),
        workflow_id=workflow_id, execution_id=execution_id, meta=meta or {},
    ))
    db.flush()
    _check_alerts(db, organization_id)


def _check_alerts(db: Session, organization_id: uuid.UUID) -> None:
    """Notify once per period when a quota crosses 80% and 100%."""
    sub, plan = get_subscription(db, organization_id)
    if not sub or not plan:
        return
    period = current_period()
    state = dict(sub.alert_state or {})
    sent = dict(state.get(period) or {})
    totals = None
    for key, metrics in QUOTAS.items():
        limit = (plan.limits or {}).get(key)
        if not limit:
            continue
        totals = totals or usage_totals(db, organization_id, period)
        pct = sum(totals[m] for m in metrics) / limit * 100
        already = sent.get(key, [])
        for threshold in ALERT_THRESHOLDS:
            if pct >= threshold and threshold not in already:
                already = already + [threshold]
                sent[key] = already
                sub.alert_state = {period: sent}
                flag_modified(sub, "alert_state")
                db.flush()
                from app.services.notifications import notify

                notify(
                    db, "usage.threshold", organization_id,
                    title=f"Usage at {threshold}% of {key.replace('_', ' ')}",
                    body=f"The organization has used {int(pct)}% of its plan allowance for {key.replace('_', ' ')} this month.",
                    severity="warning" if threshold < 100 else "error",
                    data={"quota": key, "threshold": threshold, "percent": round(pct, 1)},
                )
