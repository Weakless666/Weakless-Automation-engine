"""Hot paths stay cheap as organizations grow: event triggers are an indexed lookup and quota
checks read running counters, not a month of usage records."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import event, func, select

from app import db as dbmod
from app.models import AppEventSubscription, UsageCounter, UsageRecord, WorkflowExecution
from app.services import workflows as wfs
from app.services.events import emit_app_event
from app.services.usage import record_usage, usage_totals
from tests.conftest import make_workflow, run, wf_def

TRIG = {"id": "t", "type": "trigger.manual"}


def _listener(pattern: str, name: str, db, org, *, active: bool = True):
    return make_workflow(db, org, wf_def([{"id": "ev", "type": "trigger.app_event", "config": {"event_type": pattern}},
                                          {"id": "x", "type": "logic.transform", "config": {"fields": {"got": "{{ trigger.event_type }}"}}}],
                                         [("ev", "x")]), name=name, active=active)


class QueryCounter:
    def __init__(self, engine):
        self.engine, self.count = engine, 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._hit)

    def _hit(self, *args):
        self.count += 1


def test_event_subscriptions_follow_publish_activation_and_edits(db, org, worker):
    exact = _listener("crm.deal_won", "Exact", db, org)
    wildcard = _listener("crm.*", "Wildcard", db, org)
    inactive = _listener("crm.deal_won", "Inactive", db, org, active=False)
    subs = {s.workflow_id: s for s in db.scalars(select(AppEventSubscription))}
    assert subs[exact.id].exact_type == "crm.deal_won" and subs[wildcard.id].exact_type is None and inactive.id in subs

    ev = emit_app_event(db, org.id, "crm.deal_won", {"deal": 1})
    db.commit()
    worker.drain()
    started = {e.workflow_id for e in db.scalars(select(WorkflowExecution).where(WorkflowExecution.trigger_type == "app_event"))}
    assert ev.status == "processed" and started == {exact.id, wildcard.id}  # inactive workflows do not start

    # Changing the trigger's pattern takes effect when the new version is published, not while it is a draft.
    definition = wfs.draft_definition(db, exact)
    definition["nodes"][0]["config"]["event_type"] = "billing.paid"
    wfs.save_draft(db, exact, definition)
    db.commit()
    assert db.scalar(select(AppEventSubscription.pattern).where(AppEventSubscription.workflow_id == exact.id)) == "crm.deal_won"
    wfs.publish(db, exact, None)
    db.commit()
    assert db.scalar(select(AppEventSubscription.pattern).where(AppEventSubscription.workflow_id == exact.id)) == "billing.paid"

    # Removing the trigger removes the subscription; deactivating stops starts immediately.
    wfs.save_draft(db, wildcard, wf_def([TRIG], []))
    wfs.publish(db, wildcard, None)
    wfs.set_active(db, exact, False)
    db.commit()
    assert db.scalar(select(func.count()).select_from(AppEventSubscription).where(AppEventSubscription.workflow_id == wildcard.id)) == 0
    assert emit_app_event(db, org.id, "billing.paid", {}).status == "ignored"


def test_emitting_an_event_does_not_read_every_workflow(db, org, worker):
    for i in range(25):
        make_workflow(db, org, wf_def([TRIG], []), name=f"Unrelated {i}")
    _listener("orders.created", "Listener", db, org)
    engine = dbmod.get_engine()
    with QueryCounter(engine) as few:
        emit_app_event(db, org.id, "nobody.listens", {})
        db.flush()
    for i in range(25):
        make_workflow(db, org, wf_def([TRIG], []), name=f"More {i}")
    with QueryCounter(engine) as same:
        emit_app_event(db, org.id, "nobody.listens", {})
        db.flush()
    assert same.count == few.count <= 4  # independent of how many workflows the organization has


def test_usage_counters_match_records_and_keep_quota_checks_constant(db, org, worker):
    wf = make_workflow(db, org, wf_def([TRIG], []))
    for _ in range(3):
        assert run(db, worker, wf).status == "COMPLETED"
    record_usage(db, org.id, "ai_input_tokens", 120)
    record_usage(db, org.id, "ai_input_tokens", Decimal("0.5"))
    record_usage(db, org.id, "ai_cost_usd", Decimal("0.000123"))
    db.commit()
    raw = dict(db.execute(select(UsageRecord.metric, func.sum(UsageRecord.quantity)).group_by(UsageRecord.metric)).all())
    totals = usage_totals(db, org.id)
    assert totals["executions"] == 3 and totals["ai_input_tokens"] == 120.5 and abs(totals["ai_cost_usd"] - 0.000123) < 1e-9
    assert {m: float(v) for m, v in raw.items()} == {m: v for m, v in totals.items() if v}
    assert db.scalar(select(func.count()).select_from(UsageCounter)) == 3  # one row per metric, however many records

    with QueryCounter(dbmod.get_engine()) as q:
        usage_totals(db, org.id)
    assert q.count == 1


def test_migration_backfills_subscriptions_and_counters(tmp_path, monkeypatch, worker):
    """Upgrading an existing database (0005 -> 0006) fills both new tables from existing data."""
    from alembic import command
    from alembic.config import Config

    from app.config import get_settings
    from app.services.organizations import create_organization
    from app.seed import seed_all

    monkeypatch.setenv("WAE_DATABASE_URL", f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}")
    get_settings.cache_clear()
    dbmod.reset_engine()
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    command.upgrade(cfg, "head")
    with dbmod.session_scope(bypass_rls=True) as s:
        seed_all(s)
        o = create_organization(s, name="Migrated Co", industry="retail", mode="saas")
        s.flush()
        listener = _listener("orders.*", "Migrated listener", s, o)
        wf = make_workflow(s, o, wf_def([TRIG], []))
        listener_id = listener.id
        for _ in range(2):
            run(s, worker, wf)
    command.downgrade(cfg, "0005")  # drops the derived tables ...
    command.upgrade(cfg, "head")  # ... which the upgrade rebuilds from usage records and published versions
    with dbmod.session_scope(bypass_rls=True) as s:
        sub = s.scalar(select(AppEventSubscription))
        assert sub.workflow_id == listener_id and sub.pattern == "orders.*" and sub.exact_type is None
        org_id = s.scalar(select(UsageCounter.organization_id))
        assert usage_totals(s, org_id)["executions"] == 2
