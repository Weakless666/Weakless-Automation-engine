"""Two-factor sign-in."""
from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.models import SystemSetting, User
from app.services import mfa
from tests.conftest import ADMIN_PASSWORD, login, make_user


def test_totp_matches_rfc6238_reference_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890"
    assert mfa.totp(secret, at=59, digits=8) == "94287082"
    assert mfa.totp(secret, at=1111111109, digits=8) == "07081804"
    assert mfa.totp(secret, at=59) == "287082"


def code_for(db, email: str, offset_steps: int = 0) -> str:
    db.expire_all()
    user = db.scalar(select(User).where(User.email == email))
    from app.core import vault

    secret = vault.decrypt(user.mfa_secret_envelope, mfa._aad(user)).decode()
    return mfa.totp(secret, at=time.time() + offset_steps * mfa.STEP_SECONDS)


def enroll(client, db, email: str, password: str = ADMIN_PASSWORD) -> list[str]:
    auth = login(client, email, password)
    started = client.post("/api/v1/auth/mfa/enroll", headers=auth).json()
    assert started["otpauth_url"].startswith("otpauth://totp/Weakless:") and started["secret"] in started["otpauth_url"]
    wrong = client.post("/api/v1/auth/mfa/enroll/confirm", headers=auth, json={"code": "000000"})
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "invalid_mfa_code"
    ok = client.post("/api/v1/auth/mfa/enroll/confirm", headers=auth, json={"code": mfa.totp(started["secret"])})
    assert ok.status_code == 200, ok.text
    codes = ok.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    return codes


def post(client, path: str, body: dict, headers: dict | None = None):
    """POST, then drop any session cookie the response set, so later calls use the bearer token only."""
    r = client.post(path, json=body, headers=headers or {})
    client.cookies.clear()
    return r


def password_step(client, email: str, password: str = ADMIN_PASSWORD) -> dict:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json()


def test_enrolled_user_needs_a_code_and_each_code_works_once(client, db, org):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    enroll(client, db, "owner@acme.co")

    step = password_step(client, "owner@acme.co")
    assert step["mfa_required"] is True and "token" not in step  # no session after the password alone
    from app.core.security import decode_session_token

    assert decode_session_token(step["mfa_token"]) is None  # a challenge token is not a session token
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {step['mfa_token']}"}).status_code == 401

    bad = post(client, "/api/v1/auth/mfa/verify", {"mfa_token": step["mfa_token"], "code": "123456"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_mfa_code"
    code = code_for(db, "owner@acme.co", offset_steps=1)  # accepted with one step of clock drift
    ok = post(client, "/api/v1/auth/mfa/verify", {"mfa_token": step["mfa_token"], "code": code})
    assert ok.status_code == 200 and ok.json()["token"]
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {ok.json()['token']}"}).json()["mfa"]["enabled"] is True

    again = password_step(client, "owner@acme.co")
    replay = post(client, "/api/v1/auth/mfa/verify", {"mfa_token": again["mfa_token"], "code": code})
    assert replay.status_code == 401  # the same code cannot be used twice


def test_recovery_codes_are_single_use(client, db, org):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    codes = enroll(client, db, "owner@acme.co")
    step = password_step(client, "owner@acme.co")
    ok = post(client, "/api/v1/auth/mfa/verify", {"mfa_token": step["mfa_token"], "code": codes[0].upper()})
    assert ok.status_code == 200
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {ok.json()['token']}"}).json()
    assert me["mfa"]["recovery_codes_left"] == 9
    step = password_step(client, "owner@acme.co")
    assert post(client, "/api/v1/auth/mfa/verify", {"mfa_token": step["mfa_token"], "code": codes[0]}).status_code == 401
    db.expire_all()
    assert codes[1] not in str(db.scalar(select(User.mfa_recovery_hashes).where(User.email == "owner@acme.co")))  # only hashes are stored


def test_wrong_codes_lock_the_account(client, db, org):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    enroll(client, db, "owner@acme.co")
    step = password_step(client, "owner@acme.co")
    for _ in range(8):
        post(client, "/api/v1/auth/mfa/verify", {"mfa_token": step["mfa_token"], "code": "000000"})
    locked = post(client, "/api/v1/auth/mfa/verify", {"mfa_token": step["mfa_token"], "code": code_for(db, "owner@acme.co")})
    assert locked.status_code == 401 and locked.json()["error"]["code"] == "locked"
    # Re-entering the password does not reset the counter or the lock.
    assert client.post("/api/v1/auth/login", json={"email": "owner@acme.co", "password": ADMIN_PASSWORD}).status_code == 401


def test_challenge_tokens_expire_and_die_with_a_password_change(client, db, org, monkeypatch):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    enroll(client, db, "owner@acme.co")
    step = password_step(client, "owner@acme.co")
    user = db.scalar(select(User).where(User.email == "owner@acme.co"))
    from app.core.security import hash_password

    user.password_hash = hash_password("Changed-Passw0rd-1")
    db.commit()
    r = post(client, "/api/v1/auth/mfa/verify", {"mfa_token": step["mfa_token"], "code": code_for(db, "owner@acme.co")})
    assert r.status_code == 401 and r.json()["error"]["code"] == "mfa_challenge_expired"

    step = password_step(client, "owner@acme.co", "Changed-Passw0rd-1")
    monkeypatch.setattr(mfa, "CHALLENGE_TTL", mfa.CHALLENGE_TTL)  # no-op; expiry is enforced by the JWT itself
    from datetime import timedelta

    import jwt as pyjwt

    from app.config import get_settings

    data = pyjwt.decode(step["mfa_token"], get_settings().secret_key, algorithms=["HS256"])
    data["exp"] = int(time.time()) - 5
    expired = pyjwt.encode(data, get_settings().secret_key, algorithm="HS256")
    assert post(client, "/api/v1/auth/mfa/verify", {"mfa_token": expired, "code": code_for(db, "owner@acme.co")}).status_code == 401
    assert timedelta(minutes=5) == mfa.CHALLENGE_TTL


def _require_staff_mfa(db, on: bool = True):
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == "security.staff_mfa_required"))
    row.value = on
    db.commit()


def test_staff_must_set_up_two_factor_at_sign_in(client, db, org):
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    make_user(db, "member@acme.co", org=org, role="CLIENT_MEMBER")
    _require_staff_mfa(db)

    step = password_step(client, "eng@weakless.pro")
    assert step["mfa_setup_required"] is True and "token" not in step
    login(client, "member@acme.co")  # client users are not forced

    started = client.post("/api/v1/auth/mfa/setup", json={"mfa_token": step["mfa_token"]}).json()
    verify_token_rejected = post(client, "/api/v1/auth/mfa/verify", {"mfa_token": step["mfa_token"], "code": mfa.totp(started["secret"])})
    assert verify_token_rejected.status_code == 401  # a setup token cannot be used to skip setup
    done = post(client, "/api/v1/auth/mfa/setup/confirm", {"mfa_token": step["mfa_token"], "code": mfa.totp(started["secret"])})
    assert done.status_code == 200 and done.json()["token"] and len(done.json()["recovery_codes"]) == 10
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {done.json()['token']}"}).json()
    assert me["mfa"] == {**me["mfa"], "enabled": True, "required": True}

    # Staff under the requirement cannot switch it off.
    auth = {"Authorization": f"Bearer {done.json()['token']}"}
    r = client.post("/api/v1/auth/mfa/disable", headers=auth, json={"password": ADMIN_PASSWORD, "code": code_for(db, "eng@weakless.pro")})
    assert r.status_code == 403 and r.json()["error"]["code"] == "mfa_required"
    assert password_step(client, "eng@weakless.pro")["mfa_required"] is True


def test_staff_invitation_leads_to_two_factor_setup(client, db):
    make_user(db, "root@weakless.pro", platform_role="SUPER_ADMIN")
    root = login(client, "root@weakless.pro")
    _require_staff_mfa(db)
    invited = client.post("/api/v1/users", headers=root, json={"email": "new@weakless.pro", "full_name": "New", "platform_role": "SUPPORT"}).json()
    token = invited["invitation"]["url"].split("token=")[1]
    accepted = post(client, "/api/v1/auth/invitations/accept", {"token": token, "password": "Brand-New-Passw0rd"})
    assert accepted.status_code == 200 and accepted.json()["mfa_setup_required"] is True and "token" not in accepted.json()


def test_client_can_disable_with_password_and_code(client, db, org):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    first_codes = enroll(client, db, "owner@acme.co")
    step = password_step(client, "owner@acme.co")
    # The current step's code was consumed by setup, so the next one is used here.
    auth = {"Authorization": f"Bearer {post(client, '/api/v1/auth/mfa/verify', {'mfa_token': step['mfa_token'], 'code': code_for(db, 'owner@acme.co', 1)}).json()['token']}"}
    assert client.post("/api/v1/auth/mfa/disable", headers=auth, json={"password": "wrong", "code": "000000"}).status_code == 403
    regen = client.post("/api/v1/auth/mfa/recovery-codes", headers=auth, json={"code": first_codes[0]})
    assert regen.status_code == 200 and len(regen.json()["recovery_codes"]) == 10
    off = client.post("/api/v1/auth/mfa/disable", headers=auth, json={"password": ADMIN_PASSWORD, "code": regen.json()["recovery_codes"][0]})
    assert off.status_code == 200 and off.json()["enabled"] is False
    assert "token" in password_step(client, "owner@acme.co")  # back to password-only


def test_admin_reset_for_a_lost_phone_respects_roles(client, db, org):
    make_user(db, "root@weakless.pro", platform_role="SUPER_ADMIN")
    make_user(db, "staffadmin@weakless.pro", platform_role="ADMIN")
    target = make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    enroll(client, db, "owner@acme.co")
    enroll(client, db, "root@weakless.pro")
    root_id = db.scalar(select(User.id).where(User.email == "root@weakless.pro"))

    owner_step = password_step(client, "owner@acme.co")
    owner = {"Authorization": f"Bearer {post(client, '/api/v1/auth/mfa/verify', {'mfa_token': owner_step['mfa_token'], 'code': code_for(db, 'owner@acme.co', 1)}).json()['token']}"}
    assert client.post(f"/api/v1/users/{target.id}/mfa-reset", headers=owner).status_code == 403  # clients cannot

    staff = login(client, "staffadmin@weakless.pro")
    assert client.post(f"/api/v1/users/{root_id}/mfa-reset", headers=staff).status_code == 403  # nor a non-super admin for a super admin
    r = client.post(f"/api/v1/users/{target.id}/mfa-reset", headers=staff)
    assert r.status_code == 200 and r.json()["mfa_enabled"] is False
    assert client.get("/api/v1/auth/me", headers=owner).status_code == 401  # signed out everywhere
    assert "token" in password_step(client, "owner@acme.co")


def test_password_reset_does_not_bypass_two_factor(client, db, org):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    make_user(db, "staffadmin@weakless.pro", platform_role="ADMIN")
    target = db.scalar(select(User).where(User.email == "owner@acme.co"))
    enroll(client, db, "owner@acme.co")
    link = client.post(f"/api/v1/users/{target.id}/reset-link", headers=login(client, "staffadmin@weakless.pro")).json()["url"]
    client.post("/api/v1/auth/password-reset/confirm", json={"token": link.split("token=")[1], "new_password": "After-Reset-Passw0rd"})
    assert password_step(client, "owner@acme.co", "After-Reset-Passw0rd")["mfa_required"] is True


def test_the_secret_is_encrypted_and_never_returned_after_setup(client, db, org):
    make_user(db, "owner@acme.co", org=org, role="CLIENT_ADMIN")
    auth = login(client, "owner@acme.co")
    secret = client.post("/api/v1/auth/mfa/enroll", headers=auth).json()["secret"]
    client.post("/api/v1/auth/mfa/enroll/confirm", headers=auth, json={"code": mfa.totp(secret)})
    db.expire_all()
    row = db.scalar(select(User).where(User.email == "owner@acme.co"))
    assert secret not in row.mfa_secret_envelope
    step = password_step(client, "owner@acme.co")
    verified = post(client, "/api/v1/auth/mfa/verify", {"mfa_token": step["mfa_token"], "code": code_for(db, "owner@acme.co", 1)}).json()
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {verified['token']}"})
    assert secret not in me.text and secret not in client.get("/api/v1/auth/mfa", headers={"Authorization": f"Bearer {verified['token']}"}).text


@pytest.mark.parametrize("on", [True, False])
def test_staff_requirement_is_an_editable_platform_setting(client, db, on):
    make_user(db, "root@weakless.pro", platform_role="SUPER_ADMIN")
    root = login(client, "root@weakless.pro")
    assert client.put("/api/v1/settings/security.staff_mfa_required", headers=root, json={"value": on}).status_code == 200
    make_user(db, "sup@weakless.pro", platform_role="SUPPORT")
    assert bool(password_step(client, "sup@weakless.pro").get("mfa_setup_required")) is on
