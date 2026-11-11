"""Template marketplace and industry packages."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import func, or_, select

from app.api.common import Body, Paging, iso, owned, paginate, parse_uuid
from app.core.errors import Conflict, Forbidden, NotFound
from app.deps import Ctx, authenticate, org_ctx, platform_ctx
from app.models import (
    AutomationTemplate, IndustryPackage, IndustryPackageItem, InstalledTemplate, TemplateVersion, Workflow,
)
from app.rbac import role_permissions
from app.services.audit import audit
from app.templates import service
from app.templates.library import CATEGORIES

router = APIRouter(tags=["templates"])


def template_view(t: AutomationTemplate, version: TemplateVersion | None = None, full: bool = False) -> dict:
    out = {"id": str(t.id), "key": t.key, "name": t.name, "description": t.description, "category": t.category, "industry": t.industry,
           "required_integrations": t.required_integrations or [], "required_credentials": t.required_credentials or [],
           "pricing": t.pricing or {}, "status": t.status, "version": version.version if version else None, "updated_at": iso(t.updated_at)}
    if full:
        out["setup_instructions"] = t.setup_instructions
        out["definition"] = version.definition if version else None
        out["config_schema"] = version.config_schema if version else {}
    return out


def _require_catalog_access(ctx: Ctx) -> None:
    """Any staff member with templates.read, or any client user (every client role includes templates.read)."""
    if ctx.is_staff:
        if "templates.read" not in role_permissions(ctx.db, ctx.user.platform_role or ""):
            raise Forbidden("Missing permission: templates.read")


@router.get("/templates")
def list_templates(category: str | None = None, industry: str | None = None, q: str | None = None, paging: Paging = Depends(),
                   ctx: Ctx = Depends(authenticate)) -> dict:
    _require_catalog_access(ctx)
    query = select(AutomationTemplate)
    if not ctx.is_staff:
        query = query.where(AutomationTemplate.status == "published")
    if category:
        query = query.where(AutomationTemplate.category == category)
    if industry:
        query = query.where(AutomationTemplate.industry == industry)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(or_(func.lower(AutomationTemplate.name).like(like), func.lower(AutomationTemplate.description).like(like)))
    versions = {v.id: v for v in ctx.db.scalars(select(TemplateVersion))}
    page = paginate(ctx.db, query.order_by(AutomationTemplate.category, AutomationTemplate.name), paging,
                    lambda t: template_view(t, versions.get(t.current_version_id)))
    page["categories"] = CATEGORIES
    return page


@router.get("/templates/{template_id}")
def get_template(template_id: str, ctx: Ctx = Depends(authenticate)) -> dict:
    _require_catalog_access(ctx)
    t = ctx.db.get(AutomationTemplate, parse_uuid(template_id, "Template"))
    if not t or (t.status != "published" and not ctx.is_staff):
        raise NotFound("Template not found")
    version = ctx.db.get(TemplateVersion, t.current_version_id) if t.current_version_id else None
    return template_view(t, version, full=True)


class TemplateCreate(Body):
    key: str = Field(..., pattern=r"^[a-z0-9][a-z0-9\-]{2,98}$")
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=5000)
    category: str
    industry: str | None = None
    required_integrations: list[str] = Field(default_factory=list)
    required_credentials: list[str] = Field(default_factory=list)
    setup_instructions: str = ""
    pricing: dict[str, Any] = Field(default_factory=dict)
    definition: dict
    config_schema: dict = Field(default_factory=dict)


class TemplateUpdate(Body):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    category: str | None = None
    industry: str | None = None
    setup_instructions: str | None = None
    pricing: dict[str, Any] | None = None
    status: Literal["draft", "published", "deprecated"] | None = None
    definition: dict | None = Field(None, description="Supplying a definition creates a new template version")
    config_schema: dict | None = None
    changelog: str = ""


@router.post("/templates", status_code=201)
def create_template(body: TemplateCreate, ctx: Ctx = Depends(platform_ctx("platform.templates.manage"))) -> dict:
    if ctx.db.scalar(select(AutomationTemplate.id).where(AutomationTemplate.key == body.key)):
        raise Conflict("A template with this key already exists")
    data = body.model_dump(exclude={"definition", "config_schema"})
    t = AutomationTemplate(status="published", **data)
    ctx.db.add(t)
    ctx.db.flush()
    version = service.add_version(ctx.db, t, body.definition, body.config_schema or {"type": "object", "properties": {}}, "Initial version")
    audit(ctx.db, "template.created", ctx=ctx, resource_type="template", resource_id=t.id, meta={"key": t.key})
    ctx.db.commit()
    return template_view(t, version, full=True)


@router.patch("/templates/{template_id}")
def update_template(template_id: str, body: TemplateUpdate, ctx: Ctx = Depends(platform_ctx("platform.templates.manage"))) -> dict:
    t = ctx.db.get(AutomationTemplate, parse_uuid(template_id, "Template"))
    if not t:
        raise NotFound("Template not found")
    changes = body.model_dump(exclude_none=True, exclude={"definition", "config_schema", "changelog"})
    for k, v in changes.items():
        setattr(t, k, v)
    version = ctx.db.get(TemplateVersion, t.current_version_id) if t.current_version_id else None
    if body.definition is not None:
        schema = body.config_schema if body.config_schema is not None else (version.config_schema if version else {})
        version = service.add_version(ctx.db, t, body.definition, schema, body.changelog)
    audit(ctx.db, "template.updated", ctx=ctx, resource_type="template", resource_id=t.id,
          meta={"fields": sorted(changes), "new_version": version.version if body.definition is not None and version else None})
    ctx.db.commit()
    return template_view(t, version, full=True)


class WorkflowAsTemplate(Body):
    workflow_id: str
    key: str = Field(..., pattern=r"^[a-z0-9][a-z0-9\-]{2,98}$")
    name: str = Field(..., min_length=1, max_length=200)
    category: str
    industry: str | None = None
    description: str = ""


@router.post("/templates/from-workflow", status_code=201)
def template_from_workflow(body: WorkflowAsTemplate, ctx: Ctx = Depends(org_ctx("workflows.read"))) -> dict:
    """Turn a workflow into a reusable global template (a reusable automation component)."""
    if "platform.templates.manage" not in ctx.permissions:
        raise Forbidden("Missing permission: platform.templates.manage")
    from app.services.workflows import draft_definition

    wf = owned(ctx.db, Workflow, body.workflow_id, ctx.org_id, "Workflow")
    if ctx.db.scalar(select(AutomationTemplate.id).where(AutomationTemplate.key == body.key)):
        raise Conflict("A template with this key already exists")
    props = {k: {"type": "number" if isinstance(v, (int, float)) and not isinstance(v, bool) else "string", "title": k.replace("_", " ").capitalize()}
             for k, v in (wf.variables or {}).items()}
    t = AutomationTemplate(key=body.key, name=body.name, description=body.description or wf.description, category=body.category,
                           industry=body.industry, status="draft", pricing={"model": "included"})
    ctx.db.add(t)
    ctx.db.flush()
    # Variable *values* belong to the source tenant and are not copied - only their names.
    version = service.add_version(ctx.db, t, draft_definition(ctx.db, wf), {"type": "object", "properties": props, "required": []}, "Created from workflow")
    audit(ctx.db, "template.created_from_workflow", ctx=ctx, resource_type="template", resource_id=t.id, meta={"workflow_id": str(wf.id)})
    ctx.db.commit()
    return template_view(t, version, full=True)


# ----------------------------------------------------------------------------- installation


def _can_install(ctx: Ctx) -> None:
    ctx.require("templates.install")
    if not ctx.is_staff and ctx.org.mode != "saas":
        raise Forbidden("Templates for this organization are installed by your Weakless team", code="agency_managed")


def installed_view(ctx: Ctx, i: InstalledTemplate) -> dict:
    t = ctx.db.get(AutomationTemplate, i.template_id)
    version = ctx.db.get(TemplateVersion, i.template_version_id)
    latest = ctx.db.get(TemplateVersion, t.current_version_id) if t and t.current_version_id else None
    wf = ctx.db.get(Workflow, i.workflow_id) if i.workflow_id else None
    return {"id": str(i.id), "template_id": str(i.template_id), "template_key": t.key if t else None, "template_name": t.name if t else None,
            "category": t.category if t else None, "workflow_id": str(i.workflow_id) if i.workflow_id and wf and not wf.deleted_at else None,
            "workflow_active": bool(wf and wf.is_active), "workflow_published": bool(wf and wf.current_version_id),
            "version": version.version if version else None, "latest_version": latest.version if latest else None,
            "update_available": bool(latest and version and latest.id != version.id),
            "config": i.config or {}, "config_schema": version.config_schema if version else {},
            "missing_config": service.missing_config(ctx.db, i), "status": i.status, "installed_at": iso(i.created_at)}


class InstallBody(Body):
    config: dict[str, Any] = Field(default_factory=dict)


@router.post("/templates/{template_id}/install", status_code=201)
def install(template_id: str, body: InstallBody | None = None, ctx: Ctx = Depends(org_ctx("templates.read"))) -> dict:
    _can_install(ctx)
    t = ctx.db.get(AutomationTemplate, parse_uuid(template_id, "Template"))
    if not t or t.status != "published":
        raise NotFound("Template not found")
    installed = service.install_template(ctx.db, ctx.org_id, t, (body.config if body else {}), ctx.user.id)
    audit(ctx.db, "template.installed", ctx=ctx, resource_type="template", resource_id=t.id, meta={"key": t.key, "workflow_id": str(installed.workflow_id)})
    ctx.db.commit()
    return installed_view(ctx, installed)


@router.get("/installed-templates")
def list_installed(paging: Paging = Depends(), ctx: Ctx = Depends(org_ctx("templates.read"))) -> dict:
    q = select(InstalledTemplate).where(InstalledTemplate.organization_id == ctx.org_id, InstalledTemplate.status != "removed") \
        .order_by(InstalledTemplate.created_at.desc())
    return paginate(ctx.db, q, paging, lambda i: installed_view(ctx, i))


@router.put("/installed-templates/{installed_id}/config")
def configure(installed_id: str, body: InstallBody, ctx: Ctx = Depends(org_ctx("templates.read"))) -> dict:
    _can_install(ctx)
    installed = owned(ctx.db, InstalledTemplate, installed_id, ctx.org_id, "Installed template")
    service.configure_installed(ctx.db, installed, body.config)
    audit(ctx.db, "template.configured", ctx=ctx, resource_type="installed_template", resource_id=installed.id, meta={"fields": sorted(body.config)})
    ctx.db.commit()
    return installed_view(ctx, installed)


@router.post("/installed-templates/{installed_id}/update")
def update_installed(installed_id: str, ctx: Ctx = Depends(org_ctx("templates.read"))) -> dict:
    _can_install(ctx)
    installed = owned(ctx.db, InstalledTemplate, installed_id, ctx.org_id, "Installed template")
    changed = service.update_installed(ctx.db, installed)
    if changed:
        audit(ctx.db, "template.updated_installation", ctx=ctx, resource_type="installed_template", resource_id=installed.id)
    ctx.db.commit()
    return {"updated": changed, "installed": installed_view(ctx, installed)}


@router.delete("/installed-templates/{installed_id}")
def uninstall(installed_id: str, ctx: Ctx = Depends(org_ctx("templates.read"))) -> dict:
    """Mark the installation removed. The workflow is kept (deactivated) so history is preserved."""
    _can_install(ctx)
    installed = owned(ctx.db, InstalledTemplate, installed_id, ctx.org_id, "Installed template")
    installed.status = "removed"
    wf = ctx.db.get(Workflow, installed.workflow_id) if installed.workflow_id else None
    if wf and wf.is_active:
        from app.services.workflows import set_active

        set_active(ctx.db, wf, False)
    audit(ctx.db, "template.uninstalled", ctx=ctx, resource_type="installed_template", resource_id=installed.id)
    ctx.db.commit()
    return {"ok": True}


# ----------------------------------------------------------------------------- industry packages


def package_view(ctx: Ctx, p: IndustryPackage, full: bool = False) -> dict:
    items = ctx.db.execute(select(IndustryPackageItem, AutomationTemplate).join(AutomationTemplate, AutomationTemplate.id == IndustryPackageItem.template_id)
                           .where(IndustryPackageItem.package_id == p.id).order_by(IndustryPackageItem.position)).all()
    out = {"id": str(p.id), "key": p.key, "name": p.name, "description": p.description, "industry": p.industry, "pricing": p.pricing or {},
           "status": p.status, "template_count": len(items)}
    if full:
        out["templates"] = [{**template_view(t), "is_required": item.is_required, "position": item.position} for item, t in items]
    else:
        out["template_names"] = [t.name for _, t in items]
    return out


@router.get("/industry-packages")
def list_packages(ctx: Ctx = Depends(authenticate)) -> dict:
    _require_catalog_access(ctx)
    q = select(IndustryPackage).order_by(IndustryPackage.name)
    if not ctx.is_staff:
        q = q.where(IndustryPackage.status == "published")
    return {"items": [package_view(ctx, p) for p in ctx.db.scalars(q)]}


@router.get("/industry-packages/{package_id}")
def get_package(package_id: str, ctx: Ctx = Depends(authenticate)) -> dict:
    _require_catalog_access(ctx)
    p = ctx.db.get(IndustryPackage, parse_uuid(package_id, "Package"))
    if not p or (p.status != "published" and not ctx.is_staff):
        raise NotFound("Package not found")
    return package_view(ctx, p, full=True)


class PackageBody(Body):
    key: str = Field(..., pattern=r"^[a-z0-9][a-z0-9\-]{2,98}$")
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    industry: str = Field(..., min_length=1, max_length=80)
    pricing: dict[str, Any] = Field(default_factory=dict)
    status: Literal["draft", "published", "deprecated"] = "published"
    template_ids: list[str] = Field(default_factory=list)


def _set_items(ctx: Ctx, p: IndustryPackage, template_ids: list[str]) -> None:
    for item in ctx.db.scalars(select(IndustryPackageItem).where(IndustryPackageItem.package_id == p.id)):
        ctx.db.delete(item)
    ctx.db.flush()
    for pos, tid in enumerate(dict.fromkeys(template_ids)):
        t = ctx.db.get(AutomationTemplate, parse_uuid(tid, "Template"))
        if not t:
            raise NotFound(f"Template {tid} not found")
        ctx.db.add(IndustryPackageItem(package_id=p.id, template_id=t.id, position=pos))
    ctx.db.flush()


@router.post("/industry-packages", status_code=201)
def create_package(body: PackageBody, ctx: Ctx = Depends(platform_ctx("platform.templates.manage"))) -> dict:
    if ctx.db.scalar(select(IndustryPackage.id).where(IndustryPackage.key == body.key)):
        raise Conflict("A package with this key already exists")
    p = IndustryPackage(**body.model_dump(exclude={"template_ids"}))
    ctx.db.add(p)
    ctx.db.flush()
    _set_items(ctx, p, body.template_ids)
    audit(ctx.db, "package.created", ctx=ctx, resource_type="package", resource_id=p.id, meta={"key": p.key})
    ctx.db.commit()
    return package_view(ctx, p, full=True)


@router.put("/industry-packages/{package_id}")
def update_package(package_id: str, body: PackageBody, ctx: Ctx = Depends(platform_ctx("platform.templates.manage"))) -> dict:
    p = ctx.db.get(IndustryPackage, parse_uuid(package_id, "Package"))
    if not p:
        raise NotFound("Package not found")
    for k, v in body.model_dump(exclude={"template_ids", "key"}).items():
        setattr(p, k, v)
    _set_items(ctx, p, body.template_ids)
    audit(ctx.db, "package.updated", ctx=ctx, resource_type="package", resource_id=p.id)
    ctx.db.commit()
    return package_view(ctx, p, full=True)


class PackageInstall(Body):
    template_ids: list[str] | None = Field(None, description="Subset to install; default is every template in the package")
    config: dict[str, Any] = Field(default_factory=dict, description="Shared configuration applied to each template that defines the key")


@router.post("/industry-packages/{package_id}/install", status_code=201)
def install_package(package_id: str, body: PackageInstall | None = None, ctx: Ctx = Depends(org_ctx("templates.read"))) -> dict:
    _can_install(ctx)
    p = ctx.db.get(IndustryPackage, parse_uuid(package_id, "Package"))
    if not p or p.status != "published":
        raise NotFound("Package not found")
    body = body or PackageInstall()
    ids = [parse_uuid(t, "Template") for t in body.template_ids] if body.template_ids is not None else None
    installed = service.install_package(ctx.db, ctx.org_id, p, ctx.user.id, ids, body.config)
    audit(ctx.db, "package.installed", ctx=ctx, resource_type="package", resource_id=p.id, meta={"key": p.key, "installed": len(installed)})
    ctx.db.commit()
    return {"installed": [installed_view(ctx, i) for i in installed]}
