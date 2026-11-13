# WAE Database Schema

Source of truth: `backend/app/models.py` (55 tables). Migrations: `backend/alembic/versions/`.

## Conventions

- UUID primary keys everywhere.
- `organization_id` (FK to `organizations`, `ON DELETE CASCADE`) on every tenant-owned table.
- Timestamps are timezone-aware UTC (`created_at`, usually `updated_at`).
- JSONB on PostgreSQL (JSON on SQLite) for extensible configuration: node configs, provider settings, plan limits, interpreter state.
- Status columns are guarded by `CHECK` constraints.
- Soft deletion (`deleted_at`) on users, organizations, workflows, agents and integrations; history tables are never soft-deleted.
- Secrets are stored only as envelope-encrypted text (see WAE_SECURITY.md).

## Tables

### Identity and access
| Table | Purpose | Notable constraints |
|---|---|---|
| `users` | Accounts. `platform_role` is set for Weakless staff, NULL for client users. Two-factor columns: encrypted TOTP secret (and a pending one during set-up), enabled time, last accepted step, recovery code hashes | unique `email`; check on `platform_role` |
| `user_sessions` | Server-side sessions (revocable), per-session CSRF token | index `user_id` |
| `login_history` | Every sign-in attempt | index `email`, `created_at` |
| `user_tokens` | Password reset and invitation links (SHA-256 of the token only), single use, with expiry | unique `token_hash`; check on `purpose`; index (user, purpose) |
| `roles`, `permissions`, `role_permissions` | RBAC grants (seeded, editable) | unique keys; unique (role, permission) |

### Tenancy
| Table | Purpose | Notable constraints |
|---|---|---|
| `organizations` | Tenants. `mode` agency/saas, `status` | unique `slug` |
| `organization_members` | Client users in an organization with a client role | unique (organization, user); check on role |
| `clients` | Business profile of the customer, one per organization | unique `organization_id` |
| `workspaces` | Grouping of workflows inside an organization | unique (organization, slug) |

### Workflows and execution
| Table | Purpose | Notable constraints |
|---|---|---|
| `workflows` | Workflow header: status, active flag, variables, current version | index (organization, is_active) |
| `workflow_nodes`, `workflow_connections` | The editable draft graph | unique (workflow, node_key); unique edge |
| `workflow_versions` | Immutable published snapshots | unique (workflow, version) |
| `workflow_executions` | One run: status, trigger payload, interpreter state, output, error, idempotency key | unique (workflow, idempotency_key); indexes (organization, created_at), (organization, status) |
| `workflow_execution_steps` | Step log: resolved input, output, error, attempt, loop iteration, timing | index (execution, sequence) |
| `workflow_schedules` | Cron schedules of Schedule triggers | unique (workflow, node_key); index (is_active, next_run_at) |
| `execution_queues` | Durable job queue; `DEAD` rows are the dead-letter queue | index (status, run_at) |
| `approval_requests` | Human approvals for workflow steps and agent tools | index `status` |
| `generated_documents` | Output of the Generate Document node | |

### Templates
| Table | Purpose |
|---|---|
| `automation_templates`, `template_versions` | Global template catalog with versioned definitions and configuration schemas |
| `installed_templates` | A template installed in an organization, linked to its own workflow |
| `industry_packages`, `industry_package_items` | Packages as ordered collections of templates |

### AI
| Table | Purpose |
|---|---|
| `ai_agents` | Agent configuration (instructions, provider, model, fallbacks, limits) |
| `ai_agent_tools` | The explicit tool allowlist of an agent, with scoped configuration and approval flag |
| `ai_conversations`, `ai_messages` | Conversation memory |
| `ai_agent_executions` | Each agent run or single LLM call: tokens, estimated cost, trace, pending approvals |
| `knowledge_bases`, `knowledge_documents`, `knowledge_chunks` | Documents and BM25-indexed passages |

### Integrations and events
| Table | Purpose |
|---|---|
| `integration_providers` | Catalog mirror (synced from code at start-up) |
| `integrations` | A connection owned by an organization: status, non-secret config, health |
| `integration_credentials` | Encrypted secret material, with the id of the wrapping key |
| `oauth_states` | Single-use OAuth state (hashed) and encrypted PKCE verifier |
| `webhook_endpoints` | Per-trigger inbound URL, signature scheme, encrypted secret, rate limit |
| `webhook_events` | Central event log (inbound, application, replay) with de-duplication key |

### Governance, notifications, billing
| Table | Purpose |
|---|---|
| `audit_logs` | Who did what, to which resource, from where |
| `notifications`, `notification_preferences` | In-app inbox and per-user preferences |
| `notification_channels`, `notification_deliveries` | Organization channels (encrypted targets) and delivery log |
| `subscription_plans`, `subscriptions` | Editable plans; one subscription per organization |
| `usage_records` | Metered usage per metric and month (detail for analytics and charts) |
| `usage_counters` | Running total per organization, period and metric, incremented atomically (`INSERT ... ON CONFLICT DO UPDATE`) with every record; quota checks, alerts and the usage report read these instead of summing records |
| `app_event_subscriptions` | Application Event triggers of each workflow's published version (`exact_type` for exact patterns, NULL for wildcards), synced on publish and activation; emitting an event is an indexed lookup joined to active workflows |
| `billing_events` | Subscription and payment-provider events (idempotent on provider event id) |
| `system_settings` | Platform configuration (non-secret) |

The brief's entity list maps one-to-one onto these tables. Additions beyond it: `user_sessions`, `login_history`, `user_tokens`, `clients`, `industry_packages`, `industry_package_items`, `oauth_states`, `notification_preferences`, `notification_channels`, `notification_deliveries`, `generated_documents`.

## Tenant isolation in the database

`TENANT_TABLES` in `models.py` lists 29 tables protected by Row Level Security. Migration `0002` runs, for each:

```sql
ALTER TABLE t ENABLE ROW LEVEL SECURITY;
ALTER TABLE t FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON t
  USING (current_setting('app.rls_bypass', true) = 'on'
         OR organization_id::text = current_setting('app.current_org_id', true))
  WITH CHECK (...same...);
```

The application sets both settings at the start of every transaction (`app/db.py`). A session with neither set sees no tenant rows. `organization_members` and `organizations` are not under RLS because authentication must read them before a tenant is known; they are guarded in the API layer. A test asserts that every table with a mandatory `organization_id` is in `TENANT_TABLES` (except `organization_members`).

**Status (2026-11-12):** verified on PostgreSQL 17 locally and PostgreSQL 16 in CI. The whole backend suite runs against PostgreSQL as a role without `SUPERUSER`/`BYPASSRLS` that owns the tables (so `FORCE` makes the policies apply to it), and `tests/test_rls.py` checks isolation in the database itself: a tenant session sees and counts only its own rows in every tenant table, a session with no tenant sees nothing, cross-tenant inserts are rejected by the policy and cross-tenant updates/deletes affect no rows. The migration is a no-op on SQLite.

## Migrations

| Revision | Content |
|---|---|
| `0001` | Complete schema, generated from the models and hand-adjusted so the two circular foreign keys (`workflows.current_version_id`, `automation_templates.current_version_id`) are added after both tables exist |
| `0002` | Row Level Security (PostgreSQL only) |
| `0003` | `user_tokens` for password reset and invitations |
| `0004` | Two-factor sign-in columns on `users` |
| `0005` | `workflow_executions.details_purged_at` and indexes for the retention sweeps |
| `0006` | `app_event_subscriptions` and `usage_counters`, back-filled from published versions and existing usage records; Row Level Security on both (PostgreSQL). `0002` now carries its own frozen table list, so later tenant tables get their policies in their own migration |
| `0007` | `rate_limit_counters` for limits shared by every API instance (not a tenant table: keys are SHA-256 digests) |
| `0010` | `workflow_executions.parent_execution_id` / `parent_node_key` (Run Workflow) and `response` (Respond to Webhook) |
| `0009` | AI teams and the content calendar: `ai_agents.team_key/team_module/team_lead`, `ai_agent_executions.parent_execution_id` (delegation chain), `industry_packages.team_key`, and `content_items` (Row Level Security on PostgreSQL) |
| `0008` | `mailbox_watches`: one row per Mailbox Trigger of a published workflow (folder, poll interval, UIDVALIDITY and last UID read, failures); Row Level Security on PostgreSQL |

## Data retention

`app/services/retention.py` applies the `data.retention` platform setting (Settings -> Platform), hourly from every worker and on demand (`POST /api/v1/retention/run`, `python -m app.cli retention`). Sweeps delete in batches of 500 rows, at most 20 batches per category per run, so a backlog is worked off over several runs without long locks. Cut-offs fall on whole UTC days.

| Setting (default) | Effect |
|---|---|
| `execution_details_days` (30) | Finished executions lose their step rows, trigger payload, state, variables, output and resume token; `details_purged_at` is set. Status, timing and error remain. Such executions can no longer be retried |
| `executions_days` (365) | Finished executions are deleted with their steps, queue jobs and approvals; agent runs and generated documents are unlinked. Finished agent runs without approvals are deleted too |
| `events_days` (30) | `webhook_events` deleted. Duplicate detection only sees stored events |
| `usage_detail_days` (62) | `usage_records` merged into one row per organization, metric, period, workflow and UTC day (`meta.compacted_records`); period and daily totals are unchanged |
| `notifications_days` (90) | `notifications` and `notification_deliveries` deleted |
| `queue_jobs_days` (7) | `DONE` queue jobs deleted; `DEAD` jobs are kept |
| `audit_days` (off) | `audit_logs` and `login_history` deleted; minimum 30 days |

Always: sessions, account links and OAuth states are deleted a week after they expire. Executions that are not finished (waiting, running) are never touched. Conversations, knowledge bases and workflow versions are not subject to retention.

Verified locally on SQLite: upgrade, `alembic check` (no drift), downgrade to base, upgrade again.
