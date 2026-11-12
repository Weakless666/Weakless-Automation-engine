# WAE Implementation Plan

## What was done, by phase

| Phase | Scope | Result |
|---|---|---|
| 1 | Repository inspection, architecture | Repository was empty. Toolchain: Python 3.14, Node 24; **no Docker, PostgreSQL or Redis** available. Architecture chosen to be testable without them (see WAE_ARCHITECTURE.md). |
| 2 | Database and authentication | 49-table schema, Alembic migrations, Argon2 passwords, revocable sessions, CSRF, lockout |
| 3 | Multi-tenant infrastructure | Organizations, members, clients, workspaces, nine roles, API-layer isolation, RLS migration |
| 4 | Admin dashboard and design system | Tailwind token system, UI kit, overview dashboard |
| 5 | Workflow builder | React Flow editor, schema-driven node forms, draft/publish/version API |
| 6 | Execution engine | Interpreter, durable queue, worker, retries, waits, crash recovery, dead letters |
| 7 | AI agent engine | Four providers, agent loop, controlled tools, approvals, MCP client, knowledge bases |
| 8 | Integration hub | 32 providers, OAuth, health checks, revocation |
| 9 | Triggers and events | Signed webhooks, forms, schedules, application events, replay |
| 10 | Template marketplace | 46 templates from 17 patterns, install/configure/update |
| 11 | Industry packages | Six packages |
| 12 | Vault and security hardening | Envelope encryption, rotation, redaction, SSRF guard, audit |
| 13 | Analytics and monitoring | Overview, time series, per-workflow, AI, integration and system health |
| 14 | Billing and usage | Metering, quotas, alerts, plans, manual and Stripe providers |
| 15 | Testing, documentation, deployment | 58 backend tests; README and documents; Dockerfiles, compose, CI (unverified) |

## What should happen next

Ordered by how much risk each item removes.

### 1. Verify on real infrastructure (before any client data)
1. Run `docker compose up --build` on a machine with Docker; fix whatever the first real build reveals.
2. Run both migrations on PostgreSQL and execute the cross-tenant checks in WAE_SECURITY.md as the `wae_app` role.
3. Run the backend test suite against PostgreSQL (add a `WAE_TEST_DATABASE_URL` switch to `tests/conftest.py`), with two workers, to exercise `SKIP LOCKED` claiming and RLS together.
4. Push to GitHub and get the CI workflow green.

### 2. Verify against real providers
5. Set a platform Anthropic key and run an agent and each AI node type once. Confirm the refusal-fallback beta parameter is accepted by the account; if not, switch it off under Settings -> Platform.
6. For each integration a first client needs: connect it with a real account and run one workflow through it. Expect small fixes in adapters that have never been called.
7. Create the Google and Microsoft OAuth apps and complete one authorization each.
8. Configure Stripe in test mode: one checkout, one webhook delivery.

### 3. Close product gaps
9. ~~Browser end-to-end tests for every main screen~~ (done 2026-11-12).
10. ~~Password reset and email invitations~~ (done 2026-11-12); MFA for staff.
11. Typed response models so OpenAPI documents responses.
12. Live execution updates over WebSocket or SSE if polling proves too slow.
13. Vector search for knowledge bases once an embedding provider is chosen.
14. Mailbox polling for the Email trigger if clients cannot use inbound-parse webhooks.

### 4. Scale-out
15. Shared rate limiting (gateway or Redis) when running more than one API instance.
16. Retention jobs for `workflow_execution_steps`, `webhook_events` and `usage_records`.
17. Index and query review under realistic volume (the application-event trigger lookup scans an organization's active workflows).
