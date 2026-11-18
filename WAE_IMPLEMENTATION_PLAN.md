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
| 15 | Testing, documentation, deployment | 86 backend tests (OpenAPI typed for every response); 24 browser tests; README and documents; Dockerfiles, compose, CI (unverified) |

## What should happen next

Ordered by how much risk each item removes.

### 1. Verify on real infrastructure (before any client data)
1. Run `docker compose up --build` on a machine with Docker; fix whatever the first real build reveals.
2. ~~Run the migrations on PostgreSQL and the cross-tenant checks as a non-superuser role~~ (done 2026-11-12: `tests/test_rls.py`, locally and in CI).
3. ~~Run the backend test suite against PostgreSQL with parallel workers~~ (done 2026-11-12: `WAE_TEST_DATABASE_URL`, four-worker queue test).
4. ~~Push to GitHub and get the CI workflow green~~ (green since the first run).

### 2. Verify against real providers
5. Set a platform Anthropic key and run an agent and each AI node type once. Confirm the refusal-fallback beta parameter is accepted by the account; if not, switch it off under Settings -> Platform.
6. For each integration a first client needs: connect it with a real account and run one workflow through it. Expect small fixes in adapters that have never been called.
7. Create the Google and Microsoft OAuth apps and complete one authorization each.
8. Configure Stripe in test mode: one checkout, one webhook delivery.

### 3. Close product gaps
9. ~~Browser end-to-end tests for every main screen~~ (done 2026-11-12).
10. ~~Password reset, email invitations and two-factor sign-in for staff~~ (done 2026-11-12); SSO and passkeys later.
11. ~~Typed response models so OpenAPI documents responses~~ (done 2026-11-12).
12. Live execution updates over WebSocket or SSE if polling proves too slow.
13. Vector search for knowledge bases once an embedding provider is chosen.
14. ~~Mailbox polling for clients who cannot use inbound-parse webhooks~~ (done 2026-11-13: Mailbox Trigger over IMAP; next, try it against Gmail and Microsoft 365 with real accounts).

### 3b. AI Marketing Team (from 2026-11-13)
- ~~Phase 1: lead agent delegating to nine specialists, content calendar with approval, team board, weekly plan/newsletter/review workflows, lead intake~~ (done 2026-11-13, package "AI Marketing Team").
- Phase 2: native publishing for the channels a client uses, replacing "notify a person to post" in the Publish Approved Content workflow. LinkedIn (`w_member_social`, already an OAuth integration), Instagram and Facebook (Meta Graph API, app review for `instagram_content_publish`), YouTube (Data API upload, Google OAuth), TikTok (Content Posting API, requires TikTok's app audit before public posts). Each needs a developer app and test accounts from the business.
- Phase 3: Creative Studio - AI images, voice and short video from the media brief, once providers are chosen (keys from the business); results attached to the calendar item for approval.
- Phase 4: real performance data in the Analytics Loop (post insights from the connected networks, Stripe revenue, CRM pipeline) instead of what people record in the calendar.
- Live trend data for the Trend Spotter and Keyword Finder needs a search or trends API key; today they work from the playbook and the team's past results and say so.

### 4. Scale-out
15. ~~Shared rate limiting when running more than one API instance~~ (done 2026-11-12 in the database, no Redis; the general per-client budget stays per process).
15a. ~~Live execution status and real binary files~~ (done 2026-11-18: Server-Sent Events stream, `files` table, attachments and uploads; next for files: object storage for large media).
16. ~~Retention jobs for `workflow_execution_steps`, `webhook_events` and `usage_records`~~ (done 2026-11-12, configurable under Settings -> Platform).
17. Index and query review under realistic volume. Done 2026-11-12 for the two paths that grew with volume: application-event triggers are an indexed subscription lookup, and quota checks read running counters. Still to do: measure on PostgreSQL with production-sized data (`EXPLAIN ANALYZE` on the execution list, analytics and retention queries).
