"""Workflow drafts, versioning, activation and trigger synchronisation."""
from __future__ import annotations

import re
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core import vault
from app.core.errors import Conflict, ValidationFailed
from app.core.security import random_token
from app.engine.graph import NODE_ID, has_errors, validate_definition
from app.models import (
    AppEventSubscription, WebhookEndpoint, Workflow, WorkflowConnection, WorkflowNode, WorkflowSchedule, WorkflowVersion, Workspace,
)
from app.services.scheduler import next_run, valid_cron

# trigger node type -> (endpoint kind, default signature scheme)
ENDPOINT_TRIGGERS: dict[str, tuple[str, str]] = {
    "trigger.webhook": ("webhook", "hmac_sha256"),
    "trigger.form": ("form", "none"),
    "trigger.database_event": ("database", "shared_secret"),
    "trigger.email": ("email", "shared_secret"),
    "trigger.message": ("message", "telegram"),
    "trigger.payment": ("payment", "stripe"),
}
SIGNATURE_SCHEMES = ("none", "shared_secret", "hmac_sha256", "stripe", "meta", "telegram", "slack")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "item"


def default_workspace(db: Session, organization_id: uuid.UUID) -> Workspace:
    ws = db.scalar(select(Workspace).where(Workspace.organization_id == organization_id, Workspace.is_default.is_(True)))
    if not ws:
        ws = Workspace(organization_id=organization_id, name="Default", slug="default", is_default=True)
        db.add(ws)
        db.flush()
    return ws


def draft_definition(db: Session, wf: Workflow) -> dict:
    nodes = db.scalars(select(WorkflowNode).where(WorkflowNode.workflow_id == wf.id).order_by(WorkflowNode.created_at, WorkflowNode.node_key)).all()
    conns = db.scalars(select(WorkflowConnection).where(WorkflowConnection.workflow_id == wf.id).order_by(WorkflowConnection.created_at)).all()
    return {
        "nodes": [
            {"id": n.node_key, "type": n.type, "name": n.name, "config": n.config or {}, "settings": n.settings or {},
             "position": {"x": n.position_x, "y": n.position_y}}
            for n in nodes
        ],
        "edges": [{"source": c.source_key, "source_handle": c.source_handle, "target": c.target_key} for c in conns],
    }


def save_draft(db: Session, wf: Workflow, definition: dict) -> list[dict]:
    """Replace the draft graph. Structural problems are rejected; semantic issues
    are returned (drafts may be incomplete, publishing requires zero errors)."""
    nodes, edges = definition.get("nodes"), definition.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValidationFailed("Definition must contain 'nodes' and 'edges' lists")
    ids: set[str] = set()
    for n in nodes:
        nid = n.get("id") if isinstance(n, dict) else None
        if not isinstance(nid, str) or not NODE_ID.match(nid) or nid in ids:
            raise ValidationFailed(f"Invalid or duplicate node id {nid!r}")
        if not isinstance(n.get("type"), str):
            raise ValidationFailed(f"Node '{nid}' has no type")
        ids.add(nid)
    seen_edges: set[tuple] = set()
    for e in edges:
        if not isinstance(e, dict) or e.get("source") not in ids or e.get("target") not in ids:
            raise ValidationFailed("Connection references a node that does not exist")
        seen_edges.add((e["source"], e.get("source_handle") or "out", e["target"]))
    db.execute(delete(WorkflowConnection).where(WorkflowConnection.workflow_id == wf.id))
    db.execute(delete(WorkflowNode).where(WorkflowNode.workflow_id == wf.id))
    db.flush()
    for n in nodes:
        pos = n.get("position") or {}
        db.add(WorkflowNode(
            organization_id=wf.organization_id, workflow_id=wf.id, node_key=n["id"], type=n["type"],
            name=str(n.get("name") or "")[:200], config=n.get("config") or {}, settings=n.get("settings") or {},
            position_x=float(pos.get("x") or 0), position_y=float(pos.get("y") or 0),
        ))
    for source, handle, target in seen_edges:
        db.add(WorkflowConnection(organization_id=wf.organization_id, workflow_id=wf.id, source_key=source,
                                  source_handle=handle, target_key=target))
    wf.draft_revision += 1
    db.flush()
    ensure_endpoints(db, wf, definition)
    return validate_definition(definition)


def ensure_endpoints(db: Session, wf: Workflow, definition: dict) -> None:
    """Create webhook endpoints for endpoint-backed trigger nodes so their URL
    is known while the workflow is still a draft."""
    existing = {e.node_key: e for e in db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.workflow_id == wf.id))}
    for n in definition.get("nodes", []):
        mapping = ENDPOINT_TRIGGERS.get(n.get("type"))
        if not mapping:
            continue
        kind, default_scheme = mapping
        cfg = n.get("config") or {}
        scheme = cfg.get("signature_scheme") or {"whatsapp": "meta", "slack": "slack", "telegram": "telegram"}.get(cfg.get("provider"), default_scheme)
        if scheme not in SIGNATURE_SCHEMES:
            scheme = default_scheme
        ep = existing.get(n["id"])
        if ep:
            ep.kind, ep.signature_scheme = kind, scheme
            if cfg.get("rate_limit_per_minute"):
                ep.rate_limit_per_minute = int(cfg["rate_limit_per_minute"])
            continue
        ep = WebhookEndpoint(
            id=uuid.uuid4(), organization_id=wf.organization_id, workflow_id=wf.id, node_key=n["id"],
            endpoint_key=random_token(24), kind=kind, signature_scheme=scheme, is_active=True,
            rate_limit_per_minute=int(cfg.get("rate_limit_per_minute") or 120),
        )
        if scheme in ("hmac_sha256", "shared_secret", "telegram"):
            ep.secret_envelope = vault.encrypt_json({"secret": random_token(32)}, endpoint_aad(ep))
        elif scheme == "meta":  # the app secret is supplied later; the verify token is ours
            ep.secret_envelope = vault.encrypt_json({"verify_token": random_token(24)}, endpoint_aad(ep))
        db.add(ep)
    db.flush()


def endpoint_aad(ep: WebhookEndpoint) -> str:
    return vault.aad_for(ep.organization_id, f"webhook_endpoint:{ep.id}")


def endpoint_secret(ep: WebhookEndpoint) -> str | None:
    if not ep.secret_envelope:
        return None
    return vault.decrypt_json(ep.secret_envelope, endpoint_aad(ep)).get("secret")


def set_endpoint_secret(ep: WebhookEndpoint, secret: str) -> None:
    current = vault.decrypt_json(ep.secret_envelope, endpoint_aad(ep)) if ep.secret_envelope else {}
    ep.secret_envelope = vault.encrypt_json({**current, "secret": secret}, endpoint_aad(ep))


def publish(db: Session, wf: Workflow, user_id: uuid.UUID | None, notes: str = "") -> WorkflowVersion:
    definition = draft_definition(db, wf)
    issues = validate_definition(definition)
    if has_errors(issues):
        raise ValidationFailed("Workflow has validation errors and cannot be published", details=issues)
    last = db.scalar(select(func.max(WorkflowVersion.version)).where(WorkflowVersion.workflow_id == wf.id)) or 0
    version = WorkflowVersion(
        organization_id=wf.organization_id, workflow_id=wf.id, version=last + 1, definition=definition,
        variables=dict(wf.variables or {}), notes=notes, published_by=user_id,
    )
    db.add(version)
    db.flush()
    wf.current_version_id = version.id
    wf.status = "published"
    db.flush()
    sync_triggers(db, wf)
    return version


def set_active(db: Session, wf: Workflow, active: bool) -> None:
    if active and not wf.current_version_id:
        raise Conflict("Publish the workflow before activating it", code="not_published")
    wf.is_active = active
    db.flush()
    sync_triggers(db, wf)


def sync_triggers(db: Session, wf: Workflow) -> None:
    """Align schedules with the current published version and the active flag."""
    version = db.get(WorkflowVersion, wf.current_version_id) if wf.current_version_id else None
    nodes = version.definition.get("nodes", []) if version else []
    wanted = {}
    for n in nodes:
        if n.get("type") == "trigger.schedule":
            cfg = n.get("config") or {}
            if valid_cron(cfg.get("cron", "")):
                wanted[n["id"]] = (cfg["cron"], cfg.get("timezone") or "UTC")
    existing = {s.node_key: s for s in db.scalars(select(WorkflowSchedule).where(WorkflowSchedule.workflow_id == wf.id))}
    for key, sch in existing.items():
        if key not in wanted:
            db.delete(sch)
    for key, (cron, tz) in wanted.items():
        sch = existing.get(key)
        if not sch:
            sch = WorkflowSchedule(organization_id=wf.organization_id, workflow_id=wf.id, node_key=key, cron=cron, timezone=tz)
            db.add(sch)
        changed = sch.cron != cron or sch.timezone != tz or sch.is_active != wf.is_active
        sch.cron, sch.timezone, sch.is_active = cron, tz, wf.is_active
        if wf.is_active and (changed or not sch.next_run_at):
            sch.next_run_at = next_run(cron, tz)
        if not wf.is_active:
            sch.next_run_at = None
    sync_event_subscriptions(db, wf, nodes)
    db.flush()


def sync_event_subscriptions(db: Session, wf: Workflow, nodes: list[dict]) -> None:
    """Mirror the published version's Application Event triggers into the subscription index."""
    wanted = {}
    for n in nodes:
        if n.get("type") == "trigger.app_event":
            pattern = str((n.get("config") or {}).get("event_type") or "").strip()[:120]
            if pattern:
                wanted[n["id"]] = pattern
    existing = {s.node_key: s for s in db.scalars(select(AppEventSubscription).where(AppEventSubscription.workflow_id == wf.id))}
    for key, sub in existing.items():
        if key not in wanted:
            db.delete(sub)
    for key, pattern in wanted.items():
        sub = existing.get(key) or AppEventSubscription(organization_id=wf.organization_id, workflow_id=wf.id, node_key=key, pattern=pattern)
        sub.pattern, sub.exact_type = pattern, None if pattern.endswith("*") else pattern
        db.add(sub)


def create_workflow(
    db: Session,
    organization_id: uuid.UUID,
    name: str,
    *,
    description: str = "",
    definition: dict | None = None,
    variables: dict | None = None,
    workspace_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
    installed_template_id: uuid.UUID | None = None,
) -> Workflow:
    from app.services.usage import check_quota

    check_quota(db, organization_id, "workflows")
    ws_id = workspace_id or default_workspace(db, organization_id).id
    wf = Workflow(organization_id=organization_id, workspace_id=ws_id, name=name, description=description,
                  variables=variables or {}, created_by=created_by, installed_template_id=installed_template_id)
    db.add(wf)
    db.flush()
    if definition:
        save_draft(db, wf, definition)
    return wf
