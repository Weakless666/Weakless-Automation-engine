"""Mailbox Trigger against a real IMAP server over TLS (tests/imap_server.py)."""
from __future__ import annotations

from datetime import timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.core.errors import NodeError, ValidationFailed
from app.db import utcnow
from app.integrations import imap
from app.integrations.service import connection, create_integration
from app.models import MailboxWatch, Notification, WebhookEvent, WorkflowExecution
from app.services import workflows as wfs
from app.services.mailbox import poll_due_mailboxes
from tests.conftest import make_user, make_workflow, wf_def
from tests.imap_server import ImapServer


@pytest.fixture
def server(monkeypatch):
    s = ImapServer()
    monkeypatch.setattr(imap, "_ssl_context", s.client_context)  # trust the test certificate (still verified)
    monkeypatch.setenv("WAE_ALLOW_PRIVATE_NETWORK", "true")  # the test server listens on localhost
    get_settings.cache_clear()
    yield s
    s.close()


def _mail(subject: str, sender: str = "Guest <guest@example.org>", body: str = "Hello", *, html: str | None = None, attachment: bytes | None = None) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = sender, "Desk <desk@example.com>", subject, f"<{abs(hash(subject))}@example.org>"
    m["Date"] = "Thu, 12 Nov 2026 09:30:00 +0200"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if attachment:
        m.add_attachment(attachment, maintype="application", subtype="pdf", filename="booking.pdf")
    return bytes(m)


def _mailbox(db, org, server: ImapServer, password: str | None = None, name: str = "Front desk"):
    integration = create_integration(db, org.id, "imap", name, {"host": "127.0.0.1", "port": str(server.port), "security": "ssl",
                                                                         "username": server.username, "password": password or server.password}, None)
    db.commit()
    return integration


def _flow(db, org, integration, **cfg):
    return make_workflow(db, org, wf_def(
        [{"id": "mail", "type": "trigger.mailbox", "config": {"integration_id": str(integration.id), **cfg}},
         {"id": "x", "type": "logic.transform", "config": {"fields": {"subject": "{{ trigger.subject }}", "who": "{{ trigger.from.email }}"}}}],
        [("mail", "x")]), name="Inbox flow")


def _poll(db) -> int:
    for w in db.scalars(select(MailboxWatch)):
        w.next_poll_at = utcnow() - timedelta(seconds=1)  # due now
    db.commit()
    return poll_due_mailboxes(db)


def test_integration_connects_over_tls_and_reports_bad_passwords(db, org, server):
    good = _mailbox(db, org, server)
    assert good.status == "connected" and server.logins == 1
    bad = _mailbox(db, org, server, password="wrong-password", name="Typo")
    assert bad.status == "error" and "authentication failed" in bad.last_error.lower()


def test_only_new_mail_starts_the_workflow_exactly_once(db, org, server, worker):
    server.deliver(_mail("Old booking"))  # already in the mailbox before activation
    integration = _mailbox(db, org, server)
    wf = _flow(db, org, integration)
    watch = db.scalar(select(MailboxWatch))
    assert watch.is_active and watch.next_poll_at is not None and watch.last_uid is None

    assert _poll(db) == 0  # the first poll only notes where new mail begins
    db.refresh(watch)
    assert watch.last_uid == 1 and watch.uidvalidity == 1000

    server.deliver(_mail("Booking for Friday", html="<p>Booking for <b>Friday</b></p>", attachment=b"%PDF-1.4 test"))
    server.deliver(_mail("Late checkout?", sender="Ana <ana@example.org>"))
    assert _poll(db) == 2
    assert _poll(db) == 0  # nothing new: no duplicates
    worker.drain()
    runs = db.scalars(select(WorkflowExecution).where(WorkflowExecution.workflow_id == wf.id).order_by(WorkflowExecution.created_at)).all()
    assert [r.status for r in runs] == ["COMPLETED", "COMPLETED"]
    assert [r.output["x"] for r in runs] == [{"subject": "Booking for Friday", "who": "guest@example.org"},
                                             {"subject": "Late checkout?", "who": "ana@example.org"}]
    first = runs[0].trigger_payload
    assert first["text"].strip() == "Hello" and "<b>Friday</b>" in first["html"] and first["date"].startswith("2026-11-12T09:30")
    assert first["attachments"] == [{"filename": "booking.pdf", "content_type": "application/pdf", "size": 13}]
    assert first["to"] == [{"name": "Desk", "email": "desk@example.com"}] and first["uid"] == 2 and first["folder"] == "INBOX"
    assert runs[0].trigger_type == "mailbox" and runs[0].idempotency_key.startswith("mailbox:")
    assert len(db.scalars(select(WebhookEvent).where(WebhookEvent.source == "mailbox")).all()) == 2
    assert server.flags(2) == set()  # read flags untouched unless the trigger is told to mark mail as read


def test_filters_and_mark_as_read_touch_only_matching_mail(db, org, server, worker):
    integration = _mailbox(db, org, server)
    _flow(db, org, integration, subject_contains="booking", from_contains="example.org", mark_as_read=True)
    _poll(db)
    matching = server.deliver(_mail("New BOOKING request"))
    newsletter = server.deliver(_mail("Weekly newsletter"))
    other_sender = server.deliver(_mail("Booking spam", sender="x@spam.test"))
    assert _poll(db) == 1
    assert server.flags(matching) == {"\\Seen"} and server.flags(newsletter) == set() and server.flags(other_sender) == set()
    assert db.scalar(select(MailboxWatch.last_uid)) == other_sender  # skipped mail is not looked at again


def test_a_rebuilt_folder_or_a_reactivation_never_replays_old_mail(db, org, server, worker):
    integration = _mailbox(db, org, server)
    wf = _flow(db, org, integration)
    _poll(db)
    server.deliver(_mail("One"))
    assert _poll(db) == 1
    server.reset_uidvalidity()  # every UID now means something else
    assert _poll(db) == 0
    server.deliver(_mail("Two"))
    assert _poll(db) == 1

    wfs.set_active(db, wf, False)
    db.commit()
    server.deliver(_mail("Arrived while off"))
    assert poll_due_mailboxes(db) == 0
    wfs.set_active(db, wf, True)
    db.commit()
    assert _poll(db) == 0  # mail from while the workflow was off is not replayed
    server.deliver(_mail("Three"))
    assert _poll(db) == 1
    worker.drain()
    subjects = [e.trigger_payload["subject"] for e in db.scalars(select(WorkflowExecution).order_by(WorkflowExecution.created_at))]
    assert subjects == ["One", "Two", "Three"]


def test_mailbox_problems_back_off_notify_and_recover(db, org, server, worker):
    make_user(db, "admin@client.example", org=org, role="CLIENT_ADMIN")
    integration = _mailbox(db, org, server)
    _flow(db, org, integration)
    _poll(db)
    server.password = "rotated-by-the-mail-host"
    for attempt in range(1, 4):
        assert _poll(db) == 0
        watch = db.scalar(select(MailboxWatch))
        assert watch.failures == attempt and "authentication failed" in watch.last_error.lower()
        assert watch.next_poll_at > utcnow() + timedelta(minutes=3)  # backs off instead of hammering the server
    assert db.scalar(select(Notification).where(Notification.event_type == "integration.disconnected")) is not None
    server.password = "app-password-1"
    server.deliver(_mail("After the fix"))
    assert _poll(db) == 1
    watch = db.scalar(select(MailboxWatch))
    assert watch.failures == 0 and watch.last_error is None


def test_untrusted_certificates_and_internal_hosts_are_refused(db, org, server, monkeypatch):
    integration = _mailbox(db, org, server)
    conn = connection(db, integration)
    monkeypatch.setattr(imap, "_ssl_context", lambda: __import__("ssl").create_default_context())  # system trust store only
    with pytest.raises(NodeError) as tls:
        imap.test(conn)
    assert tls.value.code == "tls_error"
    monkeypatch.setenv("WAE_ALLOW_PRIVATE_NETWORK", "false")
    get_settings.cache_clear()
    with pytest.raises(NodeError) as ssrf:
        imap.test(conn)
    assert ssrf.value.code == "ssrf_blocked"


def test_a_trigger_cannot_watch_another_organizations_mailbox(db, org, other_org, server):
    foreign = _mailbox(db, other_org, server)
    with pytest.raises(ValidationFailed, match="does not exist in this organization"):
        _flow(db, org, foreign)
