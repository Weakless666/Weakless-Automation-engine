"""Password hashing, session tokens and small crypto helpers."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

_hasher = PasswordHasher()
JWT_ALG = "HS256"
SESSION_COOKIE = "wae_session"
CSRF_HEADER = "x-wae-csrf"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def validate_password_strength(password: str) -> str | None:
    if len(password) < 10:
        return "Password must be at least 10 characters long"
    if password.lower() == password or not any(c.isdigit() for c in password):
        return "Password must contain an uppercase letter and a digit"
    return None


def create_session_token(user_id: uuid.UUID, session_id: uuid.UUID, expires_at: datetime) -> str:
    payload = {"sub": str(user_id), "sid": str(session_id), "exp": int(expires_at.timestamp()), "typ": "session"}
    return jwt.encode(payload, get_settings().secret_key, algorithm=JWT_ALG)


def decode_session_token(token: str) -> dict | None:
    try:
        data = jwt.decode(token, get_settings().secret_key, algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        return None
    return data if data.get("typ") == "session" else None


def session_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=get_settings().session_hours)


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def hmac_sha256_hex(secret: str | bytes, message: bytes) -> str:
    if isinstance(secret, str):
        secret = secret.encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
