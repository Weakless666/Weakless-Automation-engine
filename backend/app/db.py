"""Database engine, session factory and tenant-scoped session helpers."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import JSON, DateTime, MetaData, create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return eng
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
        event.listen(_SessionLocal, "after_begin", _apply_tenant_guc)
    return _engine


def reset_engine() -> None:
    """Dispose the engine (used by tests when the database URL changes)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine, _SessionLocal = None, None


def _apply_tenant_guc(session: Session, transaction, connection) -> None:
    """On PostgreSQL, publish the tenant context to Row Level Security policies.

    ``set_config(..., true)`` is transaction-local, so this runs at the start of
    every transaction of a tenant-scoped session.
    """
    if connection.dialect.name != "postgresql":
        return
    org_id = session.info.get("org_id")
    bypass = session.info.get("rls_bypass", False)
    connection.execute(
        text("SELECT set_config('app.current_org_id', :org, true), set_config('app.rls_bypass', :bp, true)"),
        {"org": str(org_id) if org_id else "", "bp": "on" if bypass else "off"},
    )


def new_session(org_id: uuid.UUID | None = None, bypass_rls: bool = False) -> Session:
    get_engine()
    assert _SessionLocal is not None
    s = _SessionLocal()
    s.info["org_id"] = org_id
    s.info["rls_bypass"] = bypass_rls
    return s


def set_tenant(session: Session, org_id: uuid.UUID | None, bypass_rls: bool = False) -> None:
    """Change the tenant context of an open session (applies immediately on PostgreSQL)."""
    session.info["org_id"] = org_id
    session.info["rls_bypass"] = bypass_rls
    if session.in_transaction():
        _apply_tenant_guc(session, None, session.connection())


@contextmanager
def session_scope(org_id: uuid.UUID | None = None, bypass_rls: bool = False) -> Iterator[Session]:
    s = new_session(org_id, bypass_rls)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Starts with RLS bypass *off* and no tenant: the auth
    dependency sets the tenant context after validating membership."""
    s = new_session()
    try:
        yield s
    finally:
        s.close()
