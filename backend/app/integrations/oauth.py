"""OAuth 2.0 authorization-code flow (with PKCE where supported) and token refresh."""
from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import http, vault
from app.core.errors import AppError, NodeError
from app.core.security import random_token, sha256_hex
from app.db import utcnow
from app.integrations.providers import ProviderDef
from app.models import Integration, OAuthState

STATE_TTL_MINUTES = 15


def redirect_uri() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/v1/integrations/oauth/callback"


def _client(p: ProviderDef) -> tuple[str, str]:
    assert p.oauth is not None
    creds = get_settings().oauth_client(p.oauth.group)
    if not creds:
        raise AppError(
            f"OAuth is not configured for {p.name}. Set WAE_OAUTH_{p.oauth.group.upper()}_CLIENT_ID and "
            f"WAE_OAUTH_{p.oauth.group.upper()}_CLIENT_SECRET.", code="awaiting_credentials", status_code=409)
    return creds


def authorization_url(db: Session, integration: Integration, p: ProviderDef, user_id: uuid.UUID) -> str:
    assert p.oauth is not None
    client_id, _ = _client(p)
    state = random_token(32)
    row = OAuthState(id=uuid.uuid4(), organization_id=integration.organization_id, integration_id=integration.id, user_id=user_id,
                     state_hash=sha256_hex(state), expires_at=utcnow() + timedelta(minutes=STATE_TTL_MINUTES))
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri(),
              "scope": " ".join(p.oauth.scopes), "state": state, **p.oauth.extra_params}
    if p.oauth.pkce:
        verifier = random_token(48)
        row.verifier_envelope = vault.encrypt(verifier, vault.aad_for(integration.organization_id, f"oauth_state:{row.id}"))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params.update({"code_challenge": challenge, "code_challenge_method": "S256"})
    db.add(row)
    db.flush()
    return f"{p.oauth.authorize_url}?{urlencode(params)}"


def _store_tokens(db: Session, integration: Integration, tokens: dict, previous: dict | None = None) -> dict:
    from app.integrations.service import store_secret

    secret = dict(previous or {})
    secret["access_token"] = tokens["access_token"]
    if tokens.get("refresh_token"):
        secret["refresh_token"] = tokens["refresh_token"]
    if tokens.get("instance_url"):
        secret["instance_url"] = tokens["instance_url"]
    store_secret(db, integration, secret)
    expires_in = tokens.get("expires_in")
    integration.token_expires_at = utcnow() + timedelta(seconds=int(expires_in)) if expires_in else None
    if tokens.get("scope"):
        integration.scopes = str(tokens["scope"]).replace(",", " ").split()
    db.flush()
    return secret


def _token_request(p: ProviderDef, data: dict) -> dict:
    assert p.oauth is not None
    resp = http.request("POST", p.oauth.token_url, data=data, headers={"Accept": "application/json"}, timeout=20)
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error_description") or resp.json().get("error")
        except ValueError:
            err = None
        raise NodeError(f"{p.name} rejected the token request: {err or resp.status_code}", code="oauth_error",
                        retryable=resp.status_code >= 500)
    tokens = resp.json()
    if "access_token" not in tokens:
        raise NodeError(f"{p.name} returned no access token", code="oauth_error")
    return tokens


def complete_authorization(db: Session, state: str, code: str) -> Integration:
    """Exchange the code. ``db`` must be able to read ``oauth_states`` across tenants
    (the callback has no session); the row itself binds the tenant."""
    from app.integrations.service import provider_def, test_integration

    row = db.scalar(select(OAuthState).where(OAuthState.state_hash == sha256_hex(state)))
    if not row or row.used_at or row.expires_at <= utcnow():
        raise AppError("OAuth state is invalid or expired", code="oauth_state_invalid")
    row.used_at = utcnow()  # single use, even if the exchange fails
    db.flush()
    integration = db.get(Integration, row.integration_id)
    if not integration or integration.organization_id != row.organization_id or integration.deleted_at:
        raise AppError("Integration no longer exists", code="oauth_state_invalid")
    p = provider_def(integration.provider_key)
    client_id, client_secret = _client(p)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
            "client_id": client_id, "client_secret": client_secret}
    if row.verifier_envelope:
        data["code_verifier"] = vault.decrypt(row.verifier_envelope, vault.aad_for(row.organization_id, f"oauth_state:{row.id}")).decode()
    try:
        tokens = _token_request(p, data)
    except NodeError as exc:
        integration.status, integration.last_error = "error", exc.message[:500]
        db.flush()
        raise AppError(exc.message, code="oauth_error") from exc
    _store_tokens(db, integration, tokens)
    test_integration(db, integration)
    return integration


def refresh_tokens(db: Session, integration: Integration, p: ProviderDef, secret: dict) -> dict:
    client_id, client_secret = _client(p)
    tokens = _token_request(p, {"grant_type": "refresh_token", "refresh_token": secret["refresh_token"],
                                "client_id": client_id, "client_secret": client_secret})
    return _store_tokens(db, integration, tokens, previous=secret)
