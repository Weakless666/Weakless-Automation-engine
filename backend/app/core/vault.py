"""Credential vault: envelope encryption with a key-management abstraction.

Each secret is encrypted with its own random 256-bit data key (AES-256-GCM).
The data key is wrapped by a key-encryption key (KEK) held by a ``KeyProvider``.
The default provider reads KEKs from ``WAE_MASTER_KEYS``; a cloud KMS can be
plugged in by implementing ``KeyProvider`` (wrap/unwrap) and calling
``set_key_provider``.

Rotation: add a new KEK to ``WAE_MASTER_KEYS``, set ``WAE_ACTIVE_KEY_ID`` to it
and run ``python -m app.cli rotate-keys``. Only the wrapped data keys are
re-encrypted (``rewrap``), so rotation never touches secret plaintext at rest.

The ``aad`` (additional authenticated data) binds a ciphertext to its owner
(organization + purpose): a ciphertext copied to another tenant's row fails to
decrypt.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings


class VaultError(Exception):
    pass


class KeyProvider(Protocol):
    def active_key_id(self) -> str: ...
    def wrap(self, key_id: str, data_key: bytes) -> bytes: ...
    def unwrap(self, key_id: str, wrapped: bytes) -> bytes: ...


class LocalKeyProvider:
    """KEKs supplied through configuration (environment / secret manager injection)."""

    def __init__(self, ring: dict[str, bytes], active: str):
        self._ring = ring
        self._active = active

    def active_key_id(self) -> str:
        return self._active

    def _kek(self, key_id: str) -> AESGCM:
        if key_id not in self._ring:
            raise VaultError(f"key '{key_id}' is not available in the key ring")
        return AESGCM(self._ring[key_id])

    def wrap(self, key_id: str, data_key: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._kek(key_id).encrypt(nonce, data_key, key_id.encode())

    def unwrap(self, key_id: str, wrapped: bytes) -> bytes:
        try:
            return self._kek(key_id).decrypt(wrapped[:12], wrapped[12:], key_id.encode())
        except InvalidTag as exc:
            raise VaultError("data key could not be unwrapped") from exc


_provider: KeyProvider | None = None


def get_key_provider() -> KeyProvider:
    global _provider
    if _provider is None:
        s = get_settings()
        _provider = LocalKeyProvider(s.key_ring(), s.active_key_id)
    return _provider


def set_key_provider(provider: KeyProvider | None) -> None:
    global _provider
    _provider = provider


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def encrypt(plaintext: str | bytes, aad: str) -> str:
    """Return a JSON envelope string."""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode()
    kp = get_key_provider()
    kid = kp.active_key_id()
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext, aad.encode())
    return json.dumps({"v": 1, "kid": kid, "wk": _b64(kp.wrap(kid, dek)), "n": _b64(nonce), "ct": _b64(ct)})


def decrypt(envelope: str, aad: str) -> bytes:
    try:
        env = json.loads(envelope)
        dek = get_key_provider().unwrap(env["kid"], base64.b64decode(env["wk"]))
        return AESGCM(dek).decrypt(base64.b64decode(env["n"]), base64.b64decode(env["ct"]), aad.encode())
    except (InvalidTag, KeyError, ValueError) as exc:
        raise VaultError("secret could not be decrypted") from exc


def encrypt_json(value: Any, aad: str) -> str:
    return encrypt(json.dumps(value), aad)


def decrypt_json(envelope: str, aad: str) -> Any:
    return json.loads(decrypt(envelope, aad))


def envelope_key_id(envelope: str) -> str:
    return json.loads(envelope)["kid"]


def rewrap(envelope: str) -> str:
    """Re-wrap the data key under the active KEK (key rotation)."""
    env = json.loads(envelope)
    kp = get_key_provider()
    active = kp.active_key_id()
    if env["kid"] == active:
        return envelope
    dek = kp.unwrap(env["kid"], base64.b64decode(env["wk"]))
    env["wk"] = _b64(kp.wrap(active, dek))
    env["kid"] = active
    return json.dumps(env)


def aad_for(organization_id: Any, purpose: str) -> str:
    return f"org:{organization_id}|{purpose}"
