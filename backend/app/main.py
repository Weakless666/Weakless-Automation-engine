"""FastAPI application factory."""
from __future__ import annotations

import logging
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import get_settings
from app.core.errors import AppError
from app.core.ratelimit import limiter
from app.core.redact import RedactingFilter
from app.db import new_session, session_scope

log = logging.getLogger("wae")


def configure_logging() -> None:
    s = get_settings()
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'))
        handler.addFilter(RedactingFilter())  # secrets never reach the log stream
        root.addHandler(handler)
    root.setLevel(s.log_level.upper())
    if s.sentry_dsn:
        try:
            import sentry_sdk

            sentry_sdk.init(dsn=s.sentry_dsn, environment=s.env, send_default_pii=False, traces_sample_rate=0.0)
        except ImportError:  # pragma: no cover
            log.warning("WAE_SENTRY_DSN is set but sentry-sdk is not installed")


def _error(status: int, code: str, message: str, request: Request, details=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    worker = None
    if s.env != "test":
        from app.seed import seed_all

        try:
            with session_scope(bypass_rls=True) as db:
                seed_all(db)
        except Exception:  # noqa: BLE001
            log.exception("Seeding failed - is the database migrated? Run: alembic upgrade head")
    if s.embedded_worker and s.env != "test":
        from app.engine.queue import Worker

        worker = Worker()
        threading.Thread(target=worker.run_forever, name="wae-worker", daemon=True).start()
    yield
    if worker:
        worker.stop()


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging()
    app = FastAPI(
        title="Weakless Automation Engine API", version=__version__, lifespan=lifespan,
        description="Multi-tenant automation infrastructure: workflows, AI agents, integrations, triggers, templates, billing.\n\n"
                    "Authenticate with `POST /api/v1/auth/login`, then send `Authorization: Bearer <token>` and, for tenant-scoped "
                    "endpoints, `X-Organization-Id`. Errors use `{error: {code, message, details, request_id}}`. "
                    "List endpoints accept `limit`/`offset` and return `{items, total, limit, offset}`.",
        docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None,
    )
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list(), allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-Organization-Id", "X-WAE-CSRF", "Idempotency-Key"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        path = request.url.path
        # Inbound hooks have their own per-endpoint limits; everything else shares a per-client budget.
        if path.startswith("/api/") and not path.startswith("/api/v1/hooks/"):
            key = f"api:{request.client.host if request.client else 'unknown'}"
            if not limiter.allow(key, s.rate_limit_per_minute):
                return _error(429, "rate_limited", "Too many requests", request)
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - never leak internals to clients
            log.exception("Unhandled error on %s %s [%s]", request.method, path, request.state.request_id)
            response = _error(500, "internal_error", "An unexpected error occurred", request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if not path.startswith("/api/docs"):
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if s.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return _error(exc.status_code, exc.code, exc.message, request, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]} for e in exc.errors()]
        return _error(422, "validation_error", "Request validation failed", request, details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, str(exc.detail), request)

    from app.api import agents, auth, catalog, events, insights, integrations, orgs, workflows

    for r in (auth.router, orgs.router, workflows.router, agents.router, integrations.router, events.router, events.public,
              catalog.router, insights.router, insights.public):
        app.include_router(r, prefix="/api/v1")

    @app.get("/api/v1/health", tags=["system"])
    def health() -> dict:
        """Liveness probe."""
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/health/ready", tags=["system"])
    def ready():
        """Readiness probe: verifies database connectivity."""
        try:
            with new_session() as db:
                db.execute(text("SELECT 1"))
            return {"status": "ready"}
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "unavailable"}, status_code=503)

    return app


app = create_app()
