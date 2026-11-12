"""Organizations, members, client profiles and workspaces."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import EmailStr, Field
from sqlalchemy import func, or_, select

from app.api import schemas
from app.api.common import ApiRoute
from app.api.common import Body, Paging, iso, owned, paginate, parse_uuid
from app.core.errors import AppError, Conflict, Forbidden, NotFound, ValidationFailed
from app.core.security import hash_password, validate_password_strength
from app.db import utcnow
from app.deps import Ctx, authenticate, org_ctx, platform_ctx, resolve_org
from app.models import CLIENT_ROLES, Client, Organization, OrganizationMember, User, UserSession, Workflow, Workspace
from app.services import accounts
from app.services.audit import audit
from app.services.organizations import create_organization
from app.services.workflows import slugify

router = APIRouter(route_class=ApiRoute, tags=["organizations"])


def org_view(o: Organization) -> dict:
    return {"id": str(o.id), "name": o.name, "slug": o.slug, "status": o.status, "mode": o.mode, "industry": o.industry,
            "settings": o.settings or {}, "created_at": iso(o.created_at)}


def client_view(c: Client | None) -> dict | None:
    if not c:
        return None
    return {"legal_name": c.legal_name, "contact_name": c.contact_name, "contact_email": c.contact_email, "contact_phone": c.contact_phone,
            "country": c.country, "timezone": c.timezone, "notes": c.notes, "status": c.status,
            "account_manager_id": str(c.account_manager_id) if c.account_manager_id else None}


class ClientBody(Body):
    legal_name: str | None = Field(None, max_length=200)
    contact_name: str | None = Field(None, max_length=200)
    contact_email: str | None = Field(None, max_length=320)
    contact_phone: str | None = Field(None, max_length=50)
    country: str | None = Field(None, max_length=80)
    timezone: str | None = Field(None, max_length=64)
    notes: str | None = Field(None, max_length=10000)
    status: Literal["lead", "onboarding", "active", "paused", "churned"] | None = None


class OrgCreate(Body):
    name: str = Field(..., min_length=2, max_length=200)
    industry: str | None = Field(None, max_length=80)
    mode: Literal["agency", "saas"] = "agency"
    plan_key: str | None = None
    client: ClientBody | None = None


class OrgUpdate(Body):
    name: str | None = Field(None, min_length=2, max_length=200)
    industry: str | None = Field(None, max_length=80)
    mode: Literal["agency", "saas"] | None = None
    status: Literal["active", "suspended", "archived"] | None = None
    ai_default: dict | None = Field(None, description="{provider, model} default for AI nodes")


@router.get("/organizations", response_model=schemas.Page[schemas.OrganizationOut])
def list_organizations(q: str | None = None, status: str | None = None, paging: Paging = Depends(), ctx: Ctx = Depends(authenticate)) -> dict:
    db = ctx.db
    query = select(Organization).where(Organization.deleted_at.is_(None))
    if not ctx.is_staff:  # client users only ever see their own organizations
        query = query.join(OrganizationMember, OrganizationMember.organization_id == Organization.id).where(
            OrganizationMember.user_id == ctx.user.id, OrganizationMember.status == "active")
    if q:
        like = f"%{q.lower()}%"
        query = query.where(or_(func.lower(Organization.name).like(like), Organization.slug.like(like)))
    if status:
        query = query.where(Organization.status == status)
    page = paginate(db, query.order_by(Organization.name), paging, org_view)
    ids = [parse_uuid(i["id"]) for i in page["items"]]
    if ids:
        members = dict(db.execute(select(OrganizationMember.organization_id, func.count()).where(OrganizationMember.organization_id.in_(ids))
                                  .group_by(OrganizationMember.organization_id)).all())
        if ctx.is_staff:
            from app.db import set_tenant

            set_tenant(db, None, bypass_rls=True)
            wfs = dict(db.execute(select(Workflow.organization_id, func.count()).where(
                Workflow.organization_id.in_(ids), Workflow.is_active.is_(True), Workflow.deleted_at.is_(None)).group_by(Workflow.organization_id)).all())
            clients = {c.organization_id: c for c in db.scalars(select(Client).where(Client.organization_id.in_(ids)))}
        else:
            wfs, clients = {}, {}
        for item in page["items"]:
            oid = parse_uuid(item["id"])
            item["member_count"] = members.get(oid, 0)
            item["active_workflows"] = wfs.get(oid)
            item["client_status"] = clients[oid].status if oid in clients else None
    return page


@router.post("/organizations", response_model=schemas.OrganizationOut, status_code=201)
def create_org(body: OrgCreate, ctx: Ctx = Depends(platform_ctx("platform.organizations.create"))) -> dict:
    client = body.client.model_dump(exclude_none=True) if body.client else None
    org = create_organization(ctx.db, name=body.name, industry=body.industry, mode=body.mode, plan_key=body.plan_key, client=client)
    audit(ctx.db, "organization.created", ctx=ctx, organization_id=org.id, resource_type="organization", resource_id=org.id, meta={"name": org.name})
    ctx.db.commit()
    return org_view(org)


@router.get("/organizations/{org_id}", response_model=schemas.OrganizationDetail)
def get_org(org_id: str, ctx: Ctx = Depends(authenticate)) -> dict:
    resolve_org(ctx, parse_uuid(org_id, "Organization"))
    ctx.require("org.read")
    client = ctx.db.scalar(select(Client).where(Client.organization_id == ctx.org_id))
    return {**org_view(ctx.org), "client": client_view(client), "role": ctx.role, "permissions": sorted(ctx.permissions)}


@router.patch("/organizations/{org_id}", response_model=schemas.OrganizationOut)
def update_org(org_id: str, body: OrgUpdate, ctx: Ctx = Depends(authenticate)) -> dict:
    resolve_org(ctx, parse_uuid(org_id, "Organization"))
    ctx.require("org.manage")
    org = ctx.org
    changes = body.model_dump(exclude_none=True)
    # Lifecycle and operating mode are Weakless decisions, not client decisions.
    if ("status" in changes or "mode" in changes) and not ctx.is_staff:
        raise Forbidden("Only Weakless staff can change organization status or mode")
    for field in ("name", "industry", "mode", "status"):
        if field in changes:
            setattr(org, field, changes[field])
    if "ai_default" in changes:
        d = changes["ai_default"]
        if not isinstance(d.get("provider"), str) or not isinstance(d.get("model"), str):
            raise ValidationFailed("ai_default must be {provider, model}")
        org.settings = {**(org.settings or {}), "ai": {"provider": d["provider"], "model": d["model"]}}
    audit(ctx.db, "organization.updated", ctx=ctx, resource_type="organization", resource_id=org.id, meta=changes)
    ctx.db.commit()
    return org_view(org)


@router.delete("/organizations/{org_id}", response_model=schemas.Ok)
def archive_org(org_id: str, ctx: Ctx = Depends(platform_ctx("platform.organizations.create"))) -> dict:
    org = ctx.db.get(Organization, parse_uuid(org_id, "Organization"))
    if not org or org.deleted_at:
        raise NotFound("Organization not found")
    # Soft delete: data is retained; workflows stop because triggers check the active flags.
    org.status, org.deleted_at = "archived", utcnow()
    for wf in ctx.db.scalars(select(Workflow).where(Workflow.organization_id == org.id, Workflow.is_active.is_(True))):
        from app.services.workflows import set_active

        set_active(ctx.db, wf, False)
    audit(ctx.db, "organization.archived", ctx=ctx, organization_id=org.id, resource_type="organization", resource_id=org.id)
    ctx.db.commit()
    return {"ok": True}


@router.put("/organizations/{org_id}/client", response_model=schemas.ClientProfile)
def update_client(org_id: str, body: ClientBody, ctx: Ctx = Depends(authenticate)) -> dict:
    resolve_org(ctx, parse_uuid(org_id, "Organization"))
    ctx.require("org.manage")
    client = ctx.db.scalar(select(Client).where(Client.organization_id == ctx.org_id))
    if not client:
        client = Client(organization_id=ctx.org_id)
        ctx.db.add(client)
    changes = body.model_dump(exclude_none=True)
    if "status" in changes and not ctx.is_staff:
        raise Forbidden("Only Weakless staff can change the client lifecycle status")
    for k, v in changes.items():
        setattr(client, k, v)
    audit(ctx.db, "client.updated", ctx=ctx, resource_type="client", resource_id=ctx.org_id, meta={"fields": sorted(changes)})
    ctx.db.commit()
    return client_view(client)


# ----------------------------------------------------------------------------- members


def member_view(m: OrganizationMember) -> dict:
    return {"id": str(m.id), "user_id": str(m.user_id), "email": m.user.email, "full_name": m.user.full_name, "role": m.role,
            "status": m.status, "last_login_at": iso(m.user.last_login_at), "created_at": iso(m.created_at)}


class MemberCreate(Body):
    email: EmailStr
    full_name: str = Field("", max_length=200)
    role: str
    password: str | None = Field(None, max_length=200, description="Optional initial password for a new account; omit to send an invitation")


class MemberUpdate(Body):
    role: str | None = None
    status: Literal["active", "disabled"] | None = None


def _check_role(role: str) -> None:
    if role not in CLIENT_ROLES:
        raise ValidationFailed(f"Role must be one of: {', '.join(CLIENT_ROLES)}")


@router.get("/members", response_model=schemas.Page[schemas.MemberOut])
def list_members(paging: Paging = Depends(), ctx: Ctx = Depends(org_ctx("members.read"))) -> dict:
    q = select(OrganizationMember).where(OrganizationMember.organization_id == ctx.org_id).order_by(OrganizationMember.created_at)
    return paginate(ctx.db, q, paging, member_view)


@router.post("/members", response_model=schemas.MemberCreated, status_code=201)
def add_member(body: MemberCreate, ctx: Ctx = Depends(org_ctx("members.manage"))) -> dict:
    _check_role(body.role)
    db = ctx.db
    email = body.email.lower().strip()
    user = db.scalar(select(User).where(User.email == email))
    if user and user.platform_role:
        raise Conflict("Weakless staff already have access to every organization")
    if user and user.deleted_at:
        raise Conflict("This account has been removed")
    if user and db.scalar(select(OrganizationMember.id).where(OrganizationMember.organization_id == ctx.org_id, OrganizationMember.user_id == user.id)):
        raise Conflict("This user is already a member")
    invitation = None
    if not user and body.password:
        # Admin-set initial password: the account is usable immediately.
        problem = validate_password_strength(body.password)
        if problem:
            raise ValidationFailed(problem)
        user = User(email=email, full_name=body.full_name or email.split("@")[0], password_hash=hash_password(body.password))
        db.add(user)
        db.flush()
        status = "active"
    elif not user:
        user = accounts.new_pending_user(db, email, body.full_name)
        status = "invited"
    elif accounts.has_password(user):
        status = "active"  # an existing account joins directly; it already proved its owner
    else:
        status = "invited"  # someone else's invitation is still pending for this email
    m = OrganizationMember(organization_id=ctx.org_id, user_id=user.id, role=body.role, status=status)
    db.add(m)
    db.flush()
    if status == "invited":
        invitation = accounts.invite(db, user, organization=ctx.org, invited_by=ctx.user)
    audit(db, "member.added", ctx=ctx, resource_type="member", resource_id=m.id, meta={"email": email, "role": body.role, "status": status})
    db.commit()
    db.refresh(m)
    return {**member_view(m), "invitation": invitation.view() if invitation else None}


@router.post("/members/{member_id}/invitation", response_model=schemas.IssuedLinkOut)
def resend_invitation(member_id: str, ctx: Ctx = Depends(org_ctx("members.manage"))) -> dict:
    """Issue a new invitation link (the previous one stops working)."""
    m = owned(ctx.db, OrganizationMember, member_id, ctx.org_id, "Member")
    if m.status != "invited":
        raise Conflict("This member is not waiting for an invitation")
    invitation = accounts.invite(ctx.db, m.user, organization=ctx.org, invited_by=ctx.user)
    audit(ctx.db, "member.invitation_resent", ctx=ctx, resource_type="member", resource_id=m.id, meta={"email": m.user.email})
    ctx.db.commit()
    return invitation.view()


def _last_admin(ctx: Ctx, m: OrganizationMember) -> bool:
    if m.role != "CLIENT_ADMIN" or m.status != "active":
        return False
    others = ctx.db.scalar(select(func.count()).select_from(OrganizationMember).where(
        OrganizationMember.organization_id == ctx.org_id, OrganizationMember.role == "CLIENT_ADMIN",
        OrganizationMember.status == "active", OrganizationMember.id != m.id)) or 0
    return others == 0 and not ctx.is_staff


@router.patch("/members/{member_id}", response_model=schemas.MemberOut)
def update_member(member_id: str, body: MemberUpdate, ctx: Ctx = Depends(org_ctx("members.manage"))) -> dict:
    m = owned(ctx.db, OrganizationMember, member_id, ctx.org_id, "Member")
    if body.role:
        _check_role(body.role)
    if m.status == "invited" and body.status == "active":
        raise AppError("This person has not accepted the invitation yet. Resend it instead.", code="invitation_pending", status_code=409)
    demoting = (body.role and body.role != "CLIENT_ADMIN") or body.status == "disabled"
    if demoting and _last_admin(ctx, m):
        raise AppError("An organization needs at least one active Client Admin", code="last_admin", status_code=409)
    if m.user_id == ctx.user.id and demoting:
        raise AppError("You cannot demote or disable yourself", code="self_modification", status_code=409)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(m, k, v)
    if changes:
        for s in ctx.db.scalars(select(UserSession).where(UserSession.user_id == m.user_id, UserSession.revoked_at.is_(None))):
            s.revoked_at = utcnow()  # role changes take effect immediately
    audit(ctx.db, "member.updated", ctx=ctx, resource_type="member", resource_id=m.id, meta=changes)
    ctx.db.commit()
    return member_view(m)


@router.delete("/members/{member_id}", response_model=schemas.Ok)
def remove_member(member_id: str, ctx: Ctx = Depends(org_ctx("members.manage"))) -> dict:
    m = owned(ctx.db, OrganizationMember, member_id, ctx.org_id, "Member")
    if _last_admin(ctx, m):
        raise AppError("An organization needs at least one active Client Admin", code="last_admin", status_code=409)
    if m.user_id == ctx.user.id:
        raise AppError("You cannot remove yourself", code="self_modification", status_code=409)
    audit(ctx.db, "member.removed", ctx=ctx, resource_type="member", resource_id=m.id, meta={"email": m.user.email})
    ctx.db.delete(m)
    ctx.db.commit()
    return {"ok": True}


# ----------------------------------------------------------------------------- workspaces


class WorkspaceBody(Body):
    name: str = Field(..., min_length=1, max_length=200)


def ws_view(w: Workspace) -> dict:
    return {"id": str(w.id), "name": w.name, "slug": w.slug, "is_default": w.is_default, "created_at": iso(w.created_at)}


@router.get("/workspaces", response_model=schemas.Items[schemas.WorkspaceOut])
def list_workspaces(ctx: Ctx = Depends(org_ctx("org.read"))) -> dict:
    rows = ctx.db.scalars(select(Workspace).where(Workspace.organization_id == ctx.org_id).order_by(Workspace.created_at)).all()
    return {"items": [ws_view(w) for w in rows]}


@router.post("/workspaces", response_model=schemas.WorkspaceOut, status_code=201)
def create_workspace(body: WorkspaceBody, ctx: Ctx = Depends(org_ctx("org.manage"))) -> dict:
    slug = slugify(body.name)
    if ctx.db.scalar(select(Workspace.id).where(Workspace.organization_id == ctx.org_id, Workspace.slug == slug)):
        raise Conflict("A workspace with this name already exists")
    w = Workspace(organization_id=ctx.org_id, name=body.name, slug=slug)
    ctx.db.add(w)
    audit(ctx.db, "workspace.created", ctx=ctx, resource_type="workspace", resource_id=w.id, meta={"name": body.name})
    ctx.db.commit()
    return ws_view(w)


@router.delete("/workspaces/{workspace_id}", response_model=schemas.Ok)
def delete_workspace(workspace_id: str, ctx: Ctx = Depends(org_ctx("org.manage"))) -> dict:
    w = owned(ctx.db, Workspace, workspace_id, ctx.org_id, "Workspace")
    if w.is_default:
        raise Conflict("The default workspace cannot be deleted")
    if ctx.db.scalar(select(func.count()).select_from(Workflow).where(Workflow.workspace_id == w.id, Workflow.deleted_at.is_(None))):
        raise Conflict("Move or delete the workflows in this workspace first")
    ctx.db.delete(w)
    audit(ctx.db, "workspace.deleted", ctx=ctx, resource_type="workspace", resource_id=w.id)
    ctx.db.commit()
    return {"ok": True}
