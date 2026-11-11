"""Integration lifecycle: create, store credentials, test, use, revoke."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import http, vault
from app.core.errors import AppError, NodeError, NotFound, ValidationFailed
from app.db import utcnow
from app.integrations.providers import PROVIDERS, Conn, ProviderDef
from app.models import Integration, IntegrationCredential, IntegrationProvider

log = logging.getLogger("wae.integrations")


def sync_catalog(db: Session) -> None:
    """Mirror the code-defined catalog into ``integration_providers``."""
    rows = {r.key: r for r in db.scalars(select(IntegrationProvider))}
    for key, p in PROVIDERS.items():
        desc = p.describe()
        row = rows.get(key)
        if not row:
            row = IntegrationProvider(key=key, name=p.name, category=p.category, auth_type=p.auth_type, config=desc)
            db.add(row)
        else:
            row.name, row.category, row.auth_type, row.config = p.name, p.category, p.auth_type, desc
    db.flush()


def provider_def(key: str) -> ProviderDef:
    p = PROVIDERS.get(key)
    if not p:
        raise NotFound(f"Unknown integration provider '{key}'")
    return p


def oauth_configured(p: ProviderDef) -> bool:
    return bool(p.oauth and get_settings().oauth_client(p.oauth.group))


def _aad(integration: Integration) -> str:
    return vault.aad_for(integration.organization_id, f"integration:{integration.id}")


def _credential(db: Session, integration: Integration) -> IntegrationCredential | None:
    return db.scalar(select(IntegrationCredential).where(
        IntegrationCredential.integration_id == integration.id, IntegrationCredential.kind == "primary"))


def load_secret(db: Session, integration: Integration) -> dict:
    cred = _credential(db, integration)
    return vault.decrypt_json(cred.envelope, _aad(integration)) if cred else {}


def store_secret(db: Session, integration: Integration, secret: dict) -> None:
    envelope = vault.encrypt_json(secret, _aad(integration))
    cred = _credential(db, integration)
    if cred:
        cred.envelope, cred.key_id, cred.rotated_at = envelope, vault.envelope_key_id(envelope), utcnow()
    else:
        db.add(IntegrationCredential(organization_id=integration.organization_id, integration_id=integration.id,
                                     kind="primary", envelope=envelope, key_id=vault.envelope_key_id(envelope)))
    db.flush()


def split_fields(p: ProviderDef, values: dict, *, partial: bool = False) -> tuple[dict, dict]:
    """Split submitted values into (non-secret config, secrets) and validate required fields."""
    known = {f.name: f for f in p.fields}
    unknown = set(values) - set(known)
    if unknown:
        raise ValidationFailed(f"Unknown field(s): {', '.join(sorted(unknown))}")
    config, secret, missing = {}, {}, []
    for f in p.fields:
        v = values.get(f.name)
        if v in (None, ""):
            if f.required and not partial:
                missing.append(f.label)
            continue
        if not isinstance(v, (str, int, float)):
            raise ValidationFailed(f"Field '{f.label}' must be a string")
        (secret if f.secret else config)[f.name] = str(v).strip()
    if missing and p.auth_type != "oauth2":
        raise ValidationFailed(f"Missing required field(s): {', '.join(missing)}")
    return config, secret


def create_integration(db: Session, organization_id: uuid.UUID, provider_key: str, name: str, values: dict,
                       user_id: uuid.UUID | None) -> Integration:
    from app.services.usage import check_quota

    p = provider_def(provider_key)
    check_quota(db, organization_id, "integrations")
    config, secret = split_fields(p, values)
    integration = Integration(id=uuid.uuid4(), organization_id=organization_id, provider_key=provider_key,
                              name=name.strip() or p.name, config=config, created_by=user_id,
                              scopes=list(p.oauth.scopes) if p.oauth else [])
    if p.auth_type == "oauth2":
        # No token yet. If the platform OAuth app is missing we say so instead of pretending.
        integration.status = "pending" if oauth_configured(p) else "awaiting_credentials"
        if not oauth_configured(p):
            integration.last_error = f"Platform OAuth client for '{p.oauth.group}' is not configured"
    else:
        integration.status = "pending"
    db.add(integration)
    db.flush()
    if secret:
        store_secret(db, integration, secret)
    if p.auth_type != "oauth2":
        test_integration(db, integration)
    return integration


def update_integration(db: Session, integration: Integration, name: str | None, values: dict | None) -> Integration:
    p = provider_def(integration.provider_key)
    if name:
        integration.name = name.strip()
    if values:
        config, secret = split_fields(p, values, partial=True)
        integration.config = {**(integration.config or {}), **config}
        if secret:
            store_secret(db, integration, {**load_secret(db, integration), **secret})
        if p.auth_type != "oauth2" or integration.status in ("connected", "error"):
            test_integration(db, integration)
    db.flush()
    return integration


def connection(db: Session, integration: Integration) -> Conn:
    """Decrypt credentials (refreshing OAuth tokens when needed)."""
    p = provider_def(integration.provider_key)
    secret = load_secret(db, integration)
    if p.oauth and secret.get("refresh_token") and integration.token_expires_at:
        if integration.token_expires_at <= utcnow() + timedelta(seconds=90):
            from app.integrations.oauth import refresh_tokens

            secret = refresh_tokens(db, integration, p, secret)
    return Conn(provider_key=integration.provider_key, config=dict(integration.config or {}), secret=secret)


def test_integration(db: Session, integration: Integration) -> dict:
    """Call the provider and record health. Never raises for provider failures."""
    p = provider_def(integration.provider_key)
    was_connected = integration.status == "connected"
    integration.last_checked_at = utcnow()
    result: dict = {}
    try:
        conn = connection(db, integration)
        if p.auth_type == "oauth2" and not conn.secret.get("access_token"):
            raise NodeError("Authorization has not been completed", code="not_authorized")
        result = p.adapter.test(conn)
        integration.status, integration.last_error, integration.last_ok_at = "connected", None, utcnow()
    except (NodeError, AppError) as exc:
        message = exc.message
        if p.auth_type == "oauth2" and not oauth_configured(p) and not load_secret(db, integration).get("access_token"):
            integration.status = "awaiting_credentials"
        elif getattr(exc, "code", "") == "not_authorized":
            integration.status = "pending"
        else:
            integration.status = "error"
        integration.last_error = message[:500]
        result = {"error": message}
    except vault.VaultError:
        integration.status, integration.last_error = "error", "Stored credentials could not be decrypted"
        result = {"error": integration.last_error}
    except Exception as exc:  # noqa: BLE001
        log.exception("integration test crashed for %s", integration.id)
        integration.status, integration.last_error = "error", f"Unexpected error: {type(exc).__name__}"
        result = {"error": integration.last_error}
    db.flush()
    if was_connected and integration.status == "error":
        from app.services.notifications import notify

        notify(db, "integration.disconnected", integration.organization_id,
               title=f"Integration disconnected: {integration.name}", body=integration.last_error or "",
               severity="error", data={"integration_id": str(integration.id), "provider": integration.provider_key})
    return {"status": integration.status, **result}


def get_integration(db: Session, organization_id: uuid.UUID, integration_id) -> Integration:
    try:
        iid = integration_id if isinstance(integration_id, uuid.UUID) else uuid.UUID(str(integration_id))
    except ValueError:
        raise NodeError("Integration id is not valid", code="invalid_config")
    integration = db.get(Integration, iid)
    # Explicit tenant check: never rely on RLS alone.
    if not integration or integration.organization_id != organization_id or integration.deleted_at:
        raise NodeError("Integration not found in this organization", code="integration_not_found")
    return integration


def run_action(db: Session, organization_id: uuid.UUID, integration_id, action: str, params: dict,
               *, secrets_out: set[str] | None = None) -> dict:
    """Execute a provider action on behalf of a workflow node or agent tool."""
    integration = get_integration(db, organization_id, integration_id)
    p = provider_def(integration.provider_key)
    if integration.status != "connected":
        raise NodeError(f"Integration '{integration.name}' is not connected (status: {integration.status})",
                        code="integration_not_connected")
    fn = p.adapter.actions().get(action)
    if not fn:
        raise NodeError(f"{p.name} does not support the action '{action}'", code="action_not_supported")
    conn = connection(db, integration)
    if secrets_out is not None:
        secrets_out.update(conn.secrets())
    try:
        return fn(conn, params)
    except NodeError as exc:
        if exc.code in ("http_401", "http_403", "auth_failed"):
            integration.status, integration.last_error, integration.last_checked_at = "error", exc.message[:500], utcnow()
            db.flush()
        raise


def revoke_integration(db: Session, integration: Integration) -> None:
    """Delete stored credentials and best-effort revoke the provider grant."""
    p = provider_def(integration.provider_key)
    try:
        secret = load_secret(db, integration)
        token = secret.get("refresh_token") or secret.get("access_token")
        if p.oauth and p.oauth.revoke_url and token:
            http.request("POST", p.oauth.revoke_url, data={"token": token}, timeout=10)
    except Exception:  # noqa: BLE001 - revocation at the provider is best effort
        log.warning("provider-side revocation failed for integration %s", integration.id)
    cred = _credential(db, integration)
    if cred:
        db.delete(cred)
    integration.status, integration.last_error, integration.token_expires_at = "revoked", None, None
    db.flush()


def public_view(db: Session, integration: Integration) -> dict:
    """API representation. Secrets are never included - only whether they exist."""
    p = PROVIDERS.get(integration.provider_key)
    return {
        "id": str(integration.id),
        "provider_key": integration.provider_key,
        "provider_name": p.name if p else integration.provider_key,
        "category": p.category if p else "",
        "auth_type": p.auth_type if p else "",
        "name": integration.name,
        "status": integration.status,
        "config": integration.config or {},
        "scopes": integration.scopes or [],
        "has_credentials": _credential(db, integration) is not None,
        "oauth_ready": oauth_configured(p) if p and p.oauth else None,
        "last_checked_at": _iso(integration.last_checked_at),
        "last_ok_at": _iso(integration.last_ok_at),
        "last_error": integration.last_error,
        "token_expires_at": _iso(integration.token_expires_at),
        "created_at": _iso(integration.created_at),
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None
