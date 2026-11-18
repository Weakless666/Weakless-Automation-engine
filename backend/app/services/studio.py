"""Creative Studio: AI-generated images and videos, kept as stored files.

A request becomes a ``media_generations`` row. Images are produced in one provider call, run in the
background right after the request (or inline inside a workflow step). Videos are provider-side jobs:
the request starts the job and the worker polls it until the file can be downloaded. A request that
was interrupted (process restart, provider never answering) is closed as failed by the worker instead
of spinning forever.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ai import media as media_api
from app.config import get_settings
from app.core.errors import AppError, NodeError, NotFound, QuotaExceeded, ValidationFailed
from app.db import new_session, utcnow
from app.models import MediaGeneration, StoredFile
from app.services.usage import check_quota, record_usage

log = logging.getLogger("wae.studio")

MEDIA_PROVIDERS = ("openai", "gemini", "openai_compatible")
IMAGE_STALE_MINUTES = 10
VIDEO_STALE_MINUTES = 30
VIDEO_POLL_SECONDS = 10
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}


def _credentials(db: Session, organization_id: uuid.UUID, provider: str) -> tuple[dict, dict] | None:
    from app.ai.service import resolve_credentials

    try:
        return resolve_credentials(db, organization_id, provider)
    except (NodeError, AppError):
        return None


def meta(db: Session, organization_id: uuid.UUID) -> dict:
    providers = [{"key": p, "name": media_api.PROVIDER_NAMES[p], "available": _credentials(db, organization_id, p) is not None,
                  "image_models": media_api.IMAGE_MODELS[p], "video_models": media_api.VIDEO_MODELS[p]} for p in MEDIA_PROVIDERS]
    return {"providers": providers, "aspects": list(media_api.ASPECTS), "video_seconds": list(media_api.VIDEO_SECONDS)}


def views(db: Session, generations: list[MediaGeneration]) -> list[dict]:
    from app.services.files import ref

    ids = [g.file_id for g in generations if g.file_id]
    files = {f.id: f for f in db.scalars(select(StoredFile).where(StoredFile.id.in_(ids)))} if ids else {}
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return [{"id": str(g.id), "kind": g.kind, "provider": g.provider, "model": g.model, "prompt": g.prompt, "params": g.params or {},
             "status": g.status, "error": g.error, "file": ref(files[g.file_id]) if g.file_id in files else None,
             "created_at": iso(g.created_at), "finished_at": iso(g.finished_at)} for g in generations]


def view(db: Session, g: MediaGeneration) -> dict:
    return views(db, [g])[0]


def get_generation(db: Session, organization_id: uuid.UUID, generation_id) -> MediaGeneration:
    try:
        gid = generation_id if isinstance(generation_id, uuid.UUID) else uuid.UUID(str(generation_id))
    except ValueError:
        raise NotFound("Generation not found")
    g = db.get(MediaGeneration, gid)
    if not g or g.organization_id != organization_id:
        raise NotFound("Generation not found")
    return g


def create_generation(db: Session, organization_id: uuid.UUID, *, kind: str, prompt: str, provider: str | None = None, model: str | None = None,
                      aspect: str = "1:1", seconds: int = 8, user_id: uuid.UUID | None = None, execution_id: uuid.UUID | None = None) -> MediaGeneration:
    prompt = (prompt or "").strip()
    if kind not in ("image", "video"):
        raise ValidationFailed("Kind must be 'image' or 'video'")
    if len(prompt) < 3:
        raise ValidationFailed("Describe what to create")
    if aspect not in media_api.ASPECTS:
        raise ValidationFailed(f"Aspect must be one of: {', '.join(media_api.ASPECTS)}")
    if kind == "video" and seconds not in media_api.VIDEO_SECONDS:
        raise ValidationFailed(f"Video length must be one of: {', '.join(str(s) for s in media_api.VIDEO_SECONDS)} seconds")
    models = media_api.IMAGE_MODELS if kind == "image" else media_api.VIDEO_MODELS
    if not provider:
        # The first provider that has credentials and a known model for this kind.
        provider = next((p for p in MEDIA_PROVIDERS if models[p] and _credentials(db, organization_id, p) is not None), None)
        if not provider:
            raise ValidationFailed(f"No AI provider that makes {kind}s is connected. Connect OpenAI or Google Gemini under Integrations.")
    if provider not in MEDIA_PROVIDERS:
        raise ValidationFailed(f"Provider must be one of: {', '.join(MEDIA_PROVIDERS)}")
    model = (model or "").strip() or (models[provider][0] if models[provider] else "")
    if not model:
        raise ValidationFailed(f"Enter the {kind} model to use on this provider")
    check_quota(db, organization_id, "media_generations_per_month")
    g = MediaGeneration(organization_id=organization_id, kind=kind, provider=provider, model=model[:120], prompt=prompt[:8000],
                        params={"aspect": aspect, **({"seconds": seconds} if kind == "video" else {})}, status="queued",
                        created_by=user_id, execution_id=execution_id)
    db.add(g)
    db.flush()
    return g


def _name(g: MediaGeneration, content_type: str) -> str:
    words = "-".join("".join(ch if ch.isalnum() else " " for ch in g.prompt.lower()).split()[:6])[:60] or g.kind
    return f"{words}.{_EXT.get(content_type, 'bin')}"


def _finish(db: Session, g: MediaGeneration, media: media_api.Media, *, source: str = "studio", expires_at=None) -> None:
    from app.services.files import save_file

    f = save_file(db, g.organization_id, _name(g, media.content_type), media.content, content_type=media.content_type, source=source,
                  user_id=g.created_by, execution_id=g.execution_id, expires_at=expires_at)
    g.file_id, g.status, g.error, g.finished_at = f.id, "completed", "", utcnow()
    record_usage(db, g.organization_id, "media_generations", 1, execution_id=g.execution_id,
                 meta={"kind": g.kind, "provider": g.provider, "model": g.model})
    db.flush()


def _fail(db: Session, g: MediaGeneration, message: str) -> None:
    g.status, g.error, g.finished_at = "failed", message[:1000], utcnow()
    db.flush()


def _message(exc: Exception) -> str:
    if isinstance(exc, (NodeError, AppError)):
        return exc.message
    return f"Unexpected error: {type(exc).__name__}"


def _produce(db: Session, g: MediaGeneration, *, secrets_out: set[str] | None = None, source: str = "studio", expires_at=None) -> None:
    """Run the provider call for a claimed (``running``) generation. Raises on failure."""
    from app.ai.service import resolve_credentials

    secret, config = resolve_credentials(db, g.organization_id, g.provider)
    if secrets_out is not None:
        secrets_out.update(str(v) for v in secret.values() if isinstance(v, str) and len(v) >= 6)
    provider = media_api.get_media_provider(g.provider)
    aspect = (g.params or {}).get("aspect") or "1:1"
    if g.kind == "image":
        _finish(db, g, provider.generate_image(secret, config, model=g.model, prompt=g.prompt, aspect=aspect), source=source, expires_at=expires_at)
    else:
        g.provider_job_id = provider.start_video(secret, config, model=g.model, prompt=g.prompt, aspect=aspect,
                                                 seconds=int((g.params or {}).get("seconds") or 8))[:300]
        db.flush()


def run_generation(generation_id: uuid.UUID) -> None:
    """Background task: make the image, or start the video job. Safe to call twice (only one caller claims the row)."""
    with new_session(bypass_rls=True) as db:
        claimed = db.execute(update(MediaGeneration).where(MediaGeneration.id == generation_id, MediaGeneration.status == "queued")
                             .values(status="running", updated_at=utcnow()).execution_options(synchronize_session=False))
        db.commit()
        if claimed.rowcount != 1:
            return
        g = db.get(MediaGeneration, generation_id)
        try:
            _produce(db, g)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - the request must end in a visible state
            db.rollback()
            if not isinstance(exc, (NodeError, AppError)):
                log.exception("generation %s crashed", generation_id)
            g = db.get(MediaGeneration, generation_id)
            _fail(db, g, _message(exc))
            db.commit()


def generate_image_now(db: Session, organization_id: uuid.UUID, *, prompt: str, provider: str | None, model: str | None, aspect: str,
                       execution_id: uuid.UUID | None, secrets_out: set[str] | None = None) -> dict:
    """Inline image generation for a workflow step. The file follows the workflow file retention."""
    from app.services.files import ref, workflow_file_expiry

    try:
        g = create_generation(db, organization_id, kind="image", prompt=prompt, provider=provider, model=model, aspect=aspect, execution_id=execution_id)
    except QuotaExceeded as exc:
        raise NodeError(exc.message, code="quota_exceeded") from exc
    except AppError as exc:
        raise NodeError(exc.message, code="invalid_config") from exc
    g.status = "running"
    try:
        _produce(db, g, secrets_out=secrets_out, source="workflow", expires_at=workflow_file_expiry(db))
    except AppError as exc:
        raise NodeError(exc.message, code=exc.code) from exc
    return {"file": ref(db.get(StoredFile, g.file_id)), "provider": g.provider, "model": g.model, "generation_id": str(g.id)}


def poll_generations(db: Session, limit: int = 5) -> int:
    """Worker maintenance: collect finished videos and close requests that were interrupted."""
    now = utcnow()
    finished = 0
    running = db.scalars(select(MediaGeneration).where(
        MediaGeneration.status == "running", MediaGeneration.kind == "video", MediaGeneration.provider_job_id.is_not(None),
        MediaGeneration.updated_at <= now - timedelta(seconds=VIDEO_POLL_SECONDS)).order_by(MediaGeneration.updated_at).limit(limit)).all()
    for g in running:
        # Claim this poll so two workers do not download the same video.
        claimed = db.execute(update(MediaGeneration).where(MediaGeneration.id == g.id, MediaGeneration.updated_at == g.updated_at)
                             .values(updated_at=utcnow()).execution_options(synchronize_session=False))
        db.commit()
        if claimed.rowcount != 1:
            continue
        db.refresh(g)
        try:
            from app.ai.service import resolve_credentials

            secret, config = resolve_credentials(db, g.organization_id, g.provider)
            status = media_api.get_media_provider(g.provider).poll_video(secret, config, g.provider_job_id,
                                                                         max_bytes=get_settings().file_max_mb * 1024 * 1024)
            if status.state == "completed" and status.media:
                _finish(db, g, status.media)
                finished += 1
            elif status.state == "failed":
                _fail(db, g, status.error or "The provider could not make this video")
            elif g.created_at <= now - timedelta(minutes=VIDEO_STALE_MINUTES):
                _fail(db, g, f"The provider did not finish the video within {VIDEO_STALE_MINUTES} minutes")
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            retryable = isinstance(exc, NodeError) and exc.retryable
            if not isinstance(exc, (NodeError, AppError)):
                log.exception("polling generation %s crashed", g.id)
            g = db.get(MediaGeneration, g.id)
            if not retryable or g.created_at <= now - timedelta(minutes=VIDEO_STALE_MINUTES):
                _fail(db, g, _message(exc))
                db.commit()
    # Requests whose process died before they reached a final state.
    stale = db.scalars(select(MediaGeneration).where(
        MediaGeneration.status.in_(("queued", "running")), MediaGeneration.provider_job_id.is_(None),
        MediaGeneration.updated_at <= now - timedelta(minutes=IMAGE_STALE_MINUTES)).limit(50)).all()
    for g in stale:
        _fail(db, g, "The request was interrupted before it finished. Try again.")
    if stale:
        db.commit()
    return finished
