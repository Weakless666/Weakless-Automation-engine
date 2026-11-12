"""Mailbox Trigger polling (run by every worker in its maintenance loop).

Each due watch is claimed with a compare-and-swap on ``next_poll_at`` (as schedules are), so two
workers never poll the same mailbox at once. For every new message, in UID order:

1. the trigger's filters are applied;
2. an execution is started with idempotency key ``mailbox:<watch>:<uidvalidity>:<uid>``, so a
   crash between starting it and saving the read position cannot start the same email twice;
3. the event is recorded in the event log, a matching message is optionally marked as read and
   ``last_uid`` advances (committed per message).

The first poll after each activation, and any poll that finds a new UIDVALIDITY, only records the
newest UID: mail that arrived before activation (or while the workflow was off) is never replayed. Connection or login failures back off
exponentially (up to an hour) and notify the organization after three in a row.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError, NodeError
from app.core.redact import redact
from app.db import set_tenant, utcnow
from app.models import MailboxWatch, WebhookEvent, Workflow

log = logging.getLogger("wae.mailbox")
PER_TICK = 5  # watches per maintenance pass: one slow mailbox never stalls the worker for long
PER_POLL = 25  # messages per poll; the rest follow on the next poll
NOTIFY_AFTER = 3


def _matches(filters: dict, message: dict) -> bool:
    sender = f"{message['from'].get('name', '')} {message['from'].get('email', '')}".lower()
    want_from = (filters.get("from_contains") or "").strip().lower()
    want_subject = (filters.get("subject_contains") or "").strip().lower()
    return (not want_from or want_from in sender) and (not want_subject or want_subject in message["subject"].lower())


def _claim(db: Session, watch: MailboxWatch, now) -> bool:
    res = db.execute(update(MailboxWatch).where(MailboxWatch.id == watch.id, MailboxWatch.next_poll_at == watch.next_poll_at)
                     .values(next_poll_at=now + timedelta(minutes=watch.poll_minutes), last_polled_at=now)
                     .execution_options(synchronize_session=False))
    db.commit()
    return res.rowcount == 1


def poll_watch(db: Session, watch: MailboxWatch) -> int:
    """Poll one mailbox; returns the number of executions started. Raises NodeError on mailbox problems."""
    from app.integrations import imap
    from app.integrations.service import connection, get_integration
    from app.services.executions import start_execution

    set_tenant(db, watch.organization_id)
    wf = db.get(Workflow, watch.workflow_id)
    if not wf or wf.deleted_at or not wf.is_active:
        return 0
    integration = get_integration(db, watch.organization_id, watch.integration_id)  # explicit tenant check
    client = imap.connect(connection(db, integration))
    started = 0
    try:
        state = imap.folder_state(client, watch.folder, watch.last_uid, writable=watch.mark_seen)
        if watch.uidvalidity != state.uidvalidity or watch.last_uid is None:
            watch.uidvalidity, watch.last_uid = state.uidvalidity, state.highest  # start from now, never replay a folder
            db.commit()
            return 0
        for uid in state.uids[:PER_POLL]:
            message = imap.parse(imap.fetch(client, uid))
            if _matches(watch.filters or {}, message):
                payload = {**message, "folder": watch.folder, "uid": uid, "mailbox": integration.name}
                ex = start_execution(db, wf, trigger_type="mailbox", trigger_node=watch.node_key, payload=payload,
                                     idempotency_key=f"mailbox:{watch.id}:{state.uidvalidity}:{uid}")
                db.add(WebhookEvent(organization_id=watch.organization_id, source="mailbox", event_type="email.received",
                                    dedupe_key=None, status="processed", execution_id=ex.id,
                                    payload=redact({"subject": message["subject"], "from": message["from"], "message_id": message["message_id"],
                                                    "uid": uid, "folder": watch.folder})))
                started += 1
                if watch.mark_seen:  # only mail the workflow handled; everything else stays as the person left it
                    imap.mark_seen(client, uid)
            watch.last_uid = uid
            db.commit()
    finally:
        imap._close(client)
    return started


def poll_due_mailboxes(db: Session) -> int:
    """Poll every due watch (bounded per call). Returns executions started."""
    from app.services.notifications import notify

    now = utcnow()
    due = db.scalars(select(MailboxWatch).where(MailboxWatch.is_active.is_(True), MailboxWatch.next_poll_at.is_not(None),
                                                MailboxWatch.next_poll_at <= now).order_by(MailboxWatch.next_poll_at).limit(PER_TICK)).all()
    total = 0
    for watch in due:
        set_tenant(db, None, bypass_rls=True)  # claiming and bookkeeping span tenants; polling is tenant-scoped
        if not _claim(db, watch, now):
            continue  # another worker took it
        try:
            total += poll_watch(db, watch)
            if watch.failures or watch.last_error:
                watch.failures, watch.last_error = 0, None
                db.commit()
        except (NodeError, AppError) as exc:
            db.rollback()
            set_tenant(db, None, bypass_rls=True)
            watch = db.get(MailboxWatch, watch.id)
            if watch is None:
                continue
            watch.failures += 1
            watch.last_error = exc.message[:500]
            watch.next_poll_at = utcnow() + timedelta(minutes=min(60, watch.poll_minutes * 2 ** min(watch.failures, 6)))
            if watch.failures == NOTIFY_AFTER:
                notify(db, "integration.disconnected", watch.organization_id, title="A mailbox trigger cannot read its mailbox",
                       body=watch.last_error, severity="error",
                       data={"workflow_id": str(watch.workflow_id), "node_id": watch.node_key, "integration_id": str(watch.integration_id)})
            db.commit()
            log.warning("mailbox watch %s failed (%s): %s", watch.id, watch.failures, exc.message)
        except Exception:  # noqa: BLE001 - one broken mailbox must not stop the others
            db.rollback()
            log.exception("mailbox watch %s crashed", watch.id)
    set_tenant(db, None, bypass_rls=True)
    return total
