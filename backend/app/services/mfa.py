"""Two-factor sign-in: TOTP (RFC 6238) with one-time recovery codes.

* Secrets are 160-bit, stored as vault envelopes bound to the user (``aad = org:None|user:<id>|mfa``).
* Codes are 6 digits, 30-second steps, accepted one step either side for clock drift. The last
  accepted step is remembered, so a code (or an older one) can never be used twice.
* Recovery codes are 10 random codes of 10 characters, stored as SHA-256 hashes, each single use.
* Between the password and the code, the browser holds a short-lived signed *challenge token*,
  not a session. It is bound to the current password hash, so a password change voids it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import vault
from app.core.errors import Unauthorized, ValidationFailed
from app.db import utcnow
from app.models import SystemSetting, User

STEP_SECONDS = 30
DIGITS = 6
DRIFT_STEPS = 1
RECOVERY_CODES = 10
CHALLENGE_TTL = timedelta(minutes=5)
ISSUER = "Weakless"


# ----------------------------------------------------------------------------- TOTP


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))


def totp(secret: str, *, at: float | None = None, counter: int | None = None, digits: int = DIGITS) -> str:
    c = counter if counter is not None else int((time.time() if at is None else at) // STEP_SECONDS)
    digest = hmac.new(_key(secret), struct.pack(">Q", c), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def otpauth_url(secret: str, account: str) -> str:
    label = f"{quote(ISSUER)}:{quote(account)}"  # Key URI format: "Issuer:account" with a literal colon
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"


def _aad(user: User) -> str:
    return vault.aad_for(None, f"user:{user.id}|mfa")


def enabled(user: User) -> bool:
    return user.mfa_enabled_at is not None and bool(user.mfa_secret_envelope)


def required_for(db: Session, user: User) -> bool:
    """Staff must use two-factor sign-in while the platform setting says so."""
    if not user.platform_role:
        return False
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == "security.staff_mfa_required"))
    return bool(row.value) if row is not None and isinstance(row.value, bool) else get_settings().staff_mfa_required


def _normalize(code: str) -> str:
    return "".join(ch for ch in code if ch.isalnum()).lower()


def _hash_recovery(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode()).hexdigest()


def _new_recovery_codes() -> tuple[list[str], list[str]]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes (0/o, 1/l/i)
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes, [_hash_recovery(c) for c in codes]


def check_code(db: Session, user: User, code: str) -> str | None:
    """Return "totp" or "recovery" when the code is valid (and consume it), else None."""
    cleaned = _normalize(code)
    if not enabled(user) or not cleaned:
        return None
    if cleaned.isdigit() and len(cleaned) == DIGITS:
        secret = vault.decrypt(user.mfa_secret_envelope, _aad(user)).decode()
        now = int(time.time() // STEP_SECONDS)
        last = user.mfa_last_step if user.mfa_last_step is not None else -1
        for step in range(now - DRIFT_STEPS, now + DRIFT_STEPS + 1):
            if step > last and hmac.compare_digest(totp(secret, counter=step), cleaned):
                user.mfa_last_step = step
                db.flush()
                return "totp"
        return None
    hashes = list(user.mfa_recovery_hashes or [])
    h = _hash_recovery(cleaned)
    if any(hmac.compare_digest(h, x) for x in hashes):
        hashes.remove(h)
        user.mfa_recovery_hashes = hashes
        db.flush()
        return "recovery"
    return None


# ----------------------------------------------------------------------------- enrollment


def begin_enrollment(db: Session, user: User) -> dict:
    secret = new_secret()
    user.mfa_pending_envelope = vault.encrypt(secret, _aad(user))
    db.flush()
    return {"secret": secret, "otpauth_url": otpauth_url(secret, user.email)}


def confirm_enrollment(db: Session, user: User, code: str) -> list[str]:
    """Activate the pending secret once the person proves their app produces matching codes."""
    if not user.mfa_pending_envelope:
        raise ValidationFailed("Start the setup again: no pending authenticator was found")
    secret = vault.decrypt(user.mfa_pending_envelope, _aad(user)).decode()
    cleaned = _normalize(code)
    now = int(time.time() // STEP_SECONDS)
    match = next((s for s in range(now - DRIFT_STEPS, now + DRIFT_STEPS + 1) if hmac.compare_digest(totp(secret, counter=s), cleaned)), None)
    if match is None:
        raise ValidationFailed("That code does not match. Check the time on your phone and try the newest code.", code="invalid_mfa_code")
    codes, hashes = _new_recovery_codes()
    user.mfa_secret_envelope, user.mfa_pending_envelope = user.mfa_pending_envelope, None
    user.mfa_enabled_at, user.mfa_last_step, user.mfa_recovery_hashes = utcnow(), match, hashes
    db.flush()
    return codes


def regenerate_recovery_codes(db: Session, user: User) -> list[str]:
    codes, hashes = _new_recovery_codes()
    user.mfa_recovery_hashes = hashes
    db.flush()
    return codes


def disable(db: Session, user: User) -> None:
    user.mfa_secret_envelope = user.mfa_pending_envelope = None
    user.mfa_enabled_at = user.mfa_last_step = None
    user.mfa_recovery_hashes = None
    db.flush()


def status(db: Session, user: User) -> dict:
    return {"enabled": enabled(user), "required": required_for(db, user), "enabled_at": user.mfa_enabled_at.isoformat() if user.mfa_enabled_at else None,
            "recovery_codes_left": len(user.mfa_recovery_hashes or []) if enabled(user) else 0}


# ----------------------------------------------------------------------------- challenge tokens


def _password_fingerprint(user: User) -> str:
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def challenge_token(user: User, purpose: str) -> str:
    exp = datetime.now(timezone.utc) + CHALLENGE_TTL
    payload = {"sub": str(user.id), "typ": "mfa_challenge", "purpose": purpose, "pwf": _password_fingerprint(user), "exp": int(exp.timestamp())}
    return jwt.encode(payload, get_settings().secret_key, algorithm="HS256")


def user_from_challenge(db: Session, token: str, purpose: str) -> User:
    import uuid

    expired = Unauthorized("Your sign-in expired. Enter your email and password again.", code="mfa_challenge_expired")
    try:
        data = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
        uid = uuid.UUID(data["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise expired
    if data.get("typ") != "mfa_challenge" or data.get("purpose") != purpose:
        raise expired
    user = db.get(User, uid)
    if not user or not user.is_active or user.deleted_at or data.get("pwf") != _password_fingerprint(user):
        raise expired
    return user
