"""Workflows, versions, executions, approvals and the dead-letter queue."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header
from pydantic import Field
from sqlalchemy import func, select

from app.api import schemas
from app.api.common import ApiRoute
from app.api.common import Body, Paging, iso, owned, paginate, parse_dt, parse_uuid
from app.config import get_settings
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db import set_tenant, utcnow
from app.deps import Ctx, optional_org_ctx, org_ctx, platform_ctx
from app.engine.graph import validate_definition
from app.engine.registry import all_specs
from app.models import (
    EXECUTION_STATUSES, ApprovalRequest, ExecutionQueueJob, GeneratedDocument, Organization, WebhookEndpoint, Workflow,
    WorkflowExecution, WorkflowExecutionStep, WorkflowSchedule, WorkflowVersion, Workspace,
)
from app.models import MailboxWatch
from app.services import workflows as wfs
from app.services.approvals import decide_approval
from app.services.audit import audit
from app.services.executions import cancel_execution, resumable_state, resume_execution, retry_execution, start_execution

router = APIRouter(route_class=ApiRoute, tags=["workflows"])


@router.get("/locate/{kind}/{resource_id}", response_model=schemas.LocateOut)
def locate(kind: str, resource_id: str, ctx: Ctx = Depends(platform_ctx())) -> dict:
    """Staff only: which organization owns a workflow or agent, so a deep link can open in the right tenant."""
    from app.models import AIAgent

    model = {"workflow": Workflow, "agent": AIAgent}.get(kind)
    obj = ctx.db.get(model, parse_uuid(resource_id, "Resource")) if model else None
    if obj is None or obj.deleted_at:
        raise NotFound("Resource not found")
    return {"organization_id": str(obj.organization_id)}


@router.get("/node-types", response_model=schemas.Items[schemas.NodeTypeOut])
def node_types(ctx: Ctx = Depends(org_ctx("workflows.read"))) -> dict:
    """The controlled node registry: everything the builder can place on the canvas."""
    return {"items": [s.describe() for s in all_specs()]}


def wf_view(w: Workflow, extra: dict | None = None) -> dict:
    return {"id": str(w.id), "name": w.name, "description": w.description, "status": w.status, "is_active": w.is_active,
            "workspace_id": str(w.workspace_id), "current_version_id": str(w.current_version_id) if w.current_version_id else None,
            "draft_revision": w.draft_revision, "variables": w.variables or {}, "settings": w.settings or {},
            "installed_template_id": str(w.installed_template_id) if w.installed_template_id else None,
            "created_at": iso(w.created_at), "updated_at": iso(w.updated_at), **(extra or {})}


class WorkflowCreate(Body):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=5000)
    workspace_id: str | None = None
    definition: dict | None = None


class WorkflowUpdate(Body):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    variables: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


class DraftBody(Body):
    definition: dict
    base_revision: int | None = Field(None, description="Reject the save if the draft changed since this revision")


@router.get("/workflows", response_model=schemas.Page[schemas.WorkflowOut])
def list_workflows(q: str | None = None, active: bool | None = None, paging: Paging = Depends(), ctx: Ctx = Depends(org_ctx("workflows.read"))) -> dict:
    db = ctx.db
    query = select(Workflow).where(Workflow.organization_id == ctx.org_id, Workflow.deleted_at.is_(None))
    if q:
        query = query.where(func.lower(Workflow.name).like(f"%{q.lower()}%"))
    if active is not None:
        query = query.where(Workflow.is_active.is_(active))
    page = paginate(db, query.order_by(Workflow.updated_at.desc()), paging, wf_view)
    ids = [parse_uuid(i["id"]) for i in page["items"]]
    if ids:
        E = WorkflowExecution
        stats = {r[0]: r for r in db.execute(select(E.workflow_id, func.count(), func.max(E.created_at)).where(
            E.workflow_id.in_(ids), E.is_test.is_(False)).group_by(E.workflow_id))}
        versions = dict(db.execute(select(WorkflowVersion.workflow_id, func.max(WorkflowVersion.version)).where(
            WorkflowVersion.workflow_id.in_(ids)).group_by(WorkflowVersion.workflow_id)).all())
        for item in page["items"]:
            wid = parse_uuid(item["id"])
            item["executions"] = stats[wid][1] if wid in stats else 0
            item["last_execution_at"] = iso(stats[wid][2]) if wid in stats else None
            item["version"] = versions.get(wid)
    return page


@router.post("/workflows", response_model=schemas.WorkflowOut, status_code=201)
def create_workflow(body: WorkflowCreate, ctx: Ctx = Depends(org_ctx("workflows.write"))) -> dict:
    ws_id = owned(ctx.db, Workspace, body.workspace_id, ctx.org_id, "Workspace").id if body.workspace_id else None
    definition = body.definition or {
        "nodes": [{"id": "trigger", "type": "trigger.manual", "name": "Manual Trigger", "config": {}, "position": {"x": 120, "y": 160}}], "edges": []}
    wf = wfs.create_workflow(ctx.db, ctx.org_id, body.name, description=body.description, definition=definition,
                             workspace_id=ws_id, created_by=ctx.user.id)
    audit(ctx.db, "workflow.created", ctx=ctx, resource_type="workflow", resource_id=wf.id, meta={"name": wf.name})
    ctx.db.commit()
    return wf_view(wf)


def _triggers(ctx: Ctx, wf: Workflow) -> dict:
    base = get_settings().public_base_url.rstrip("/")
    eps = ctx.db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.workflow_id == wf.id)).all()
    schedules = ctx.db.scalars(select(WorkflowSchedule).where(WorkflowSchedule.workflow_id == wf.id)).all()
    return {
        "endpoints": [{"id": str(e.id), "node_id": e.node_key, "kind": e.kind, "signature_scheme": e.signature_scheme,
                       "url": f"{base}/api/v1/hooks/{e.endpoint_key}", "has_secret": bool(e.secret_envelope), "is_active": e.is_active,
                       "rate_limit_per_minute": e.rate_limit_per_minute, "last_received_at": iso(e.last_received_at)} for e in eps],
        "schedules": [{"node_id": s.node_key, "cron": s.cron, "timezone": s.timezone, "is_active": s.is_active,
                       "next_run_at": iso(s.next_run_at), "last_run_at": iso(s.last_run_at)} for s in schedules],
        "mailboxes": [{"node_id": m.node_key, "integration_id": str(m.integration_id), "folder": m.folder, "is_active": m.is_active,
                       "poll_minutes": m.poll_minutes, "next_poll_at": iso(m.next_poll_at), "last_polled_at": iso(m.last_polled_at),
                       "watching": m.last_uid is not None, "last_error": m.last_error}
                      for m in ctx.db.scalars(select(MailboxWatch).where(MailboxWatch.workflow_id == wf.id))],
    }


@router.get("/workflows/{workflow_id}", response_model=schemas.WorkflowDetail)
def get_workflow(workflow_id: str, ctx: Ctx = Depends(org_ctx("workflows.read"))) -> dict:
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    definition = wfs.draft_definition(ctx.db, wf)
    version = ctx.db.get(WorkflowVersion, wf.current_version_id) if wf.current_version_id else None
    return wf_view(wf, {
        "definition": definition, "issues": validate_definition(definition), "triggers": _triggers(ctx, wf),
        "published_version": version.version if version else None,
        "has_unpublished_changes": bool(version is None or version.definition != definition),
    })


@router.patch("/workflows/{workflow_id}", response_model=schemas.WorkflowOut)
def update_workflow(workflow_id: str, body: WorkflowUpdate, ctx: Ctx = Depends(org_ctx("workflows.write"))) -> dict:
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    changes = body.model_dump(exclude_none=True)
    if "settings" in changes:
        t = changes["settings"].get("timeout_seconds")
        if t is not None and not (isinstance(t, (int, float)) and 1 <= t <= 86400):
            raise ValidationFailed("settings.timeout_seconds must be between 1 and 86400")
    for k, v in changes.items():
        setattr(wf, k, v)
    audit(ctx.db, "workflow.updated", ctx=ctx, resource_type="workflow", resource_id=wf.id, meta={"fields": sorted(changes)})
    ctx.db.commit()
    return wf_view(wf)


@router.delete("/workflows/{workflow_id}", response_model=schemas.Ok)
def delete_workflow(workflow_id: str, ctx: Ctx = Depends(org_ctx("workflows.write"))) -> dict:
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    if wf.is_active:
        wfs.set_active(ctx.db, wf, False)
    for ep in ctx.db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.workflow_id == wf.id)):
        ep.is_active = False
    wf.deleted_at = utcnow()
    audit(ctx.db, "workflow.deleted", ctx=ctx, resource_type="workflow", resource_id=wf.id, meta={"name": wf.name})
    ctx.db.commit()
    return {"ok": True}


@router.put("/workflows/{workflow_id}/draft", response_model=schemas.DraftSaved)
def save_draft(workflow_id: str, body: DraftBody, ctx: Ctx = Depends(org_ctx("workflows.write"))) -> dict:
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    if body.base_revision is not None and body.base_revision != wf.draft_revision:
        raise Conflict("This workflow was changed by someone else. Reload to see the latest draft.", code="stale_draft")
    issues = wfs.save_draft(ctx.db, wf, body.definition)
    ctx.db.commit()
    return {"draft_revision": wf.draft_revision, "issues": issues, "triggers": _triggers(ctx, wf)}


@router.post("/workflows/{workflow_id}/validate", response_model=schemas.IssuesOut)
def validate(workflow_id: str, ctx: Ctx = Depends(org_ctx("workflows.read"))) -> dict:
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    return {"issues": validate_definition(wfs.draft_definition(ctx.db, wf))}


class PublishBody(Body):
    notes: str = Field("", max_length=2000)


def version_view(v: WorkflowVersion, with_definition: bool = False) -> dict:
    out = {"id": str(v.id), "version": v.version, "notes": v.notes, "created_at": iso(v.created_at),
           "published_by": str(v.published_by) if v.published_by else None, "node_count": len(v.definition.get("nodes", []))}
    if with_definition:
        out["definition"], out["variables"] = v.definition, v.variables
    return out


@router.post("/workflows/{workflow_id}/publish", response_model=schemas.VersionOut)
def publish(workflow_id: str, body: PublishBody | None = None, ctx: Ctx = Depends(org_ctx("workflows.publish"))) -> dict:
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    version = wfs.publish(ctx.db, wf, ctx.user.id, body.notes if body else "")
    audit(ctx.db, "workflow.published", ctx=ctx, resource_type="workflow", resource_id=wf.id, meta={"version": version.version})
    ctx.db.commit()
    return version_view(version)


@router.get("/workflows/{workflow_id}/versions", response_model=schemas.Items[schemas.VersionOut])
def list_versions(workflow_id: str, ctx: Ctx = Depends(org_ctx("workflows.read"))) -> dict:
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    rows = ctx.db.scalars(select(WorkflowVersion).where(WorkflowVersion.workflow_id == wf.id).order_by(WorkflowVersion.version.desc())).all()
    return {"items": [{**version_view(v), "is_current": v.id == wf.current_version_id} for v in rows]}


@router.get("/workflows/{workflow_id}/versions/{version_id}", response_model=schemas.VersionDetail)
def get_version(workflow_id: str, version_id: str, ctx: Ctx = Depends(org_ctx("workflows.read"))) -> dict:
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    v = owned(ctx.db, WorkflowVersion, version_id, ctx.org_id, "Version")
    if v.workflow_id != wf.id:
        raise NotFound("Version not found")
    return version_view(v, with_definition=True)


@router.post("/workflows/{workflow_id}/versions/{version_id}/restore", response_model=schemas.DraftRevision)
def restore_version(workflow_id: str, version_id: str, ctx: Ctx = Depends(org_ctx("workflows.write"))) -> dict:
    """Copy a published version back into the draft (published versions themselves are immutable)."""
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    v = owned(ctx.db, WorkflowVersion, version_id, ctx.org_id, "Version")
    if v.workflow_id != wf.id:
        raise NotFound("Version not found")
    wfs.save_draft(ctx.db, wf, v.definition)
    audit(ctx.db, "workflow.version_restored", ctx=ctx, resource_type="workflow", resource_id=wf.id, meta={"version": v.version})
    ctx.db.commit()
    return {"draft_revision": wf.draft_revision}


@router.post("/workflows/{workflow_id}/activate", response_model=schemas.WorkflowOut)
def activate(workflow_id: str, ctx: Ctx = Depends(org_ctx("workflows.activate"))) -> dict:
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    wfs.set_active(ctx.db, wf, True)
    audit(ctx.db, "workflow.activated", ctx=ctx, resource_type="workflow", resource_id=wf.id)
    ctx.db.commit()
    return wf_view(wf)


@router.post("/workflows/{workflow_id}/deactivate", response_model=schemas.WorkflowOut)
def deactivate(workflow_id: str, ctx: Ctx = Depends(org_ctx("workflows.activate"))) -> dict:
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    wfs.set_active(ctx.db, wf, False)
    audit(ctx.db, "workflow.deactivated", ctx=ctx, resource_type="workflow", resource_id=wf.id)
    ctx.db.commit()
    return wf_view(wf)


class ExecuteBody(Body):
    input: dict[str, Any] = Field(default_factory=dict)
    trigger_node: str | None = None


@router.post("/workflows/{workflow_id}/execute", response_model=schemas.ExecutionOut, status_code=202)
def execute(workflow_id: str, body: ExecuteBody | None = None, idempotency_key: str | None = Header(None, max_length=200),
            ctx: Ctx = Depends(org_ctx("workflows.execute"))) -> dict:
    """Run the published version. Send an ``Idempotency-Key`` header to make retries of this call safe."""
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    body = body or ExecuteBody()
    ex = start_execution(ctx.db, wf, trigger_type="manual", trigger_node=body.trigger_node, payload={"input": body.input, **body.input},
                         idempotency_key=idempotency_key, created_by=ctx.user.id)
    ctx.db.commit()
    return exec_view(ex)


@router.post("/workflows/{workflow_id}/test", response_model=schemas.ExecutionOut, status_code=202)
def test_run(workflow_id: str, body: ExecuteBody | None = None, ctx: Ctx = Depends(org_ctx("workflows.write"))) -> dict:
    """Run the current *draft* as a test execution. Test runs perform real actions but are excluded from quotas and analytics."""
    wf = owned(ctx.db, Workflow, workflow_id, ctx.org_id, "Workflow")
    body = body or ExecuteBody()
    ex = start_execution(ctx.db, wf, trigger_type="manual", trigger_node=body.trigger_node, payload={"input": body.input, **body.input},
                         is_test=True, definition=wfs.draft_definition(ctx.db, wf), created_by=ctx.user.id)
    ctx.db.commit()
    return exec_view(ex)


# ----------------------------------------------------------------------------- executions


def exec_view(e: WorkflowExecution, names: dict | None = None, orgs: dict | None = None) -> dict:
    return {"id": str(e.id), "workflow_id": str(e.workflow_id), "workflow_name": (names or {}).get(e.workflow_id),
            "organization_id": str(e.organization_id), "organization_name": (orgs or {}).get(e.organization_id),
            "status": e.status, "trigger_type": e.trigger_type, "is_test": e.is_test, "error": e.error,
            "retry_of_id": str(e.retry_of_id) if e.retry_of_id else None, "created_at": iso(e.created_at),
            "started_at": iso(e.started_at), "finished_at": iso(e.finished_at), "duration_ms": e.duration_ms}


@router.get("/executions", response_model=schemas.Page[schemas.ExecutionOut])
def list_executions(status: str | None = None, workflow_id: str | None = None, start: str | None = None, end: str | None = None,
                    include_tests: bool = False, paging: Paging = Depends(), ctx: Ctx = Depends(optional_org_ctx("executions.read"))) -> dict:
    db = ctx.db
    E = WorkflowExecution
    q = select(E)
    if ctx.org:
        q = q.where(E.organization_id == ctx.org_id)
    if status:
        if status not in EXECUTION_STATUSES:
            raise ValidationFailed(f"status must be one of {', '.join(EXECUTION_STATUSES)}")
        q = q.where(E.status == status)
    if workflow_id:
        q = q.where(E.workflow_id == parse_uuid(workflow_id, "Workflow"))
    if not include_tests:
        q = q.where(E.is_test.is_(False))
    if (s := parse_dt(start, "start")):
        q = q.where(E.created_at >= s)
    if (e := parse_dt(end, "end")):
        q = q.where(E.created_at <= e)
    page = paginate(db, q.order_by(E.created_at.desc()), paging, lambda x: x)
    rows = page["items"]
    names = dict(db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_({r.workflow_id for r in rows} or {uuid.uuid4()}))).all())
    orgs = dict(db.execute(select(Organization.id, Organization.name).where(Organization.id.in_({r.organization_id for r in rows} or {uuid.uuid4()}))).all())
    page["items"] = [exec_view(r, names, orgs) for r in rows]
    return page


def _get_execution(ctx: Ctx, execution_id: str) -> WorkflowExecution:
    ex = ctx.db.get(WorkflowExecution, parse_uuid(execution_id, "Execution"))
    if not ex or (ctx.org and ex.organization_id != ctx.org_id):
        raise NotFound("Execution not found")
    return ex


@router.get("/executions/{execution_id}", response_model=schemas.ExecutionDetail)
def get_execution(execution_id: str, ctx: Ctx = Depends(optional_org_ctx("executions.read"))) -> dict:
    db = ctx.db
    ex = _get_execution(ctx, execution_id)
    wf = db.get(Workflow, ex.workflow_id)
    steps = db.scalars(select(WorkflowExecutionStep).where(WorkflowExecutionStep.execution_id == ex.id).order_by(WorkflowExecutionStep.sequence).limit(2000)).all()
    docs = db.scalars(select(GeneratedDocument).where(GeneratedDocument.execution_id == ex.id)).all()
    version = db.get(WorkflowVersion, ex.workflow_version_id) if ex.workflow_version_id else None
    return {
        **exec_view(ex, {ex.workflow_id: wf.name if wf else None}),
        "version": version.version if version else None,
        "trigger_node": ex.trigger_node, "trigger_payload": ex.trigger_payload, "output": ex.output,
        "waiting": [{"node_id": k, "kind": v.get("kind"), "resume_at": v.get("resume_at")} for k, v in (ex.state or {}).get("waiting", {}).items()],
        "steps": [{"id": str(s.id), "sequence": s.sequence, "node_id": s.node_key, "node_type": s.node_type, "node_name": s.node_name,
                   "iteration": s.iteration, "status": s.status, "attempt": s.attempt, "input": s.input, "output": s.output, "error": s.error,
                   "started_at": iso(s.started_at), "finished_at": iso(s.finished_at), "duration_ms": s.duration_ms} for s in steps],
        "documents": [{"id": str(d.id), "name": d.name, "content_type": d.content_type, "content": d.content} for d in docs],
        "details_purged_at": iso(ex.details_purged_at),
        "can_resume": resumable_state(ex) is not None and not ex.details_purged_at,
        "resumed_from": (ex.state or {}).get("resumed_from"),
        "parent_execution_id": str(ex.parent_execution_id) if ex.parent_execution_id else None,
    }


def _manage(ctx: Ctx, ex: WorkflowExecution) -> None:
    if not ctx.org:  # platform-wide view: bind to the execution's tenant and check the permission there
        ctx.require("executions.manage")
        set_tenant(ctx.db, ex.organization_id)
    else:
        ctx.require("executions.manage")


@router.post("/executions/{execution_id}/cancel", response_model=schemas.ExecutionOut)
def cancel(execution_id: str, ctx: Ctx = Depends(optional_org_ctx("executions.read"))) -> dict:
    ex = _get_execution(ctx, execution_id)
    _manage(ctx, ex)
    cancel_execution(ctx.db, ex)
    audit(ctx.db, "execution.cancelled", ctx=ctx, organization_id=ex.organization_id, resource_type="execution", resource_id=ex.id)
    ctx.db.commit()
    return exec_view(ex)


@router.post("/executions/{execution_id}/retry", response_model=schemas.ExecutionOut, status_code=202)
def retry(execution_id: str, from_failed_step: bool = False, ctx: Ctx = Depends(optional_org_ctx("executions.read"))) -> dict:
    """Run again. With ``from_failed_step=true`` the steps that succeeded are not repeated."""
    ex = _get_execution(ctx, execution_id)
    _manage(ctx, ex)
    new = retry_execution(ctx.db, ex, ctx.user.id, from_failed_step=from_failed_step)
    audit(ctx.db, "execution.retried", ctx=ctx, organization_id=ex.organization_id, resource_type="execution", resource_id=ex.id,
          meta={"new": str(new.id), "from_failed_step": from_failed_step})
    ctx.db.commit()
    return exec_view(new)


class ResumeBody(Body):
    node_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


@router.post("/executions/{execution_id}/resume", response_model=schemas.ResumedOut, status_code=202)
def resume(execution_id: str, body: ResumeBody | None = None, ctx: Ctx = Depends(optional_org_ctx("executions.read"))) -> dict:
    ex = _get_execution(ctx, execution_id)
    _manage(ctx, ex)
    body = body or ResumeBody()
    node = resume_execution(ctx.db, ex, body.data, body.node_id)
    audit(ctx.db, "execution.resumed", ctx=ctx, organization_id=ex.organization_id, resource_type="execution", resource_id=ex.id, meta={"node": node})
    ctx.db.commit()
    return {"ok": True, "node_id": node}


# ----------------------------------------------------------------------------- dead letters (platform)


@router.get("/dead-letters", response_model=schemas.Page[schemas.DeadLetterOut])
def dead_letters(paging: Paging = Depends(), ctx: Ctx = Depends(platform_ctx("platform.analytics.read"))) -> dict:
    q = select(ExecutionQueueJob).where(ExecutionQueueJob.status == "DEAD").order_by(ExecutionQueueJob.finished_at.desc())
    return paginate(ctx.db, q, paging, lambda j: {
        "id": str(j.id), "organization_id": str(j.organization_id), "execution_id": str(j.execution_id) if j.execution_id else None,
        "kind": j.kind, "attempts": j.attempts, "last_error": j.last_error, "finished_at": iso(j.finished_at)})


@router.post("/dead-letters/{job_id}/requeue", response_model=schemas.ExecutionOut, status_code=202)
def requeue_dead_letter(job_id: str, ctx: Ctx = Depends(platform_ctx("executions.manage"))) -> dict:
    """Re-run the work of a dead job as a fresh execution (never re-processes a half-finished one)."""
    job = ctx.db.get(ExecutionQueueJob, parse_uuid(job_id, "Job"))
    if not job or job.status != "DEAD":
        raise NotFound("Dead-letter job not found")
    if not job.execution_id:
        raise Conflict("This job cannot be requeued")
    ex = ctx.db.get(WorkflowExecution, job.execution_id)
    new = retry_execution(ctx.db, ex, ctx.user.id)
    audit(ctx.db, "dead_letter.requeued", ctx=ctx, organization_id=job.organization_id, resource_type="job", resource_id=job.id, meta={"new": str(new.id)})
    ctx.db.commit()
    return exec_view(new)


# ----------------------------------------------------------------------------- approvals


def approval_view(a: ApprovalRequest) -> dict:
    return {"id": str(a.id), "kind": a.kind, "status": a.status, "title": a.title, "summary": a.summary, "payload": a.payload,
            "execution_id": str(a.execution_id) if a.execution_id else None,
            "agent_execution_id": str(a.agent_execution_id) if a.agent_execution_id else None,
            "decided_at": iso(a.decided_at), "decision_note": a.decision_note, "expires_at": iso(a.expires_at), "created_at": iso(a.created_at)}


@router.get("/approvals", response_model=schemas.Page[schemas.ApprovalOut])
def list_approvals(status: str | None = "PENDING", paging: Paging = Depends(), ctx: Ctx = Depends(org_ctx("executions.read"))) -> dict:
    q = select(ApprovalRequest).where(ApprovalRequest.organization_id == ctx.org_id)
    if status:
        q = q.where(ApprovalRequest.status == status)
    return paginate(ctx.db, q.order_by(ApprovalRequest.created_at.desc()), paging, approval_view)


class DecisionBody(Body):
    approved: bool
    note: str = Field("", max_length=2000)


@router.post("/approvals/{approval_id}/decide", response_model=schemas.ApprovalOut)
def decide(approval_id: str, body: DecisionBody, ctx: Ctx = Depends(org_ctx("approvals.decide"))) -> dict:
    req = owned(ctx.db, ApprovalRequest, approval_id, ctx.org_id, "Approval")
    decide_approval(ctx.db, req, approved=body.approved, user_id=ctx.user.id, note=body.note)
    audit(ctx.db, "approval.decided", ctx=ctx, resource_type="approval", resource_id=req.id, meta={"approved": body.approved})
    ctx.db.commit()
    return approval_view(req)
