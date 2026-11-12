"""Operational command line.

    python -m app.cli generate-keys            # print fresh WAE_SECRET_KEY / WAE_MASTER_KEYS values
    python -m app.cli seed                     # reference data (also runs at API start-up)
    python -m app.cli create-admin EMAIL       # create a SUPER_ADMIN (password from WAE_ADMIN_PASSWORD or prompt)
    python -m app.cli worker                   # run a queue worker process
    python -m app.cli rotate-keys              # re-wrap every stored secret under the active master key
    python -m app.cli retention                # apply the data retention policy now (workers run it hourly)
    python -m app.cli export-openapi PATH      # write the OpenAPI document
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import secrets
import signal
import sys

from sqlalchemy import select

from app.config import get_settings
from app.core import vault
from app.core.security import hash_password, validate_password_strength
from app.db import session_scope


def cmd_generate_keys(_: argparse.Namespace) -> int:
    print(f"WAE_SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"WAE_MASTER_KEYS=k1:{base64.b64encode(secrets.token_bytes(32)).decode()}")
    print("WAE_ACTIVE_KEY_ID=k1")
    return 0


def cmd_seed(_: argparse.Namespace) -> int:
    from app.seed import seed_all

    with session_scope(bypass_rls=True) as db:
        seed_all(db)
    print("Reference data is up to date.")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    from app.models import User
    from app.seed import seed_all

    password = os.environ.get("WAE_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    problem = validate_password_strength(password)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    email = args.email.lower().strip()
    with session_scope(bypass_rls=True) as db:
        seed_all(db)
        if db.scalar(select(User.id).where(User.email == email)):
            print("A user with this email already exists.", file=sys.stderr)
            return 1
        db.add(User(email=email, full_name=args.name or "Administrator", password_hash=hash_password(password), platform_role="SUPER_ADMIN"))
    print(f"Created SUPER_ADMIN {email}")
    return 0


def cmd_worker(_: argparse.Namespace) -> int:
    from app.engine.queue import Worker
    from app.main import configure_logging

    configure_logging()
    worker = Worker()
    signal.signal(signal.SIGINT, lambda *_: worker.stop())
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    worker.run_forever()
    return 0


def cmd_rotate_keys(_: argparse.Namespace) -> int:
    """Re-wrap data keys under WAE_ACTIVE_KEY_ID. Old keys must still be present in
    WAE_MASTER_KEYS while this runs; remove them afterwards."""
    from app.models import IntegrationCredential, NotificationChannel, OAuthState, WebhookEndpoint

    active = get_settings().active_key_id
    counts = {"integration_credentials": 0, "webhook_endpoints": 0, "notification_channels": 0, "oauth_states": 0}
    with session_scope(bypass_rls=True) as db:
        for cred in db.scalars(select(IntegrationCredential).where(IntegrationCredential.key_id != active)):
            cred.envelope = vault.rewrap(cred.envelope)
            cred.key_id = active
            counts["integration_credentials"] += 1
        for model, column, key in ((WebhookEndpoint, "secret_envelope", "webhook_endpoints"),
                                   (NotificationChannel, "secret_envelope", "notification_channels"),
                                   (OAuthState, "verifier_envelope", "oauth_states")):
            for row in db.scalars(select(model).where(getattr(model, column).is_not(None))):
                current = getattr(row, column)
                if vault.envelope_key_id(current) != active:
                    setattr(row, column, vault.rewrap(current))
                    counts[key] += 1
    print(f"Re-wrapped secrets under key '{active}': {json.dumps(counts)}")
    return 0


def cmd_retention(_: argparse.Namespace) -> int:
    """Apply the data retention policy once (the worker also does this hourly)."""
    from app.services.retention import get_policy, run_retention

    with session_scope(bypass_rls=True) as db:
        print("Policy:", json.dumps(get_policy(db)))
        result = run_retention(db)
    for key, count in result.items():
        print(f"{key}: {count}")
    return 0


def cmd_export_openapi(args: argparse.Namespace) -> int:
    from app.main import create_app

    doc = create_app().openapi()
    with open(args.path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2)
    print(f"Wrote {len(doc['paths'])} paths to {args.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Weakless Automation Engine operations")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-keys").set_defaults(fn=cmd_generate_keys)
    sub.add_parser("seed").set_defaults(fn=cmd_seed)
    admin = sub.add_parser("create-admin")
    admin.add_argument("email")
    admin.add_argument("--name", default="")
    admin.set_defaults(fn=cmd_create_admin)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    sub.add_parser("rotate-keys").set_defaults(fn=cmd_rotate_keys)
    sub.add_parser("retention").set_defaults(fn=cmd_retention)
    export = sub.add_parser("export-openapi")
    export.add_argument("path")
    export.set_defaults(fn=cmd_export_openapi)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
