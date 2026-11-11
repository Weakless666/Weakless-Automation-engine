"""Security tests: authentication, tenant isolation, RBAC, CSRF, secrets, webhooks."""
from __future__ import annotations

import json
import time

from sqlalchemy import select

from app.core import vault
from app.core.redact import MASK, redact
from app.core.security import hmac_sha256_hex
from app.models import AuditLog, IntegrationCredential, WebhookEndpoint, WebhookEvent
from tests.conftest import ADMIN_PASSWORD, login, make_user, make_workflow, wf_def

TRIG = {"id": "t", "type": "trigger.manual"}


def hdr(auth: dict, org) -> dict:
    return {**auth, "X-Organization-Id": str(org.id)}


# ----------------------------------------------------------------------------- authentication


def test_unauthenticated_requests_are_rejected(client, org):
    for method, path in [("get", "/api/v1/workflows"), ("get", "/api/v1/organizations"), ("get", "/api/v1/executions"),
                         ("get", "/api/v1/integrations"), ("get", "/api/v1/audit-logs"), ("get", "/api/v1/auth/me"),
                         ("post", "/api/v1/organizations"), ("get", "/api/v1/settings"), ("get", "/api/v1/templates")]:
        r = getattr(client, method)(path, headers={"X-Organization-Id": str(org.id)})
        assert r.status_code == 401, (path, r.status_code)
        assert r.json()["error"]["code"] == "unauthorized"


def test_login_failure_is_generic_and_recorded(client, db):
    make_user(db, "admin@weakless.pro", platform_role="SUPER_ADMIN")
    bad = client.post("/api/v1/auth/login", json={"email": "admin@weakless.pro", "password": "wrong"})
    unknown = client.post("/api/v1/auth/login", json={"email": "nobody@weakless.pro", "password": "wrong"})
    assert bad.status_code == unknown.status_code == 401
    assert bad.json()["error"]["message"] == unknown.json()["error"]["message"]  # no account enumeration
    from app.models import LoginHistory

    assert db.scalar(select(LoginHistory).where(LoginHistory.email == "admin@weakless.pro", LoginHistory.success.is_(False))) is not None


def test_account_locks_after_repeated_failures(client, db):
    make_user(db, "lock@weakless.pro", platform_role="ADMIN")
    for _ in range(8):
        client.post("/api/v1/auth/login", json={"email": "lock@weakless.pro", "password": "wrong"})
    r = client.post("/api/v1/auth/login", json={"email": "lock@weakless.pro", "password": ADMIN_PASSWORD})
    assert r.status_code == 401  # correct password, but the account is locked


def test_logout_revokes_the_session(client, db):
    make_user(db, "admin@weakless.pro", platform_role="SUPER_ADMIN")
    auth = login(client, "admin@weakless.pro")
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=auth).status_code == 200
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 401


def test_cookie_sessions_require_csrf_token_for_mutations(client, db, org):
    make_user(db, "admin@weakless.pro", platform_role="SUPER_ADMIN")
    r = client.post("/api/v1/auth/login", json={"email": "admin@weakless.pro", "password": ADMIN_PASSWORD})
    csrf = r.json()["csrf_token"]  # the session cookie is now in the client's jar
    o = {"X-Organization-Id": str(org.id)}
    assert client.get("/api/v1/workflows", headers=o).status_code == 200
    blocked = client.post("/api/v1/workflows", headers=o, json={"name": "x"})
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "csrf_failed"
    assert client.post("/api/v1/workflows", headers={**o, "X-WAE-CSRF": "forged"}, json={"name": "x"}).status_code == 403
    assert client.post("/api/v1/workflows", headers={**o, "X-WAE-CSRF": csrf}, json={"name": "x"}).status_code == 201


def test_security_headers_and_error_envelope(client):
    r = client.get("/api/v1/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "DENY"
    assert "X-Request-Id" in r.headers
    missing = client.get("/api/v1/does-not-exist")
    assert missing.status_code == 404 and set(missing.json()["error"]) >= {"code", "message", "request_id"}


# ----------------------------------------------------------------------------- tenant isolation


def test_client_cannot_reach_another_tenant(client, db, org, other_org, worker):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    make_user(db, "owner@rival.co", org=other_org, role="CLIENT_ADMIN")
    wf = make_workflow(db, other_org, wf_def([TRIG], []), name="Rival secret workflow")
    acme = login(client, "owner@acme.co")

    # 1. Claiming the other tenant in the header is refused (indistinguishable from "does not exist").
    r = client.get("/api/v1/workflows", headers=hdr(acme, other_org))
    assert r.status_code == 404
    # 2. Using the own tenant but another tenant's object id (IDOR) finds nothing.
    for path in [f"/api/v1/workflows/{wf.id}", f"/api/v1/workflows/{wf.id}/versions"]:
        assert client.get(path, headers=hdr(acme, org)).status_code == 404
    for path in [f"/api/v1/workflows/{wf.id}/execute", f"/api/v1/workflows/{wf.id}/activate", f"/api/v1/workflows/{wf.id}/publish"]:
        assert client.post(path, headers=hdr(acme, org)).status_code in (403, 404)
    assert client.delete(f"/api/v1/workflows/{wf.id}", headers=hdr(acme, org)).status_code in (403, 404)
    # 3. Lists only contain the caller's organizations and data.
    orgs = client.get("/api/v1/organizations", headers=acme).json()["items"]
    assert [o["name"] for o in orgs] == ["Acme Hotel"]
    assert client.get("/api/v1/workflows", headers=hdr(acme, org)).json()["total"] == 0
    assert client.get(f"/api/v1/organizations/{other_org.id}", headers=acme).status_code == 404
    # 4. Platform-wide views are staff only.
    assert client.get("/api/v1/executions", headers=acme).status_code == 403
    assert client.get("/api/v1/analytics/overview", headers=acme).status_code == 403
    assert client.get("/api/v1/audit-logs", headers=acme).status_code == 403


def test_cross_tenant_execution_and_integration_access(client, db, org, other_org, worker, http_mock):
    from tests.conftest import run

    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    wf = make_workflow(db, other_org, wf_def([TRIG], []))
    ex = run(db, worker, wf, {"secret": "rival-data"})
    acme = login(client, "owner@acme.co")
    assert client.get(f"/api/v1/executions/{ex.id}", headers=hdr(acme, org)).status_code == 404
    assert client.post(f"/api/v1/executions/{ex.id}/retry", headers=hdr(acme, org)).status_code == 404

    # An integration created in the other tenant cannot be used by a workflow in this tenant.
    http_mock.add("GET", "https://api.telegram.org/", (200, {"ok": True, "result": {"username": "rival_bot"}}))
    eng = login(client, "eng@weakless.pro")
    created = client.post("/api/v1/integrations", headers=hdr(eng, other_org),
                          json={"provider_key": "telegram", "values": {"bot_token": "123456789:" + "A" * 35}})
    assert created.status_code == 201 and created.json()["status"] == "connected"
    rival_integration = created.json()["id"]
    assert client.get(f"/api/v1/integrations/{rival_integration}", headers=hdr(acme, org)).status_code == 404
    d = wf_def([TRIG, {"id": "tg", "type": "action.send_telegram", "config": {"integration_id": rival_integration, "chat_id": "1", "text": "hi"}}], [("t", "tg")])
    stolen = run(db, worker, make_workflow(db, org, d, name="steal"))
    assert stolen.status == "FAILED" and stolen.error["code"] == "integration_not_found"
    assert not [r for r in http_mock.requests if "sendMessage" in str(r.url)]


# ----------------------------------------------------------------------------- RBAC


def test_role_permissions_are_enforced(client, db, org):
    make_user(db, "viewer@acme.co", org=org, role="VIEWER")
    make_user(db, "member@acme.co", org=org, role="CLIENT_MEMBER")
    make_user(db, "admin@acme.co", org=org, role="CLIENT_ADMIN")
    viewer, member, admin = (login(client, e) for e in ("viewer@acme.co", "member@acme.co", "admin@acme.co"))
    assert client.get("/api/v1/workflows", headers=hdr(viewer, org)).status_code == 200
    # Clients cannot build or publish workflows; that is an engineer capability.
    for who in (viewer, member, admin):
        assert client.post("/api/v1/workflows", headers=hdr(who, org), json={"name": "x"}).status_code == 403
    assert client.get("/api/v1/integrations", headers=hdr(viewer, org)).status_code == 403
    assert client.get("/api/v1/members", headers=hdr(viewer, org)).status_code == 403
    assert client.get("/api/v1/billing/subscription", headers=hdr(member, org)).status_code == 403
    assert client.get("/api/v1/billing/subscription", headers=hdr(admin, org)).status_code == 200
    # Platform administration is closed to every client role.
    for path in ["/api/v1/users", "/api/v1/settings", "/api/v1/roles", "/api/v1/dead-letters", "/api/v1/analytics/system"]:
        assert client.get(path, headers=admin).status_code == 403, path
    assert client.post("/api/v1/organizations", headers=admin, json={"name": "New Co"}).status_code == 403


def test_privilege_escalation_is_blocked(client, db, org):
    make_user(db, "admin@acme.co", org=org, role="CLIENT_ADMIN")
    make_user(db, "member@acme.co", org=org, role="CLIENT_MEMBER")
    make_user(db, "root@weakless.pro", platform_role="SUPER_ADMIN")
    make_user(db, "staffadmin@weakless.pro", platform_role="ADMIN")
    admin = login(client, "admin@acme.co")
    # A client admin cannot hand out staff roles or touch organization lifecycle.
    r = client.post("/api/v1/members", headers=hdr(admin, org), json={"email": "x@acme.co", "role": "SUPER_ADMIN", "password": ADMIN_PASSWORD})
    assert r.status_code == 422
    assert client.patch(f"/api/v1/organizations/{org.id}", headers=admin, json={"mode": "agency"}).status_code == 403
    assert client.patch(f"/api/v1/organizations/{org.id}", headers=admin, json={"status": "suspended"}).status_code == 403
    # A member cannot promote themself.
    member = login(client, "member@acme.co")
    members = client.get("/api/v1/members", headers=hdr(admin, org)).json()["items"]
    me = next(m for m in members if m["email"] == "member@acme.co")
    assert client.patch(f"/api/v1/members/{me['id']}", headers=hdr(member, org), json={"role": "CLIENT_ADMIN"}).status_code == 403
    # A (non-super) staff admin cannot mint or modify Super Admins.
    staff = login(client, "staffadmin@weakless.pro")
    r = client.post("/api/v1/users", headers=staff, json={"email": "evil@weakless.pro", "full_name": "E", "password": ADMIN_PASSWORD, "platform_role": "SUPER_ADMIN"})
    assert r.status_code == 403
    root = next(u for u in client.get("/api/v1/users", headers=login(client, "root@weakless.pro")).json()["items"] if u["email"] == "root@weakless.pro")
    assert client.patch(f"/api/v1/users/{root['id']}", headers=staff, json={"is_active": False}).status_code == 403
    # Client roles can never be granted platform permissions.
    root_auth = login(client, "root@weakless.pro")
    r = client.put("/api/v1/roles/CLIENT_ADMIN", headers=root_auth, json={"permissions": ["org.read", "platform.settings.manage"]})
    assert r.status_code == 422


def test_agency_mode_clients_cannot_install_templates(client, db):
    from app.services.organizations import create_organization

    agency_org = create_organization(db, name="Managed Co", mode="agency")
    db.commit()
    make_user(db, "admin@managed.co", org=agency_org, role="CLIENT_ADMIN")
    auth = login(client, "admin@managed.co")
    tid = client.get("/api/v1/templates", headers=auth).json()["items"][0]["id"]
    r = client.post(f"/api/v1/templates/{tid}/install", headers=hdr(auth, agency_org), json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "agency_managed"


def test_sensitive_operations_are_audited(client, db, org):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    auth = login(client, "eng@weakless.pro")
    wf = client.post("/api/v1/workflows", headers=hdr(auth, org), json={"name": "Audited"}).json()
    client.post(f"/api/v1/workflows/{wf['id']}/publish", headers=hdr(auth, org))
    client.post(f"/api/v1/workflows/{wf['id']}/activate", headers=hdr(auth, org))
    actions = set(db.scalars(select(AuditLog.action).where(AuditLog.organization_id == org.id)))
    assert {"workflow.created", "workflow.published", "workflow.activated"} <= actions
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "workflow.published"))
    assert entry.actor_email == "eng@weakless.pro" and entry.resource_id == wf["id"]


# ----------------------------------------------------------------------------- credentials


def test_vault_roundtrip_tenant_binding_and_rotation(monkeypatch):
    import base64

    from app.core.vault import LocalKeyProvider

    env = vault.encrypt_json({"api_key": "sk-secret-value"}, vault.aad_for("org-a", "integration:1"))
    assert "sk-secret-value" not in env
    assert vault.decrypt_json(env, vault.aad_for("org-a", "integration:1")) == {"api_key": "sk-secret-value"}
    try:  # a ciphertext moved to another tenant's row must not decrypt
        vault.decrypt_json(env, vault.aad_for("org-b", "integration:1"))
        raise AssertionError("decrypted with the wrong tenant binding")
    except vault.VaultError:
        pass
    # Rotation: add a new KEK, rewrap, and the old KEK is no longer needed.
    old = vault.get_key_provider()
    ring = {"test1": base64.b64decode(base64.b64encode(b"\x01" * 32)), "k2": b"\x02" * 32}
    vault.set_key_provider(LocalKeyProvider(ring, "k2"))
    try:
        rotated = vault.rewrap(env)
        assert vault.envelope_key_id(rotated) == "k2"
        vault.set_key_provider(LocalKeyProvider({"k2": b"\x02" * 32}, "k2"))
        assert vault.decrypt_json(rotated, vault.aad_for("org-a", "integration:1")) == {"api_key": "sk-secret-value"}
    finally:
        vault.set_key_provider(old)


def test_credentials_never_leave_the_backend(client, db, org, http_mock, worker):
    from tests.conftest import run, steps

    token = "987654321:" + "Z" * 35
    http_mock.add("GET", "https://api.telegram.org/", (200, {"ok": True, "result": {"username": "acme_bot"}}))
    http_mock.add("POST", "https://api.telegram.org/", (200, {"ok": True, "result": {"message_id": 7}}))
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    auth = login(client, "eng@weakless.pro")
    created = client.post("/api/v1/integrations", headers=hdr(auth, org), json={"provider_key": "telegram", "values": {"bot_token": token}})
    assert created.status_code == 201
    iid = created.json()["id"]
    # 1. No API response contains the secret.
    for r in (created, client.get("/api/v1/integrations", headers=hdr(auth, org)), client.get(f"/api/v1/integrations/{iid}", headers=hdr(auth, org)),
              client.post(f"/api/v1/integrations/{iid}/test", headers=hdr(auth, org))):
        assert token not in r.text and "ZZZZZ" not in r.text
    assert created.json()["has_credentials"] is True
    # 2. It is encrypted at rest.
    cred = db.scalar(select(IntegrationCredential))
    assert token not in cred.envelope and json.loads(cred.envelope)["kid"]
    # 3. Execution logs do not capture it either.
    d = wf_def([TRIG, {"id": "tg", "type": "action.send_telegram", "config": {"integration_id": iid, "chat_id": "42", "text": "hello"}}], [("t", "tg")])
    ex = run(db, worker, make_workflow(db, org, d))
    assert ex.status == "COMPLETED" and ex.output == {"tg": {"message_id": 7}}
    detail = client.get(f"/api/v1/executions/{ex.id}", headers=hdr(auth, org))
    assert token not in detail.text
    assert token not in json.dumps([s.input for s in steps(db, ex)], default=str)
    # 4. After revocation the credential is gone and the integration is unusable.
    assert client.post(f"/api/v1/integrations/{iid}/revoke", headers=hdr(auth, org)).json()["status"] == "revoked"
    assert db.scalar(select(IntegrationCredential)) is None
    assert run(db, worker, make_workflow(db, org, d, name="after revoke")).error["code"] == "integration_not_connected"


def test_redaction_masks_secrets():
    data = redact({"Authorization": "Bearer abc.def.ghi", "nested": {"api_key": "sk-1234567890abcdefghij", "note": "token sk-abcdefghijklmnopqrstuv in text"},
                   "ok": "plain"})
    assert data["Authorization"] == MASK and data["nested"]["api_key"] == MASK and "sk-" not in data["nested"]["note"] and data["ok"] == "plain"


def test_oauth_integration_without_platform_credentials_is_not_reported_connected(client, db, org):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    auth = login(client, "eng@weakless.pro")
    gmail = next(p for p in client.get("/api/v1/integrations/providers", headers=hdr(auth, org)).json()["items"] if p["key"] == "gmail")
    assert gmail["available"] is False and "WAE_OAUTH_GOOGLE_CLIENT_ID" in gmail["unavailable_reason"]
    created = client.post("/api/v1/integrations", headers=hdr(auth, org), json={"provider_key": "gmail"})
    assert created.json()["status"] == "awaiting_credentials"
    start = client.post(f"/api/v1/integrations/{created.json()['id']}/oauth/start", headers=hdr(auth, org))
    assert start.status_code == 409 and start.json()["error"]["code"] == "awaiting_credentials"


# ----------------------------------------------------------------------------- webhooks


def _webhook(db, org):
    d = wf_def([{"id": "hook", "type": "trigger.webhook", "config": {}},
                {"id": "x", "type": "logic.transform", "config": {"fields": {"order": "{{ nodes.hook.body.order_id }}"}}}], [("hook", "x")])
    wf = make_workflow(db, org, d)
    ep = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.workflow_id == wf.id))
    from app.services.workflows import endpoint_secret

    return wf, ep, endpoint_secret(ep)


def _signed(secret: str, body: bytes, ts: int | None = None) -> dict:
    ts = ts or int(time.time())
    return {"X-WAE-Timestamp": str(ts), "X-WAE-Signature": "sha256=" + hmac_sha256_hex(secret, str(ts).encode() + b"." + body),
            "Content-Type": "application/json"}


def test_webhook_signature_replay_and_duplicate_protection(client, db, org, worker):
    wf, ep, secret = _webhook(db, org)
    url = f"/api/v1/hooks/{ep.endpoint_key}"
    body = json.dumps({"order_id": "A-1"}).encode()

    assert client.post(url, content=body, headers={"Content-Type": "application/json"}).status_code == 401  # unsigned
    assert client.post(url, content=body, headers=_signed("wrong-secret", body)).status_code == 401  # bad signature
    tampered = _signed(secret, body)
    assert client.post(url, content=b'{"order_id": "EVIL"}', headers=tampered).status_code == 401  # body changed after signing
    assert client.post(url, content=body, headers=_signed(secret, body, ts=int(time.time()) - 3600)).status_code == 401  # stale (replay)

    good = _signed(secret, body)
    first = client.post(url, content=body, headers=good)
    assert first.status_code == 202 and first.json()["status"] == "processed"
    assert client.post(url, content=body, headers=good).json() == {"status": "duplicate"}  # same delivery replayed
    worker.drain()
    from app.models import WorkflowExecution

    executions = db.scalars(select(WorkflowExecution).where(WorkflowExecution.workflow_id == wf.id)).all()
    assert len(executions) == 1 and executions[0].status == "COMPLETED" and executions[0].output == {"x": {"order": "A-1"}}
    statuses = sorted(e.status for e in db.scalars(select(WebhookEvent).where(WebhookEvent.endpoint_id == ep.id)))
    assert statuses == ["processed", "rejected", "rejected", "rejected", "rejected"]
    assert client.post("/api/v1/hooks/not-a-real-key", content=body, headers=good).status_code == 404


def test_webhook_rate_limit_and_inactive_workflow(client, db, org):
    wf, ep, secret = _webhook(db, org)
    ep.rate_limit_per_minute = 2
    db.commit()
    url = f"/api/v1/hooks/{ep.endpoint_key}"
    codes = []
    for i in range(4):
        body = json.dumps({"order_id": i}).encode()
        codes.append(client.post(url, content=body, headers=_signed(secret, body)).status_code)
    assert codes == [202, 202, 429, 429]


def test_stripe_signature_scheme(db, org):
    from app.services.webhooks import Rejected, verify_signature

    body = b'{"id": "evt_1", "type": "checkout.session.completed"}'
    ts = str(int(time.time()))
    sig = hmac_sha256_hex("whsec_test", ts.encode() + b"." + body)
    verify_signature("stripe", "whsec_test", {"stripe-signature": f"t={ts},v1={sig}"}, body)
    for headers in ({"stripe-signature": f"t={ts},v1=deadbeef"}, {"stripe-signature": f"t=1,v1={sig}"}, {}):
        try:
            verify_signature("stripe", "whsec_test", headers, body)
            raise AssertionError("accepted an invalid Stripe signature")
        except Rejected:
            pass


def test_input_validation_rejects_bad_payloads(client, db, org):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    auth = login(client, "eng@weakless.pro")
    h = hdr(auth, org)
    assert client.post("/api/v1/workflows", headers=h, json={"name": ""}).status_code == 422
    assert client.post("/api/v1/workflows", headers=h, json={"name": "x", "is_admin": True}).status_code == 422  # unknown fields rejected
    wf = client.post("/api/v1/workflows", headers=h, json={"name": "x"}).json()
    bad = {"definition": {"nodes": [{"id": "a b; DROP TABLE", "type": "trigger.manual"}], "edges": []}}
    assert client.put(f"/api/v1/workflows/{wf['id']}/draft", headers=h, json=bad).status_code == 422
    evil = {"definition": {"nodes": [{"id": "t", "type": "trigger.manual"}, {"id": "x", "type": "system.shell", "config": {"cmd": "id"}}],
                           "edges": [{"source": "t", "target": "x"}]}}
    assert client.put(f"/api/v1/workflows/{wf['id']}/draft", headers=h, json=evil).status_code == 200  # drafts may be incomplete...
    pub = client.post(f"/api/v1/workflows/{wf['id']}/publish", headers=h)  # ...but an unknown node type can never be published or run
    assert pub.status_code == 422 and any("Unknown node type" in i["message"] for i in pub.json()["error"]["details"])
    assert client.get("/api/v1/workflows/not-a-uuid", headers=h).status_code == 404
    assert client.get("/api/v1/workflows?limit=100000", headers=h).status_code == 422
