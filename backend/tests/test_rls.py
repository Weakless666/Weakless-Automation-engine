"""PostgreSQL Row Level Security, checked in the database itself (independently of the API's own
tenant checks). Runs only against PostgreSQL (WAE_TEST_DATABASE_URL); SQLite has no RLS."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import db as dbmod
from app.models import TENANT_TABLES
from app.services.events import emit_app_event
from app.services.usage import record_usage
from tests.conftest import PG_URL, make_workflow, run, wf_def

pytestmark = pytest.mark.skipif(not PG_URL, reason="Row Level Security exists only on PostgreSQL (set WAE_TEST_DATABASE_URL)")


def _tenant_data(db, org, worker) -> None:
    """A little of everything a tenant owns: workflows, versions, nodes, executions, steps, queue jobs,
    schedules, endpoints, events, subscriptions, usage records and counters, approvals..."""
    flow = make_workflow(db, org, wf_def([{"id": "t", "type": "trigger.manual"},
                                          {"id": "x", "type": "logic.transform", "config": {"fields": {"a": 1}}}], [("t", "x")]))
    make_workflow(db, org, wf_def([{"id": "h", "type": "trigger.webhook", "config": {}},
                                   {"id": "s", "type": "trigger.schedule", "config": {"cron": "0 9 * * *"}}], []))
    make_workflow(db, org, wf_def([{"id": "ev", "type": "trigger.app_event", "config": {"event_type": "rls.check"}}], []))
    run(db, worker, flow)
    emit_app_event(db, org.id, "rls.check", {"n": 1})
    record_usage(db, org.id, "ai_input_tokens", 10)
    db.commit()
    worker.drain()


def _count(session, table: str, org_id) -> int:
    return session.execute(text(f'SELECT count(*) FROM "{table}" WHERE organization_id = :o'), {"o": org_id}).scalar()


def test_a_tenant_session_sees_and_counts_only_its_own_rows(db, org, other_org, worker):
    _tenant_data(db, org, worker)
    _tenant_data(db, other_org, worker)
    admin = dbmod.new_session(bypass_rls=True)
    populated = [t for t in TENANT_TABLES if _count(admin, t, other_org.id)]
    assert len(populated) >= 12, populated  # the check below covers a real spread of tables
    with dbmod.new_session(org_id=org.id) as mine:
        leaks = [t for t in TENANT_TABLES if _count(mine, t, other_org.id)]
        assert leaks == []
        for t in populated:  # an unfiltered read returns exactly the tenant's own rows
            total = mine.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
            assert total == _count(admin, t, org.id), t
    with dbmod.new_session() as nobody:  # no tenant and no bypass: nothing at all
        assert [t for t in TENANT_TABLES if nobody.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()] == []
    admin.close()


def test_a_tenant_session_cannot_write_into_another_tenant(db, org, other_org, worker):
    _tenant_data(db, other_org, worker)
    with dbmod.new_session(org_id=org.id) as mine:
        with pytest.raises(DBAPIError, match="row-level security"):
            mine.execute(text("INSERT INTO workspaces (id, organization_id, name, slug, is_default, created_at, updated_at) "
                              "VALUES (:id, :o, 'x', :slug, false, now(), now())"), {"id": uuid.uuid4(), "o": other_org.id, "slug": uuid.uuid4().hex})
        mine.rollback()
        # Updates and deletes simply do not reach the other tenant's rows.
        assert mine.execute(text("UPDATE workflows SET name = 'hijacked' WHERE organization_id = :o"), {"o": other_org.id}).rowcount == 0
        assert mine.execute(text("DELETE FROM usage_counters WHERE organization_id = :o"), {"o": other_org.id}).rowcount == 0
        mine.rollback()
    with dbmod.new_session(bypass_rls=True) as admin:
        assert _count(admin, "usage_counters", other_org.id) >= 1


def test_the_owner_role_is_subject_to_the_policies(db):
    """The tests (and production) connect as the tables' owner; FORCE makes RLS apply to it."""
    flags = db.execute(text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:t)"),
                       {"t": list(TENANT_TABLES)}).all()
    assert {r[0] for r in flags} == set(TENANT_TABLES)
    assert all(r[1] and r[2] for r in flags), [r[0] for r in flags if not (r[1] and r[2])]
