"""PostgreSQL Row Level Security for tenant-owned tables.

Every tenant table gets a policy that only exposes rows whose organization_id
matches the session's ``app.current_org_id`` setting. The application sets that
value per transaction after verifying the caller's membership (see
``app/db.py`` and ``app/deps.py``). Trusted background contexts (worker job
claiming, schedulers, seeding, platform-wide staff views) set
``app.rls_bypass = on``.

``FORCE ROW LEVEL SECURITY`` makes the policy apply to the table owner as well,
so the API cannot bypass it by accident even when it connects as the owner.
Do not run the application as a PostgreSQL superuser or a role with BYPASSRLS:
those roles ignore RLS entirely.

No-op on SQLite (development / tests), where isolation is enforced by the API layer.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import op

# Frozen at the time of this revision: tables added later get their policies in their own migration
# (importing the live list from app.models would reference tables that do not exist yet).
TENANT_TABLES = (
    "clients", "workspaces", "workflows", "workflow_versions", "workflow_nodes", "workflow_connections",
    "workflow_executions", "workflow_execution_steps", "workflow_schedules", "execution_queues",
    "installed_templates", "ai_agents", "ai_agent_tools", "ai_conversations", "ai_messages",
    "ai_agent_executions", "knowledge_bases", "knowledge_documents", "knowledge_chunks",
    "integrations", "integration_credentials", "oauth_states", "webhook_endpoints", "webhook_events",
    "approval_requests", "notification_channels", "subscriptions", "usage_records", "generated_documents",
)

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREDICATE = (
    "current_setting('app.rls_bypass', true) = 'on' "
    "OR organization_id::text = current_setting('app.current_org_id', true)"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TENANT_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})')


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TENANT_TABLES:
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
