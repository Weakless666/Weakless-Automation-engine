"""Organization (tenant) provisioning."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.db import utcnow
from app.models import Client, Organization, Subscription, SubscriptionPlan, SystemSetting, Workspace
from app.services.workflows import slugify


def unique_slug(db: Session, name: str) -> str:
    base = slugify(name)
    slug, n = base, 1
    while db.scalar(select(Organization.id).where(Organization.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def default_plan(db: Session) -> SubscriptionPlan | None:
    setting = db.scalar(select(SystemSetting).where(SystemSetting.key == "billing.default_plan"))
    key = setting.value if setting and isinstance(setting.value, str) else "internal"
    return db.scalar(select(SubscriptionPlan).where(SubscriptionPlan.key == key, SubscriptionPlan.is_active.is_(True)))


def create_organization(
    db: Session,
    *,
    name: str,
    industry: str | None = None,
    mode: str = "agency",
    slug: str | None = None,
    plan_key: str | None = None,
    client: dict | None = None,
) -> Organization:
    """Create an organization with its client profile, default workspace and subscription."""
    if slug and db.scalar(select(Organization.id).where(Organization.slug == slug)):
        raise Conflict("An organization with this slug already exists")
    org = Organization(id=uuid.uuid4(), name=name.strip(), slug=slug or unique_slug(db, name), industry=industry, mode=mode)
    db.add(org)
    db.flush()
    db.add(Workspace(organization_id=org.id, name="Default", slug="default", is_default=True))
    db.add(Client(organization_id=org.id, legal_name=name.strip(), **(client or {})))
    plan = None
    if plan_key:
        plan = db.scalar(select(SubscriptionPlan).where(SubscriptionPlan.key == plan_key, SubscriptionPlan.is_active.is_(True)))
        if not plan:
            raise NotFound(f"Plan '{plan_key}' not found")
    plan = plan or default_plan(db)
    if plan:
        now = utcnow()
        db.add(Subscription(organization_id=org.id, plan_id=plan.id, status="active", current_period_start=now,
                            current_period_end=now + timedelta(days=30), provider="manual"))
    db.flush()
    return org
