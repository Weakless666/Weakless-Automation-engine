"""Stored files: API, limits, tenancy, and files moving through workflows."""
from __future__ import annotations

import base64
import email
import json
from datetime import timedelta
from email.message import EmailMessage

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import utcnow
from app.models import MailboxWatch, StoredFile, WorkflowExecution, WorkflowExecutionStep
from app.services.retention import run_retention
from tests.conftest import login, make_user, make_workflow, run, wf_def
from tests.test_platform import hdr

TRIG = {"id": "t", "type": "trigger.manual"}
PDF = b"%PDF-1.4\n" + bytes(range(256)) * 4  # binary on purpose: not valid UTF-8


def _upload(client, h, name="invoice.pdf", content=PDF, ctype="application/pdf"):
    return client.post("/api/v1/files", headers=h, files={"file": (name, content, ctype)})


def test_upload_list_download_delete_and_limits(client, db, org, other_org, monkeypatch):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    make_user(db, "viewer@client.example", org=org, role="VIEWER")
    make_user(db, "outsider@client.example", org=other_org, role="CLIENT_ADMIN")
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    up = _upload(client, h, name="../../etc/Invoice 7.pdf")
    assert up.status_code == 201, up.text
    f = up.json()
    assert f["name"] == "Invoice 7.pdf" and f["size"] == len(PDF) and f["source"] == "upload" and f["expires_at"] is None  # path parts are dropped

    page = client.get("/api/v1/files", headers=h).json()
    assert page["total"] == 1 and page["storage"]["used_bytes"] == len(PDF) and "data" not in page["items"][0]
    got = client.get(f"/api/v1/files/{f['id']}/content", headers=h)
    assert got.content == PDF
    assert got.headers["content-disposition"].startswith("attachment") and got.headers["content-type"] == "application/octet-stream"
    assert got.headers["x-content-type-options"] == "nosniff"  # an uploaded HTML or SVG can never render on our origin

    viewer = hdr(login(client, "viewer@client.example"), org.id)
    assert client.get(f"/api/v1/files/{f['id']}/content", headers=viewer).status_code == 200
    assert _upload(client, viewer).status_code == 403 and client.delete(f"/api/v1/files/{f['id']}", headers=viewer).status_code == 403
    outsider = hdr(login(client, "outsider@client.example"), other_org.id)
    assert client.get(f"/api/v1/files/{f['id']}", headers=outsider).status_code == 404
    assert client.get(f"/api/v1/files/{f['id']}/content", headers=outsider).status_code == 404

    monkeypatch.setenv("WAE_FILE_MAX_MB", "1")
    get_settings.cache_clear()
    h = hdr(login(client, "eng@weakless.pro"), org.id)
    assert _upload(client, h, content=b"x" * (1024 * 1024 + 1)).status_code == 422
    assert _upload(client, h, content=b"").status_code == 422
    monkeypatch.setenv("WAE_FILE_MAX_MB", "25")
    monkeypatch.setenv("WAE_ORG_STORAGE_MB", "0")
    get_settings.cache_clear()
    full = _upload(client, h)
    assert full.status_code == 402 and "storage" in full.json()["error"]["message"]
    assert client.delete(f"/api/v1/files/{f['id']}", headers=h).status_code == 200
    assert client.get(f"/api/v1/files/{f['id']}", headers=h).status_code == 404


def test_a_workflow_downloads_a_file_emails_it_and_uploads_it(db, org, worker, http_mock, monkeypatch):
    from app.integrations.service import create_integration

    http_mock.add("GET", "https://files.example.com/report", lambda r: httpx.Response(
        200, content=PDF, headers={"content-type": "application/pdf", "content-disposition": 'attachment; filename="q3 report.pdf"'}))
    http_mock.add("GET", "https://www.googleapis.com/oauth2/v3/userinfo", (200, {"email": "me@example.com"}))
    http_mock.add("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", (200, {"id": "m1", "threadId": "t1"}))
    http_mock.add("POST", "https://www.googleapis.com/upload/drive/v3/files", (200, {"id": "d1", "name": "q3 report.pdf", "webViewLink": "https://drive/d1"}))
    from app.integrations import service as isvc

    monkeypatch.setattr(isvc, "oauth_configured", lambda p: True)
    gmail = create_integration(db, org.id, "gmail", "Mail", {}, None)
    drive = create_integration(db, org.id, "google_drive", "Drive", {}, None)
    for integ in (gmail, drive):
        isvc.store_secret(db, integ, {"access_token": "ya29.test-token-123456"})
        integ.status = "connected"
    db.commit()

    wf = make_workflow(db, org, wf_def([
        TRIG,
        {"id": "get", "type": "action.http_request", "config": {"url": "https://files.example.com/report", "save_response_as_file": True}},
        {"id": "csv", "type": "action.save_file", "config": {"name": "summary.csv", "content": "name,total\n{{ trigger.name }},42\n", "content_type": "text/csv"}},
        {"id": "mail", "type": "action.send_email", "config": {"integration_id": str(gmail.id), "to": "boss@example.com", "subject": "Report",
                                                              "body": "Attached.", "attachments": ["{{ nodes.get.file }}", "{{ nodes.csv.file.file_id }}"]}},
        {"id": "drive", "type": "action.upload_file", "config": {"integration_id": str(drive.id), "file": "{{ nodes.get.file }}"}},
    ], [("t", "get"), ("get", "csv"), ("csv", "mail"), ("mail", "drive")]))
    ex = run(db, worker, wf, {"name": "Ana"})
    assert ex.status == "COMPLETED", ex.error

    stored = {f.name: f for f in db.scalars(select(StoredFile).where(StoredFile.organization_id == org.id))}
    assert set(stored) == {"q3 report.pdf", "summary.csv"} and stored["q3 report.pdf"].size == len(PDF)
    assert all(f.source == "workflow" and f.execution_id == ex.id and f.expires_at is not None for f in stored.values())

    sent = next(r for r in http_mock.requests if r.url.path.endswith("/messages/send"))
    msg = email.message_from_bytes(base64.urlsafe_b64decode(json.loads(sent.content)["raw"]))
    parts = {p.get_filename(): p.get_payload(decode=True) for p in msg.walk() if p.get_filename()}
    assert parts == {"q3 report.pdf": PDF, "summary.csv": b"name,total\nAna,42\n"}  # bytes arrive intact
    uploaded = next(r for r in http_mock.requests if r.url.path == "/upload/drive/v3/files")
    assert PDF in uploaded.content and b"application/pdf" in uploaded.content

    # Step logs and execution state hold references, never the bytes.
    logged = json.dumps([[s.input, s.output] for s in db.scalars(select(WorkflowExecutionStep).where(WorkflowExecutionStep.execution_id == ex.id))], default=str)
    assert "%PDF" not in logged and stored["q3 report.pdf"].sha256 not in logged and len(json.dumps(ex.state)) < 5000
    assert ex.output["drive"]["link"] == "https://drive/d1"

    # Workflow files expire under the retention policy; nothing else touches them.
    assert run_retention(db)["files_deleted"] == 0
    assert run_retention(db, now=utcnow() + timedelta(days=31))["files_deleted"] == 2


def test_files_cannot_cross_tenants_inside_a_workflow(client, db, org, other_org, worker):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    foreign = _upload(client, hdr(login(client, "eng@weakless.pro"), other_org.id)).json()
    wf = make_workflow(db, org, wf_def([TRIG, {"id": "up", "type": "action.upload_file",
                                               "config": {"integration_id": "00000000-0000-0000-0000-000000000000", "file": foreign["id"]}}], [("t", "up")]))
    ex = run(db, worker, wf)
    assert ex.status == "FAILED" and ex.error["code"] == "file_not_found"


def test_mailbox_attachments_become_files(db, org, worker, monkeypatch):
    from app.integrations import imap
    from app.integrations.service import create_integration
    from app.services.mailbox import poll_due_mailboxes
    from tests.imap_server import ImapServer

    server = ImapServer()
    try:
        monkeypatch.setattr(imap, "_ssl_context", server.client_context)
        monkeypatch.setenv("WAE_ALLOW_PRIVATE_NETWORK", "true")
        get_settings.cache_clear()
        box = create_integration(db, org.id, "imap", "Desk", {"host": "127.0.0.1", "port": str(server.port), "username": server.username,
                                                               "password": server.password}, None)
        db.commit()
        wf = make_workflow(db, org, wf_def([
            {"id": "mail", "type": "trigger.mailbox", "config": {"integration_id": str(box.id), "save_attachments": True}},
            {"id": "x", "type": "logic.transform", "config": {"fields": {"first": "{{ trigger.attachments[0].file_id }}", "count": "{{ trigger.attachments | length }}"}}}],
            [("mail", "x")]))

        def due():
            for w in db.scalars(select(MailboxWatch)):
                w.next_poll_at = utcnow() - timedelta(seconds=1)
            db.commit()
            return poll_due_mailboxes(db)

        due()
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "Guest <g@example.org>", "desk@example.com", "Booking form"
        m.set_content("See attached")
        m.add_attachment(PDF, maintype="application", subtype="pdf", filename="booking.pdf")
        server.deliver(bytes(m))
        assert due() == 1
        worker.drain()
        ex = db.scalar(select(WorkflowExecution).where(WorkflowExecution.workflow_id == wf.id))
        assert ex.status == "COMPLETED" and ex.output["x"]["count"] == 1
        from app.services.files import get_file

        f = get_file(db, org.id, ex.output["x"]["first"], with_data=True)
        assert f.data == PDF and f.name == "booking.pdf" and f.source == "mailbox" and f.content_type == "application/pdf"
    finally:
        server.close()
