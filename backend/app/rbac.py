"""Role-based access control.

Roles and permissions live in the database (``roles``, ``permissions``,
``role_permissions``) and are seeded from the defaults below. The API resolves
permissions from the database (with a short cache), so SUPER_ADMINs can adjust
role grants without a deploy.

Platform roles (Weakless staff) apply across every organization. Client roles
come from ``organization_members`` and only apply inside that organization.
"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CLIENT_ROLES, PLATFORM_ROLES, Permission, Role, RolePermission

PERMISSIONS: dict[str, str] = {
    # platform scope
    "platform.organizations.create": "Create organizations and onboard clients",
    "platform.users.manage": "Manage Weakless staff accounts and roles",
    "platform.settings.manage": "Change system settings, AI pricing and role grants",
    "platform.plans.manage": "Create and edit subscription plans",
    "platform.templates.manage": "Create and edit templates and industry packages",
    "platform.analytics.read": "View cross-organization analytics and system health",
    "platform.audit.read": "View the platform-wide audit log",
    # organization scope
    "org.read": "View the organization",
    "org.manage": "Edit organization and client profile",
    "members.read": "View organization members",
    "members.manage": "Invite, update and remove organization members",
    "workflows.read": "View workflows",
    "workflows.write": "Create and edit workflow drafts",
    "workflows.publish": "Publish workflow versions",
    "workflows.activate": "Activate and deactivate workflows",
    "workflows.execute": "Run workflows manually",
    "executions.read": "View executions and logs",
    "executions.manage": "Retry, cancel and resume executions; manage dead letters",
    "agents.read": "View AI agents",
    "agents.write": "Create and edit AI agents and their tools",
    "agents.run": "Chat with / run AI agents",
    "knowledge.read": "View knowledge bases",
    "knowledge.write": "Manage knowledge bases and documents",
    "integrations.read": "View integrations and their health",
    "integrations.manage": "Connect, test and revoke integrations",
    "webhooks.read": "View webhook endpoints and events",
    "webhooks.manage": "Rotate webhook secrets and replay events",
    "templates.read": "Browse templates and industry packages",
    "templates.install": "Install and configure templates",
    "analytics.read": "View analytics",
    "billing.read": "View plan, usage and billing history",
    "billing.manage": "Change plan and manage billing",
    "notifications.manage": "Manage organization notification channels",
    "audit.read": "View the organization audit log",
    "approvals.decide": "Approve or reject pending approvals",
    "content.read": "View the content calendar",
    "content.write": "Create and edit content calendar items",
    "content.approve": "Approve or reject content before it is published",
}

_ALL = set(PERMISSIONS)
_ORG_READ = {
    "org.read", "members.read", "workflows.read", "executions.read", "agents.read", "knowledge.read",
    "integrations.read", "webhooks.read", "templates.read", "analytics.read", "billing.read", "content.read",
}
_CONTENT = {"content.write", "content.approve"}

ROLE_DEFAULTS: dict[str, tuple[str, set[str]]] = {
    "SUPER_ADMIN": ("Super Admin", _ALL),
    "ADMIN": ("Admin", _ALL - {"platform.settings.manage"}),
    "MANAGER": ("Manager", _ORG_READ | {
        "org.manage", "members.manage", "executions.manage", "templates.install", "billing.manage",
        "notifications.manage", "audit.read", "approvals.decide", "workflows.activate",
        "platform.organizations.create", "platform.analytics.read", *_CONTENT,
    }),
    "AUTOMATION_ENGINEER": ("Automation Engineer", _ORG_READ | {
        "workflows.write", "workflows.publish", "workflows.activate", "workflows.execute", "executions.manage",
        "agents.write", "agents.run", "knowledge.write", "integrations.manage", "webhooks.manage",
        "templates.install", "notifications.manage", "approvals.decide",
        "platform.templates.manage", "platform.analytics.read", *_CONTENT,
    }),
    "SALES": ("Sales", {"org.read", "org.manage", "members.read", "templates.read", "analytics.read", "billing.read",
                        "platform.organizations.create"}),
    "SUPPORT": ("Support", _ORG_READ | {"executions.manage", "audit.read"}),
    "CLIENT_ADMIN": ("Client Admin", _ORG_READ | {
        "org.manage", "members.manage", "workflows.activate", "workflows.execute", "agents.run", "knowledge.write",
        "integrations.manage", "templates.install", "billing.manage", "notifications.manage", "audit.read",
        "approvals.decide", *_CONTENT,
    }),
    "CLIENT_MEMBER": ("Client Member", {
        "org.read", "members.read", "workflows.read", "workflows.execute", "executions.read", "agents.read",
        "agents.run", "knowledge.read", "integrations.read", "templates.read", "analytics.read", "content.read", "content.write",
    }),
    "VIEWER": ("Viewer", {"org.read", "workflows.read", "executions.read", "agents.read", "templates.read", "analytics.read", "content.read"}),
}

_cache: dict[str, set[str]] = {}
_cache_at = 0.0
_TTL = 30.0


def invalidate_cache() -> None:
    global _cache_at
    _cache.clear()
    _cache_at = 0.0


def role_permissions(db: Session, role: str) -> set[str]:
    global _cache_at
    if not _cache or time.monotonic() - _cache_at > _TTL:
        rows = db.execute(
            select(Role.key, Permission.key)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
        ).all()
        _cache.clear()
        for rkey, pkey in rows:
            _cache.setdefault(rkey, set()).add(pkey)
        _cache_at = time.monotonic()
    return _cache.get(role, set())


def seed_rbac(db: Session, reset_grants: bool = False) -> None:
    """Insert missing roles/permissions. Existing grants are preserved unless ``reset_grants``; a
    permission that did not exist before is granted to the roles whose defaults include it, so
    upgrades add new capabilities without undoing an administrator's changes to older ones."""
    perms = {p.key: p for p in db.scalars(select(Permission))}
    added: set[str] = set()
    for key, desc in PERMISSIONS.items():
        if key not in perms:
            perms[key] = Permission(key=key, description=desc)
            db.add(perms[key])
            added.add(key)
    roles = {r.key: r for r in db.scalars(select(Role))}
    new_roles = []
    for key, (name, _) in ROLE_DEFAULTS.items():
        if key not in roles:
            scope = "platform" if key in PLATFORM_ROLES else "organization"
            roles[key] = Role(key=key, name=name, scope=scope)
            db.add(roles[key])
            new_roles.append(key)
    db.flush()
    for key, (_, grants) in ROLE_DEFAULTS.items():
        role = roles[key]
        existing = {
            pid for (pid,) in db.execute(select(RolePermission.permission_id).where(RolePermission.role_id == role.id))
        }
        if reset_grants and existing:
            for rp in db.scalars(select(RolePermission).where(RolePermission.role_id == role.id)):
                db.delete(rp)
            existing = set()
            db.flush()
        to_grant = grants if (key in new_roles or reset_grants or not existing) else grants & added
        for pkey in to_grant:
            if perms[pkey].id not in existing:
                db.add(RolePermission(role_id=role.id, permission_id=perms[pkey].id))
    db.flush()
    invalidate_cache()


def is_platform_role(role: str | None) -> bool:
    return role in PLATFORM_ROLES


def is_client_role(role: str | None) -> bool:
    return role in CLIENT_ROLES
