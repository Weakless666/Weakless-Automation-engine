"""Environment-based configuration.

All settings are read from environment variables prefixed with ``WAE_`` (or a
``.env`` file in the backend directory). Nothing secret has a usable default in
production: the app refuses to start without ``WAE_SECRET_KEY`` and
``WAE_MASTER_KEYS`` unless ``WAE_ENV`` is ``development`` or ``test``.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEV_SECRETS = BACKEND_DIR / ".dev-secrets.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WAE_", env_file=str(BACKEND_DIR / ".env"), extra="ignore")

    env: str = "development"  # development | test | production
    database_url: str = f"sqlite:///{(BACKEND_DIR / 'wae.db').as_posix()}"
    secret_key: str = ""  # signs session JWTs and OAuth state
    # "kid:base64(32 bytes),kid2:base64(32 bytes)" - key-encryption keys for the vault
    master_keys: str = ""
    active_key_id: str = ""  # defaults to the first key in master_keys

    public_base_url: str = "http://localhost:8000"  # externally reachable API URL (webhooks, OAuth callbacks)
    frontend_url: str = "http://localhost:3000"
    cors_origins: str = "http://localhost:3000"
    cookie_secure: bool = False
    session_hours: int = 12

    embedded_worker: bool = True  # run the queue worker inside the API process (dev / single-node)
    worker_poll_seconds: float = 1.0
    job_visibility_timeout_seconds: int = 300
    job_max_attempts: int = 3
    default_execution_timeout_seconds: int = 900
    default_org_concurrency: int = 5

    allow_private_network: bool = False  # SSRF guard: allow workflow HTTP calls to private IPs
    rate_limit_per_minute: int = 300
    auth_rate_limit_per_minute: int = 20
    rate_limit_store: Literal["database", "memory"] = "database"  # where sign-in, code and hook limits are kept (shared across instances)

    staff_mfa_required: bool = True  # initial value of the security.staff_mfa_required platform setting
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    # Platform-level AI keys (optional fallback when an organization has no AI integration)
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # Platform email (notifications)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    # Billing
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    sentry_dsn: str = ""
    log_level: str = "INFO"

    oauth_clients: dict[str, dict[str, str]] = Field(default_factory=dict)

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def key_ring(self) -> dict[str, bytes]:
        ring: dict[str, bytes] = {}
        for part in self.master_keys.split(","):
            part = part.strip()
            if not part:
                continue
            kid, _, b64 = part.partition(":")
            key = base64.b64decode(b64)
            if len(key) != 32:
                raise ValueError(f"master key '{kid}' must be 32 bytes (base64 encoded)")
            ring[kid] = key
        return ring

    def oauth_client(self, group: str) -> tuple[str, str] | None:
        """OAuth client credentials for a provider group, from
        ``WAE_OAUTH_<GROUP>_CLIENT_ID`` / ``WAE_OAUTH_<GROUP>_CLIENT_SECRET``."""
        cid = os.environ.get(f"WAE_OAUTH_{group.upper()}_CLIENT_ID", "")
        sec = os.environ.get(f"WAE_OAUTH_{group.upper()}_CLIENT_SECRET", "")
        if not cid:
            entry = self.oauth_clients.get(group.lower())
            if entry:
                cid, sec = entry.get("client_id", ""), entry.get("client_secret", "")
        return (cid, sec) if cid and sec else None


def _dev_secrets() -> dict[str, str]:
    """Generate and persist local development secrets so encrypted data survives restarts."""
    if _DEV_SECRETS.exists():
        return json.loads(_DEV_SECRETS.read_text())
    data = {
        "secret_key": secrets.token_urlsafe(48),
        "master_keys": "dev1:" + base64.b64encode(secrets.token_bytes(32)).decode(),
    }
    _DEV_SECRETS.write_text(json.dumps(data))
    return data


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.secret_key or not s.master_keys:
        if s.env == "production":
            raise RuntimeError("WAE_SECRET_KEY and WAE_MASTER_KEYS must be set in production")
        if s.env == "test":
            s.secret_key = s.secret_key or "test-secret-key-not-for-production-use-0123456789"
            s.master_keys = s.master_keys or "test1:" + base64.b64encode(b"\x01" * 32).decode()
        else:
            dev = _dev_secrets()
            s.secret_key = s.secret_key or dev["secret_key"]
            s.master_keys = s.master_keys or dev["master_keys"]
    if not s.active_key_id:
        s.active_key_id = next(iter(s.key_ring()))
    if s.active_key_id not in s.key_ring():
        raise RuntimeError("WAE_ACTIVE_KEY_ID is not present in WAE_MASTER_KEYS")
    return s
