"""Creative Studio: generate images and videos with the organization's AI providers."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import Field
from sqlalchemy import select

from app.api import schemas
from app.api.common import ApiRoute, Body, Paging, paginate
from app.core.errors import Conflict
from app.deps import Ctx, org_ctx
from app.models import MediaGeneration
from app.services import studio as svc
from app.services.audit import audit

router = APIRouter(route_class=ApiRoute, tags=["studio"])


@router.get("/studio/meta", response_model=schemas.StudioMetaOut)
def studio_meta(ctx: Ctx = Depends(org_ctx("content.read"))) -> dict:
    """Providers that can make media, whether each has credentials, and suggested models."""
    return svc.meta(ctx.db, ctx.org_id)


@router.get("/studio/generations", response_model=schemas.MediaGenerationPage)
def list_generations(paging: Paging = Depends(), kind: str | None = None, ctx: Ctx = Depends(org_ctx("content.read"))) -> dict:
    query = select(MediaGeneration).where(MediaGeneration.organization_id == ctx.org_id)
    if kind:
        query = query.where(MediaGeneration.kind == kind)
    page = paginate(ctx.db, query.order_by(MediaGeneration.created_at.desc()), paging, lambda g: g)
    page["items"] = svc.views(ctx.db, page["items"])
    return page


class GenerationBody(Body):
    kind: Literal["image", "video"] = "image"
    prompt: str = Field(..., min_length=3, max_length=8000)
    provider: Literal["openai", "gemini", "openai_compatible"] | None = Field(None, description="Empty = the first connected provider that can make this")
    model: str | None = Field(None, max_length=120)
    aspect: Literal["1:1", "4:5", "16:9", "9:16"] = "1:1"
    seconds: Literal[4, 8, 12] = 8


@router.post("/studio/generations", response_model=schemas.MediaGenerationOut, status_code=202)
def create_generation(body: GenerationBody, tasks: BackgroundTasks, ctx: Ctx = Depends(org_ctx("content.write"))) -> dict:
    """Start a generation. The response is the queued request; read it again (or list) to follow it.
    Images usually take under a minute, videos a few minutes."""
    g = svc.create_generation(ctx.db, ctx.org_id, kind=body.kind, prompt=body.prompt, provider=body.provider, model=body.model,
                              aspect=body.aspect, seconds=body.seconds, user_id=ctx.user.id)
    audit(ctx.db, "studio.generation_started", ctx=ctx, resource_type="media_generation", resource_id=g.id,
          meta={"kind": g.kind, "provider": g.provider, "model": g.model})
    ctx.db.commit()
    out = svc.view(ctx.db, g)
    tasks.add_task(svc.run_generation, g.id)
    return out


@router.get("/studio/generations/{generation_id}", response_model=schemas.MediaGenerationOut)
def get_generation(generation_id: str, ctx: Ctx = Depends(org_ctx("content.read"))) -> dict:
    return svc.view(ctx.db, svc.get_generation(ctx.db, ctx.org_id, generation_id))


@router.delete("/studio/generations/{generation_id}", response_model=schemas.Ok)
def delete_generation(generation_id: str, delete_file: bool = True, ctx: Ctx = Depends(org_ctx("content.write"))) -> dict:
    """Remove a generation and, unless ``delete_file=false``, the file it produced."""
    from app.models import ContentItem, StoredFile

    g = svc.get_generation(ctx.db, ctx.org_id, generation_id)
    if g.file_id and delete_file:
        used = [i.title for i in ctx.db.scalars(select(ContentItem).where(ContentItem.organization_id == ctx.org_id,
                                                                          ContentItem.status.in_(("idea", "draft", "awaiting_approval", "approved"))))
                if any(str(m.get("file_id")) == str(g.file_id) for m in (i.media or []))]
        if used:
            raise Conflict(f"This file is attached to a planned post ({used[0]}). Remove it from the post first.")
        f = ctx.db.get(StoredFile, g.file_id)
        if f is not None:
            ctx.db.delete(f)
    ctx.db.delete(g)
    audit(ctx.db, "studio.generation_deleted", ctx=ctx, resource_type="media_generation", resource_id=generation_id)
    ctx.db.commit()
    return {"ok": True}
