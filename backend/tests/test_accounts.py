"""Password reset and invitations."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.db import utcnow
from app.models import OrganizationMember, User, UserToken
from tests.conftest import ADMIN_PASSWORD, login, make_user

NEW_PASSWORD = "Brand-New-Passw0rd"


def token_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["token"][0]


def hdr(auth: dict, org) -> dict:
    return {**auth, "X-Organization-Id": str(org.id)}


@pytest.fixture
def outbox(monkeypatch):
    """Pretend platform SMTP is configured and capture what would be sent."""
    from app.services import notifications

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(notifications, "smtp_configured", lambda: True)
    monkeypatch.setattr(notifications, "send_email", lambda to, subject, body, html=None: sent.append((to, subject, body)))
    return sent


def link_in(body: str) -> str:
    return next(word for word in body.split() if word.startswith("http"))


# ----------------------------------------------------------------------------- password reset


def test_password_reset_end_to_end(client, db, org, outbox):
    make_user(db, "member@acme.co", org=org, role="CLIENT_MEMBER")
    old = login(client, "member@acme.co")

    known = client.post("/api/v1/auth/password-reset/request", json={"email": "member@acme.co"})
    unknown = client.post("/api/v1/auth/password-reset/request", json={"email": "nobody@acme.co"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()  # no way to tell whether the account exists
    assert [m[0] for m in outbox] == ["member@acme.co"]
    url = link_in(outbox[0][2])
    assert urlsplit(url).path == "/reset-password"
    raw = token_of(url)
    assert db.scalar(select(UserToken.token_hash)) != raw  # only a hash is stored

    weak = client.post("/api/v1/auth/password-reset/confirm", json={"token": raw, "new_password": "short"})
    assert weak.status_code == 422
    assert client.post("/api/v1/auth/password-reset/confirm", json={"token": raw, "new_password": NEW_PASSWORD}).status_code == 200

    assert client.get("/api/v1/auth/me", headers=old).status_code == 401  # existing sessions are revoked
    assert client.post("/api/v1/auth/login", json={"email": "member@acme.co", "password": ADMIN_PASSWORD}).status_code == 401
    login(client, "member@acme.co", NEW_PASSWORD)
    again = client.post("/api/v1/auth/password-reset/confirm", json={"token": raw, "new_password": "Another-Passw0rd"})
    assert again.status_code == 400 and again.json()["error"]["code"] == "invalid_token"  # single use


def test_reset_links_expire_and_supersede(client, db, outbox):
    make_user(db, "staff@weakless.pro", platform_role="SUPPORT")
    client.post("/api/v1/auth/password-reset/request", json={"email": "staff@weakless.pro"})
    client.post("/api/v1/auth/password-reset/request", json={"email": "staff@weakless.pro"})
    first, second = (token_of(link_in(m[2])) for m in outbox)
    body = lambda t: {"token": t, "new_password": NEW_PASSWORD}  # noqa: E731
    assert client.post("/api/v1/auth/password-reset/confirm", json=body(first)).status_code == 400  # replaced by the newer link

    row = db.scalar(select(UserToken).where(UserToken.used_at.is_(None)))
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert client.post("/api/v1/auth/password-reset/confirm", json=body(second)).status_code == 400  # expired


def test_reset_clears_lockout(client, db, outbox):
    make_user(db, "locked@weakless.pro", platform_role="SUPPORT")
    for _ in range(8):
        client.post("/api/v1/auth/login", json={"email": "locked@weakless.pro", "password": "wrong"})
    assert client.post("/api/v1/auth/login", json={"email": "locked@weakless.pro", "password": ADMIN_PASSWORD}).status_code == 401
    client.post("/api/v1/auth/password-reset/request", json={"email": "locked@weakless.pro"})
    client.post("/api/v1/auth/password-reset/confirm", json={"token": token_of(link_in(outbox[0][2])), "new_password": NEW_PASSWORD})
    login(client, "locked@weakless.pro", NEW_PASSWORD)


def test_staff_issued_reset_links_respect_roles(client, db, org):
    make_user(db, "root@weakless.pro", platform_role="SUPER_ADMIN")
    make_user(db, "staffadmin@weakless.pro", platform_role="ADMIN")
    target = make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    root_user = db.scalar(select(User).where(User.email == "root@weakless.pro"))

    owner = login(client, "owner@acme.co")
    assert client.post(f"/api/v1/users/{target.id}/reset-link", headers=owner).status_code == 403  # client admins cannot take over accounts
    staff = login(client, "staffadmin@weakless.pro")
    assert client.post(f"/api/v1/users/{root_user.id}/reset-link", headers=staff).status_code == 403  # nor can a non-super admin for a super admin
    issued = client.post(f"/api/v1/users/{target.id}/reset-link", headers=staff)
    assert issued.status_code == 200
    assert client.post("/api/v1/auth/password-reset/confirm", json={"token": token_of(issued.json()["url"]), "new_password": NEW_PASSWORD}).status_code == 200
    login(client, "owner@acme.co", NEW_PASSWORD)


# ----------------------------------------------------------------------------- invitations


def test_member_invitation_end_to_end(client, db, org, outbox):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    owner = login(client, "owner@acme.co")
    r = client.post("/api/v1/members", headers=hdr(owner, org), json={"email": "new.hire@acme.co", "full_name": "New Hire", "role": "CLIENT_MEMBER"})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "invited" and r.json()["invitation"]["email_sent"] is True
    assert outbox[0][0] == "new.hire@acme.co" and "Acme Hotel" in outbox[0][1]
    raw = token_of(r.json()["invitation"]["url"])
    assert link_in(outbox[0][2]) == r.json()["invitation"]["url"]

    pending = client.post("/api/v1/auth/login", json={"email": "new.hire@acme.co", "password": "anything"})
    wrong = client.post("/api/v1/auth/login", json={"email": "owner@acme.co", "password": "anything"})
    assert pending.status_code == 401 and pending.json()["error"]["message"] == wrong.json()["error"]["message"]

    info = client.post("/api/v1/auth/invitations/inspect", json={"token": raw}).json()
    assert info["organization_name"] == "Acme Hotel" and info["needs_password"] is True and info["role"] == "CLIENT_MEMBER"

    assert client.post("/api/v1/auth/invitations/accept", json={"token": raw, "password": "weak"}).status_code == 422
    accepted = client.post("/api/v1/auth/invitations/accept", json={"token": raw, "password": NEW_PASSWORD, "full_name": "Nora Hire"})
    assert accepted.status_code == 200
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {accepted.json()['token']}"}).json()
    assert me["user"]["full_name"] == "Nora Hire" and [o["name"] for o in me["organizations"]] == ["Acme Hotel"]
    assert client.post("/api/v1/auth/invitations/accept", json={"token": raw, "password": NEW_PASSWORD}).status_code == 400
    login(client, "new.hire@acme.co", NEW_PASSWORD)


def test_invitation_works_without_email_and_reports_it(client, db, org):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    r = client.post("/api/v1/members", headers=hdr(login(client, "owner@acme.co"), org), json={"email": "x@acme.co", "role": "VIEWER"})
    assert r.json()["invitation"]["email_sent"] is False and r.json()["invitation"]["url"].startswith("http://localhost:3000/accept-invite?token=")


def test_existing_account_joins_directly(client, db, org, other_org):
    make_user(db, "eng@weakless.pro", platform_role="ADMIN")
    make_user(db, "consultant@partner.co", org=other_org, role="CLIENT_MEMBER")
    r = client.post("/api/v1/members", headers=hdr(login(client, "eng@weakless.pro"), org), json={"email": "consultant@partner.co", "role": "VIEWER"})
    assert r.json()["status"] == "active" and r.json()["invitation"] is None


def test_invitation_cannot_overwrite_an_existing_password(client, db, org, other_org):
    """Two organizations invite the same new person. Accepting one sets the password and joins only that
    organization; the other invitation then requires the password - it can never be used to reset it."""
    make_user(db, "eng@weakless.pro", platform_role="ADMIN")
    eng = login(client, "eng@weakless.pro")
    a = client.post("/api/v1/members", headers=hdr(eng, org), json={"email": "shared@person.co", "role": "CLIENT_MEMBER"}).json()
    b = client.post("/api/v1/members", headers=hdr(eng, other_org), json={"email": "shared@person.co", "role": "CLIENT_MEMBER"}).json()
    assert a["status"] == b["status"] == "invited"
    accepted = client.post("/api/v1/auth/invitations/accept", json={"token": token_of(a["invitation"]["url"]), "password": NEW_PASSWORD}).json()
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {accepted['token']}"}).json()
    assert [o["name"] for o in me["organizations"]] == ["Acme Hotel"]  # only the inviting organization

    tb = token_of(b["invitation"]["url"])
    assert client.post("/api/v1/auth/invitations/inspect", json={"token": tb}).json()["needs_password"] is False
    hijack = client.post("/api/v1/auth/invitations/accept", json={"token": tb, "password": "Attacker-Passw0rd"})
    assert hijack.status_code == 403
    login(client, "shared@person.co", NEW_PASSWORD)  # password unchanged
    ok = client.post("/api/v1/auth/invitations/accept", json={"token": tb, "password": NEW_PASSWORD})
    assert ok.status_code == 200
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {ok.json()['token']}"}).json()
    assert sorted(o["name"] for o in me["organizations"]) == ["Acme Hotel", "Rival Salon"]


def test_resend_disable_and_remove_invalidate_links(client, db, org):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    h = hdr(login(client, "owner@acme.co"), org)
    m = client.post("/api/v1/members", headers=h, json={"email": "p@acme.co", "role": "VIEWER"}).json()
    first = token_of(m["invitation"]["url"])
    second = token_of(client.post(f"/api/v1/members/{m['id']}/invitation", headers=h).json()["url"])
    inspect = lambda t: client.post("/api/v1/auth/invitations/inspect", json={"token": t}).status_code  # noqa: E731
    assert (inspect(first), inspect(second)) == (400, 200)

    activate = client.patch(f"/api/v1/members/{m['id']}", headers=h, json={"status": "active"})
    assert activate.status_code == 409 and activate.json()["error"]["code"] == "invitation_pending"  # cannot skip acceptance
    client.patch(f"/api/v1/members/{m['id']}", headers=h, json={"status": "disabled"})
    assert inspect(second) == 400

    m2 = client.post("/api/v1/members", headers=h, json={"email": "q@acme.co", "role": "VIEWER"}).json()
    client.delete(f"/api/v1/members/{m2['id']}", headers=h)
    assert inspect(token_of(m2["invitation"]["url"])) == 400
    assert db.scalar(select(OrganizationMember).where(OrganizationMember.id == __import__("uuid").UUID(m2["id"]))) is None


def test_staff_invitation(client, db):
    make_user(db, "root@weakless.pro", platform_role="SUPER_ADMIN")
    root = login(client, "root@weakless.pro")
    r = client.post("/api/v1/users", headers=root, json={"email": "new.eng@weakless.pro", "full_name": "New Engineer", "platform_role": "AUTOMATION_ENGINEER"})
    assert r.status_code == 201 and r.json()["invitation_pending"] is True and r.json()["invitation"]["url"]
    resent = client.post(f"/api/v1/users/{r.json()['id']}/invitation", headers=root).json()
    info = client.post("/api/v1/auth/invitations/inspect", json={"token": token_of(resent["url"])}).json()
    assert info["organization_name"] is None and info["role"] == "AUTOMATION_ENGINEER"
    client.post("/api/v1/auth/invitations/accept", json={"token": token_of(resent["url"]), "password": NEW_PASSWORD})
    me = client.get("/api/v1/auth/me", headers=login(client, "new.eng@weakless.pro", NEW_PASSWORD)).json()
    assert me["is_staff"] is True
    assert client.post(f"/api/v1/users/{r.json()['id']}/invitation", headers=root).status_code == 409  # already accepted


def test_tokens_cannot_cross_purposes(client, db, org):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    m = client.post("/api/v1/members", headers=hdr(login(client, "owner@acme.co"), org), json={"email": "z@acme.co", "role": "VIEWER"}).json()
    invite_token = token_of(m["invitation"]["url"])
    r = client.post("/api/v1/auth/password-reset/confirm", json={"token": invite_token, "new_password": NEW_PASSWORD})
    assert r.status_code == 400  # an invitation link is not a reset link
