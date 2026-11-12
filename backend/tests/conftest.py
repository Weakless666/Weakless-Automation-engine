"""Test fixtures: an isolated SQLite database per test, seeded RBAC/catalog,
a queue worker that is drained synchronously, and a mock HTTP transport."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

os.environ["WAE_ENV"] = "test"
os.environ["WAE_EMBEDDED_WORKER"] = "false"
os.environ["WAE_RATE_LIMIT_PER_MINUTE"] = "100000"
os.environ["WAE_AUTH_RATE_LIMIT_PER_MINUTE"] = "100000"
# Tests must not depend on a developer's local backend/.env (environment variables win over it).
os.environ["WAE_BOOTSTRAP_ADMIN_EMAIL"] = ""
os.environ["WAE_BOOTSTRAP_ADMIN_PASSWORD"] = ""
os.environ["WAE_PUBLIC_BASE_URL"] = "http://localhost:8000"
os.environ["WAE_FRONTEND_URL"] = "http://localhost:3000"
os.environ["WAE_STAFF_MFA_REQUIRED"] = "false"  # tests that cover the requirement switch it on explicitly
for _name in ("WAE_SECRET_KEY", "WAE_MASTER_KEYS", "WAE_ACTIVE_KEY_ID", "WAE_STRIPE_SECRET_KEY", "WAE_STRIPE_WEBHOOK_SECRET",
              "WAE_ANTHROPIC_API_KEY", "WAE_OPENAI_API_KEY", "WAE_GEMINI_API_KEY", "WAE_SMTP_HOST", "WAE_SMTP_FROM", "WAE_SENTRY_DSN"):
    os.environ[_name] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbmod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import http as http_core  # noqa: E402
from app.core.ratelimit import limiter  # noqa: E402

ADMIN_PASSWORD = "Admin-Passw0rd-Test"

# Set WAE_TEST_DATABASE_URL to a PostgreSQL database owned by a role that is neither superuser nor
# BYPASSRLS to run the suite against PostgreSQL: the schema then comes from the real migrations
# (including Row Level Security, which applies to the owner because it is FORCEd), and every test
# starts from emptied tables. Without it each test gets a fresh SQLite file.
PG_URL = os.environ.get("WAE_TEST_DATABASE_URL", "").strip()


@pytest.fixture(scope="session")
def _postgres_schema():
    if not PG_URL:
        yield
        return
    from alembic import command
    from alembic.config import Config

    os.environ["WAE_DATABASE_URL"] = PG_URL
    get_settings.cache_clear()
    dbmod.reset_engine()
    with dbmod.get_engine().connect() as conn:
        role = conn.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")).scalar()
    assert not role, "WAE_TEST_DATABASE_URL must use a role without SUPERUSER/BYPASSRLS, or Row Level Security is not tested"
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    dbmod.reset_engine()
    yield


def _empty_postgres(engine) -> None:
    tables = ", ".join(f'"{t.name}"' for t in dbmod.Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch, _postgres_schema):
    url = PG_URL or f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("WAE_DATABASE_URL", url)
    get_settings.cache_clear()
    dbmod.reset_engine()
    from app import models  # noqa: F401
    from app.seed import seed_all

    engine = dbmod.get_engine()
    if PG_URL:
        _empty_postgres(engine)
    else:
        dbmod.Base.metadata.create_all(engine)
    with dbmod.session_scope(bypass_rls=True) as s:
        seed_all(s)
    limiter.reset()
    from app import rbac

    rbac.invalidate_cache()
    yield
    dbmod.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def db():
    s = dbmod.new_session(bypass_rls=True)
    yield s
    s.close()


class HttpMock:
    """Routes outbound requests to handlers; records every request."""

    def __init__(self):
        self.routes: list[tuple[str, str, object]] = []
        self.requests: list[httpx.Request] = []

    def add(self, method: str, url_prefix: str, response):
        self.routes.append((method.upper(), url_prefix, response))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, prefix, response in self.routes:
            if request.method == method and str(request.url).startswith(prefix):
                if callable(response):
                    return response(request)
                status, body = response
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "no mock route"})


@pytest.fixture
def http_mock(monkeypatch):
    mock = HttpMock()
    monkeypatch.setattr(http_core, "TRANSPORT", httpx.MockTransport(mock.handler))
    monkeypatch.setattr(http_core, "resolve_host", lambda host: ["93.184.216.34"])
    return mock


@pytest.fixture
def worker():
    from app.engine.queue import Worker

    return Worker("test-worker")


@pytest.fixture
def org(db):
    from app.services.organizations import create_organization

    o = create_organization(db, name="Acme Hotel", industry="hospitality", mode="saas")
    db.commit()
    return o


@pytest.fixture
def other_org(db):
    from app.services.organizations import create_organization

    o = create_organization(db, name="Rival Salon", industry="beauty", mode="saas")
    db.commit()
    return o


def make_user(db, email: str, *, platform_role: str | None = None, org=None, role: str | None = None, password: str = ADMIN_PASSWORD):
    from app.core.security import hash_password
    from app.models import OrganizationMember, User

    u = User(email=email, password_hash=hash_password(password), full_name=email.split("@")[0], platform_role=platform_role)
    db.add(u)
    db.flush()
    if org is not None and role:
        db.add(OrganizationMember(organization_id=org.id, user_id=u.id, role=role))
    db.commit()
    return u


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def login(client, email: str, password: str = ADMIN_PASSWORD) -> dict:
    """Log in and return headers for a token-authenticated API client."""
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    client.cookies.clear()  # use the bearer token so tests exercise the non-cookie path by default
    return {"Authorization": f"Bearer {r.json()['token']}"}


def wf_def(nodes: list[dict], edges: list[tuple]) -> dict:
    return {
        "nodes": [{"id": n["id"], "type": n["type"], "name": n.get("name", n["id"]), "config": n.get("config", {}),
                   "settings": n.get("settings", {}), "position": {"x": 0, "y": 0}} for n in nodes],
        "edges": [{"source": e[0], "target": e[1], "source_handle": e[2] if len(e) > 2 else "out"} for e in edges],
    }


def make_workflow(db, org, definition: dict, *, name: str = "Test workflow", variables: dict | None = None, publish: bool = True, active: bool = True):
    from app.services import workflows as wfs

    wf = wfs.create_workflow(db, org.id, name, definition=definition, variables=variables)
    if publish:
        wfs.publish(db, wf, None)
        if active:
            wfs.set_active(db, wf, True)
    db.commit()
    return wf


def run(db, worker, wf, payload: dict | None = None, **kw):
    from app.services.executions import start_execution

    ex = start_execution(db, wf, trigger_type=kw.pop("trigger_type", "manual"), payload=payload or {}, **kw)
    db.commit()
    worker.drain()
    db.expire_all()
    return db.get(type(ex), ex.id)


def steps(db, ex) -> list:
    from sqlalchemy import select

    from app.models import WorkflowExecutionStep

    return list(db.scalars(select(WorkflowExecutionStep).where(WorkflowExecutionStep.execution_id == ex.id).order_by(WorkflowExecutionStep.sequence)))


def new_id() -> str:
    return uuid.uuid4().hex[:8]
