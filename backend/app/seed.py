"""Idempotent reference-data seeding: RBAC, provider catalog, plans, settings,
template library and the bootstrap administrator."""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import hash_password, validate_password_strength
from app.models import SubscriptionPlan, SystemSetting, User
from app.rbac import seed_rbac

log = logging.getLogger("wae.seed")

# Starting plan catalogue. Prices and limits are placeholders for administrators to edit
# under Usage & Billing; `null` limits are unlimited.
DEFAULT_PLANS = [
    {"key": "internal", "name": "Internal (agency managed)", "price_monthly_cents": 0, "is_public": False, "sort_order": 0,
     "description": "Unmetered plan for organizations managed and billed outside the platform.",
     "limits": {"executions_per_month": None, "ai_tokens_per_month": None, "integrations": None, "workflows": None, "concurrency": 10},
     "features": ["Managed by Weakless"]},
    {"key": "starter", "name": "Starter", "price_monthly_cents": 4900, "sort_order": 1, "description": "For a single location getting started.",
     "limits": {"executions_per_month": 2000, "ai_tokens_per_month": 500000, "integrations": 5, "workflows": 10, "concurrency": 2},
     "features": ["Template library", "Email support"]},
    {"key": "growth", "name": "Growth", "price_monthly_cents": 14900, "sort_order": 2, "description": "For growing teams automating several processes.",
     "limits": {"executions_per_month": 20000, "ai_tokens_per_month": 5000000, "integrations": 20, "workflows": 50, "concurrency": 5},
     "features": ["Everything in Starter", "AI agents", "Industry packages"]},
    {"key": "scale", "name": "Scale", "price_monthly_cents": 49900, "sort_order": 3, "description": "High-volume automation.",
     "limits": {"executions_per_month": 200000, "ai_tokens_per_month": 50000000, "integrations": None, "workflows": None, "concurrency": 20},
     "features": ["Everything in Growth", "Priority support"]},
]

DEFAULT_SETTINGS = {
    "billing.default_plan": ("internal", "Plan assigned to newly created organizations"),
    "ai.default": ({"provider": "anthropic", "model": "claude-opus-5-5"}, "Default AI provider and model for nodes that do not choose one"),
    "ai.model_pricing": ({}, "Overrides/additions to model prices: {model: {input, output}} in USD per million tokens"),
    "ai.anthropic_refusal_fallback": (True, "Send Anthropic's server-side refusal fallback parameter on models that support it"),
    "security.staff_mfa_required": (None, "Weakless staff must use two-factor sign-in (staff without it set it up at their next sign-in)"),
}


def seed_plans(db: Session) -> None:
    existing = {p.key for p in db.scalars(select(SubscriptionPlan))}
    for plan in DEFAULT_PLANS:
        if plan["key"] not in existing:
            db.add(SubscriptionPlan(**plan))
    db.flush()


def seed_settings(db: Session) -> None:
    existing = {s.key for s in db.scalars(select(SystemSetting))}
    for key, (value, description) in DEFAULT_SETTINGS.items():
        if key == "security.staff_mfa_required":
            value = get_settings().staff_mfa_required  # initial value comes from WAE_STAFF_MFA_REQUIRED
        if key not in existing:
            db.add(SystemSetting(key=key, value=value, description=description))
    db.flush()


def seed_bootstrap_admin(db: Session) -> None:
    s = get_settings()
    if not s.bootstrap_admin_email or not s.bootstrap_admin_password:
        return
    if db.scalar(select(func.count()).select_from(User).where(User.platform_role == "SUPER_ADMIN")):
        return
    problem = validate_password_strength(s.bootstrap_admin_password)
    if problem:
        log.error("Bootstrap admin not created: %s", problem)
        return
    db.add(User(email=s.bootstrap_admin_email.lower().strip(), password_hash=hash_password(s.bootstrap_admin_password),
                full_name="Platform Administrator", platform_role="SUPER_ADMIN"))
    db.flush()
    log.info("Created bootstrap SUPER_ADMIN %s", s.bootstrap_admin_email)


def seed_all(db: Session) -> None:
    from app.integrations.service import sync_catalog
    from app.templates.service import seed_library

    seed_rbac(db)
    sync_catalog(db)
    seed_plans(db)
    seed_settings(db)
    seed_library(db)
    seed_bootstrap_admin(db)
    db.flush()
