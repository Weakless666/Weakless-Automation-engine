"""Start a throwaway API for browser end-to-end tests.

Creates a fresh SQLite database (backend/e2e.db), applies every migration, seeds
reference data with a known administrator, and serves on 127.0.0.1:8100 with the
queue worker embedded. Nothing here touches the development database or secrets.

    python scripts/e2e_server.py
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "e2e.db"
PORT = int(os.environ.get("E2E_API_PORT", "8100"))
WEB = os.environ.get("E2E_WEB_URL", "http://localhost:3100")

for suffix in ("", "-wal", "-shm", "-journal"):
    Path(f"{DB}{suffix}").unlink(missing_ok=True)

os.environ.update({
    "WAE_ENV": "development",
    "WAE_DATABASE_URL": f"sqlite:///{DB.as_posix()}",
    # Fixed, test-only secrets so the run never generates or reads backend/.dev-secrets.json.
    "WAE_SECRET_KEY": "e2e-only-session-signing-key-not-for-any-real-deployment",
    "WAE_MASTER_KEYS": "e2e:" + base64.b64encode(b"e2e-vault-key-32-bytes-long!!!!!").decode(),
    "WAE_ACTIVE_KEY_ID": "e2e",
    "WAE_BOOTSTRAP_ADMIN_EMAIL": os.environ.get("E2E_ADMIN_EMAIL", "e2e-admin@weakless.pro"),
    "WAE_BOOTSTRAP_ADMIN_PASSWORD": os.environ.get("E2E_ADMIN_PASSWORD", "E2e-Admin-Passw0rd"),
    "WAE_PUBLIC_BASE_URL": WEB,
    "WAE_FRONTEND_URL": WEB,
    "WAE_CORS_ORIGINS": WEB,
    "WAE_EMBEDDED_WORKER": "true",
    "WAE_WORKER_POLL_SECONDS": "0.2",
    "WAE_RATE_LIMIT_PER_MINUTE": "100000",
    "WAE_AUTH_RATE_LIMIT_PER_MINUTE": "100000",
    "WAE_SMTP_HOST": "",
    "WAE_ANTHROPIC_API_KEY": "",
    "WAE_OPENAI_API_KEY": "",
    "WAE_GEMINI_API_KEY": "",
})
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=PORT, log_level="warning")
