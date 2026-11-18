"""Stored files: upload, list, download and delete."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from app.api import schemas
from app.api.common import ApiRoute, Paging, paginate
from app.config import get_settings
from app.core.errors import ValidationFailed
from app.deps import Ctx, org_ctx
from app.models import StoredFile
from app.services import files as svc
from app.services.audit import audit

router = APIRouter(route_class=ApiRoute, tags=["files"])


@router.get("/files", response_model=schemas.FilePage)
def list_files(paging: Paging = Depends(), source: str | None = None, q: str | None = None, ctx: Ctx = Depends(org_ctx("files.read"))) -> dict:
    query = select(StoredFile).where(StoredFile.organization_id == ctx.org_id)
    if source:
        query = query.where(StoredFile.source == source)
    if q:
        query = query.where(StoredFile.name.ilike(f"%{q.replace('%', '').replace('_', '')}%"))
    page = paginate(ctx.db, query.order_by(StoredFile.created_at.desc()), paging, svc.view)
    s = get_settings()
    page["storage"] = {"used_bytes": svc.storage_used(ctx.db, ctx.org_id), "limit_bytes": s.org_storage_mb * 1024 * 1024,
                       "max_file_bytes": s.file_max_mb * 1024 * 1024}
    return page


@router.post("/files", response_model=schemas.FileOut, status_code=201)
def upload_file(file: UploadFile = File(...), ctx: Ctx = Depends(org_ctx("files.write"))) -> dict:
    limit = get_settings().file_max_mb * 1024 * 1024
    content = file.file.read(limit + 1)  # never read more than the limit allows
    if len(content) > limit:
        raise ValidationFailed(f"Files can be at most {get_settings().file_max_mb} MB")
    f = svc.save_file(ctx.db, ctx.org_id, file.filename or "file", content, content_type=file.content_type or "", source="upload", user_id=ctx.user.id)
    audit(ctx.db, "file.uploaded", ctx=ctx, resource_type="file", resource_id=f.id, meta={"name": f.name, "size": f.size})
    ctx.db.commit()
    return svc.view(f)


@router.get("/files/{file_id}", response_model=schemas.FileOut)
def get_file(file_id: str, ctx: Ctx = Depends(org_ctx("files.read"))) -> dict:
    return svc.view(svc.get_file(ctx.db, ctx.org_id, file_id))


@router.get("/files/{file_id}/content", response_class=Response,
            responses={200: {"content": {"application/octet-stream": {}}, "description": "The file, always served as a download"}})
def download_file(file_id: str, ctx: Ctx = Depends(org_ctx("files.read"))) -> Response:
    f = svc.get_file(ctx.db, ctx.org_id, file_id, with_data=True)
    # Always an attachment with a neutral type: an uploaded HTML or SVG file must never render on this origin.
    return Response(content=f.data, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(f.name)}", "X-Content-Type-Options": "nosniff",
        "X-File-Content-Type": f.content_type})


@router.delete("/files/{file_id}", response_model=schemas.Ok)
def delete_file(file_id: str, ctx: Ctx = Depends(org_ctx("files.write"))) -> dict:
    f = svc.get_file(ctx.db, ctx.org_id, file_id)
    audit(ctx.db, "file.deleted", ctx=ctx, resource_type="file", resource_id=f.id, meta={"name": f.name})
    ctx.db.delete(f)
    ctx.db.commit()
    return {"ok": True}
