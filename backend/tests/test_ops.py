"""Operational tooling: key rotation, OpenAPI export, migrations metadata."""
from __future__ import annotations

import base64
import json

from sqlalchemy import select

from app import cli
from app.config import get_settings
from app.core import vault
from app.models import TENANT_TABLES, IntegrationCredential, WebhookEndpoint
from tests.conftest import make_workflow, wf_def


def test_key_rotation_rewraps_every_secret(db, org, monkeypatch, http_mock, capsys):
    from app.integrations.service import create_integration, load_secret

    http_mock.add("GET", "https://api.telegram.org/", (200, {"ok": True, "result": {"username": "b"}}))
    integration = create_integration(db, org.id, "telegram", "Bot", {"bot_token": "123456789:" + "R" * 35}, None)
    make_workflow(db, org, wf_def([{"id": "h", "type": "trigger.webhook", "config": {}}], []))
    db.commit()
    assert db.scalar(select(IntegrationCredential)).key_id == "test1"

    old_key = base64.b64encode(b"\x01" * 32).decode()
    new_key = base64.b64encode(b"\x09" * 32).decode()
    monkeypatch.setenv("WAE_MASTER_KEYS", f"test1:{old_key},k2:{new_key}")
    monkeypatch.setenv("WAE_ACTIVE_KEY_ID", "k2")
    get_settings.cache_clear()
    vault.set_key_provider(None)
    try:
        assert cli.main(["rotate-keys"]) == 0
        assert '"integration_credentials": 1' in capsys.readouterr().out
        # The old key can now be retired: everything decrypts with the new key alone.
        monkeypatch.setenv("WAE_MASTER_KEYS", f"k2:{new_key}")
        get_settings.cache_clear()
        vault.set_key_provider(None)
        db.expire_all()
        assert db.scalar(select(IntegrationCredential)).key_id == "k2"
        assert load_secret(db, integration)["bot_token"].startswith("123456789:")
        ep = db.scalar(select(WebhookEndpoint))
        from app.services.workflows import endpoint_secret

        assert endpoint_secret(ep)
    finally:
        monkeypatch.delenv("WAE_MASTER_KEYS")
        monkeypatch.delenv("WAE_ACTIVE_KEY_ID")
        get_settings.cache_clear()
        vault.set_key_provider(None)


def test_openapi_export_and_generate_keys(tmp_path, capsys):
    out = tmp_path / "openapi.json"
    assert cli.main(["export-openapi", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["openapi"].startswith("3.") and len(doc["paths"]) > 100
    assert cli.main(["generate-keys"]) == 0
    printed = capsys.readouterr().out
    key = next(line for line in printed.splitlines() if line.startswith("WAE_MASTER_KEYS=")).split(":", 1)[1]
    assert len(base64.b64decode(key)) == 32


def test_every_tenant_table_has_organization_id():
    from app.db import Base

    for name in TENANT_TABLES:
        table = Base.metadata.tables[name]
        assert "organization_id" in table.c and not table.c.organization_id.nullable, name
    # Any table with a mandatory organization_id must be covered by Row Level Security.
    mandatory = {t.name for t in Base.metadata.tables.values() if "organization_id" in t.c and not t.c.organization_id.nullable}
    assert mandatory - set(TENANT_TABLES) == {"organization_members"}
