"""Template marketplace: seeding, installation, configuration and updates.

Installing a template creates a *new workflow owned by the organization* from a
deep copy of the template definition. Nothing mutable is shared between
tenants: later edits to the workflow, its variables or its executions never
touch the template or another organization's copy.
"""
from __future__ import annotations

import copy
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.engine.graph import has_errors, validate_definition
from app.models import (
    AutomationTemplate, IndustryPackage, IndustryPackageItem, InstalledTemplate, TemplateVersion, Workflow,
)
from app.services import workflows as wfs


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def add_version(db: Session, template: AutomationTemplate, definition: dict, config_schema: dict, changelog: str = "") -> TemplateVersion:
    issues = validate_definition(definition)
    if has_errors(issues):
        raise ValidationFailed(f"Template '{template.key}' definition is invalid", details=issues)
    last = db.scalar(select(func.max(TemplateVersion.version)).where(TemplateVersion.template_id == template.id)) or 0
    version = TemplateVersion(template_id=template.id, version=last + 1, definition=definition, config_schema=config_schema, changelog=changelog)
    db.add(version)
    db.flush()
    template.current_version_id = version.id
    db.flush()
    return version


def seed_library(db: Session) -> None:
    """Upsert built-in templates and packages. A changed definition becomes a new template version."""
    from app.templates.library import PACKAGES, TEMPLATES

    existing = {t.key: t for t in db.scalars(select(AutomationTemplate))}
    for spec in TEMPLATES:
        t = existing.get(spec["key"])
        meta = {k: spec[k] for k in ("name", "description", "category", "industry", "required_integrations", "required_credentials",
                                     "pricing", "setup_instructions")}
        if not t:
            t = AutomationTemplate(key=spec["key"], status="published", **meta)
            db.add(t)
            db.flush()
            existing[t.key] = t
            add_version(db, t, spec["definition"], spec["config_schema"], "Initial version")
            continue
        for k, v in meta.items():
            if k != "pricing":  # pricing is administrator-editable
                setattr(t, k, v)
        current = db.get(TemplateVersion, t.current_version_id) if t.current_version_id else None
        if not current or not _same(current.definition, spec["definition"]) or not _same(current.config_schema, spec["config_schema"]):
            add_version(db, t, spec["definition"], spec["config_schema"], "Library update")
    packages = {p.key: p for p in db.scalars(select(IndustryPackage))}
    for spec in PACKAGES:
        p = packages.get(spec["key"])
        if not p:
            p = IndustryPackage(key=spec["key"], name=spec["name"], description=spec["description"], industry=spec["industry"],
                                pricing={"model": "included", "monthly_cents": None}, team_key=spec.get("team"))
            db.add(p)
            db.flush()
        else:
            p.name, p.description, p.industry, p.team_key = spec["name"], spec["description"], spec["industry"], spec.get("team")
        items = {i.template_id: i for i in db.scalars(select(IndustryPackageItem).where(IndustryPackageItem.package_id == p.id))}
        for pos, key in enumerate(spec["templates"]):
            t = existing[key]
            if t.id in items:
                items[t.id].position = pos
            else:
                db.add(IndustryPackageItem(package_id=p.id, template_id=t.id, position=pos))
    db.flush()


def normalize_config(schema: dict, config: dict | None, *, require: bool = True) -> dict:
    """Apply defaults, drop unknown keys and (optionally) enforce required configuration."""
    props = (schema or {}).get("properties") or {}
    out: dict = {}
    for name, prop in props.items():
        value = (config or {}).get(name)
        if value in (None, ""):
            value = prop.get("default")
        if value in (None, ""):
            continue
        if prop.get("type") == "number":
            try:
                value = float(value)
                value = int(value) if value.is_integer() else value
            except (TypeError, ValueError):
                raise ValidationFailed(f"'{prop.get('title', name)}' must be a number")
        elif not isinstance(value, (str, int, float, bool)):
            raise ValidationFailed(f"'{prop.get('title', name)}' has an invalid value")
        out[name] = value
    if require:
        missing = [props[n].get("title", n) for n in (schema or {}).get("required", []) if n not in out]
        if missing:
            raise ValidationFailed(f"Missing configuration: {', '.join(missing)}", details={"missing": missing})
    return out


def current_version(db: Session, template: AutomationTemplate) -> TemplateVersion:
    version = db.get(TemplateVersion, template.current_version_id) if template.current_version_id else None
    if not version:
        raise Conflict("Template has no published version")
    return version


def install_template(db: Session, organization_id: uuid.UUID, template: AutomationTemplate, config: dict | None,
                     user_id: uuid.UUID | None, *, package_id: uuid.UUID | None = None, require_config: bool = False) -> InstalledTemplate:
    if template.status != "published":
        raise Conflict("Template is not available for installation")
    version = current_version(db, template)
    variables = normalize_config(version.config_schema, config, require=require_config)
    installed = InstalledTemplate(id=uuid.uuid4(), organization_id=organization_id, template_id=template.id, template_version_id=version.id,
                                  package_id=package_id, config=variables, installed_by=user_id)
    db.add(installed)
    db.flush()
    wf = wfs.create_workflow(db, organization_id, template.name, description=template.description,
                             definition=copy.deepcopy(version.definition), variables=variables, created_by=user_id,
                             installed_template_id=installed.id)
    installed.workflow_id = wf.id
    db.flush()
    return installed


def configure_installed(db: Session, installed: InstalledTemplate, config: dict) -> InstalledTemplate:
    """Update configuration. Variables apply to future executions immediately (no republish needed)."""
    version = db.get(TemplateVersion, installed.template_version_id)
    variables = normalize_config(version.config_schema if version else {}, config, require=False)
    installed.config = variables
    wf = db.get(Workflow, installed.workflow_id) if installed.workflow_id else None
    if wf and not wf.deleted_at:
        wf.variables = variables
    db.flush()
    return installed


def missing_config(db: Session, installed: InstalledTemplate) -> list[str]:
    version = db.get(TemplateVersion, installed.template_version_id)
    schema = version.config_schema if version else {}
    props = schema.get("properties") or {}
    return [props.get(n, {}).get("title", n) for n in schema.get("required", []) if (installed.config or {}).get(n) in (None, "")]


def update_installed(db: Session, installed: InstalledTemplate) -> bool:
    """Bring an installed template to the template's latest version by replacing
    the workflow *draft*. The published version keeps running until the engineer
    reviews and republishes. Returns False when already up to date."""
    template = db.get(AutomationTemplate, installed.template_id)
    if not template:
        raise NotFound("Template not found")
    latest = current_version(db, template)
    if latest.id == installed.template_version_id:
        return False
    wf = db.get(Workflow, installed.workflow_id) if installed.workflow_id else None
    if not wf or wf.deleted_at:
        raise Conflict("The workflow created by this template no longer exists")
    wfs.save_draft(db, wf, copy.deepcopy(latest.definition))
    installed.template_version_id = latest.id
    installed.config = normalize_config(latest.config_schema, installed.config, require=False)
    wf.variables = installed.config
    db.flush()
    return True


def install_package(db: Session, organization_id: uuid.UUID, package: IndustryPackage, user_id: uuid.UUID | None,
                    template_ids: list[uuid.UUID] | None = None, shared_config: dict | None = None) -> list[InstalledTemplate]:
    """Install the selected (default: all) templates of a package that are not installed yet. A package
    with an AI team first creates (or reuses) the team; its agent ids become the workflows' variables."""
    if package.team_key:
        from app.ai.service import default_model
        from app.templates.teams import install_team

        provider, model = default_model(db, organization_id)
        shared_config = {**(shared_config or {}), **install_team(db, organization_id, package.team_key, shared_config or {},
                                                                provider=provider, model=model)}
    items = db.scalars(select(IndustryPackageItem).where(IndustryPackageItem.package_id == package.id).order_by(IndustryPackageItem.position)).all()
    already = set(db.scalars(select(InstalledTemplate.template_id).where(
        InstalledTemplate.organization_id == organization_id, InstalledTemplate.status != "removed")))
    out = []
    for item in items:
        if template_ids is not None and item.template_id not in template_ids and not item.is_required:
            continue
        if item.template_id in already:
            continue
        template = db.get(AutomationTemplate, item.template_id)
        if template and template.status == "published":
            out.append(install_template(db, organization_id, template, shared_config, user_id, package_id=package.id))
    return out
