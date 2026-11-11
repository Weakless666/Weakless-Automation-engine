# WAE Implementation Report

Date: 2026-11-11. This report separates what is proven from what is only written.

## Summary

The platform exists end to end: backend (about 10,000 lines of Python), frontend (about 3,700 lines of TypeScript), migrations, deployment files and documentation. The backend's core behaviour is covered by 58 passing automated tests, and a workflow was created, published, activated and executed through the running API, worker and web proxy.

Three things limit how far "working" can be claimed today:

1. **No external service has been contacted.** Every provider call in the tests is mocked. No real AI model, messaging provider, CRM, OAuth server, Stripe or SMTP server has been used.
2. **PostgreSQL has never run this code.** Development and tests use SQLite. The Row Level Security migration in particular is unexecuted.
3. **Docker and CI have never run.** Neither was available in the development environment.

## 1. Implemented and tested

Automated tests pass for the following (`cd backend && pytest -q`).

**Execution engine**
- Linear, if/else, switch, validate, merge and loop execution with persisted steps
- Template rendering and type preservation
- Error routing through the `error` output; failed executions name the failing node
- Durable retry with back-off only where allowed; no automatic retry of non-idempotent steps
- Delay, external wait, human approval: suspend and resume through the queue
- Cancellation, idempotency keys
- Crash recovery that refuses to repeat a non-idempotent step; dead-letter queue after repeated infrastructure failure
- Graph validation (triggers, cycles, handles, unknown node types, loop rules)
- All 45 node types register and describe themselves; all 46 templates validate

**Security and tenancy**
- Unauthenticated access rejected; generic login failures; lockout; logout revocation; CSRF on cookie sessions
- Password reset (single use, expiry, supersession, session revocation, no account enumeration) and invitations (pending accounts cannot sign in, per-organization activation, no password overwrite, resend/disable/remove invalidation, staff invitations, role limits on staff-issued reset links)
- Cross-tenant access blocked: header spoofing, IDOR, list scoping, cross-tenant integration use
- Role enforcement and privilege-escalation rules
- Vault round-trip, tenant binding, key rotation; credentials absent from API responses, storage and logs
- Webhook signatures, stale timestamps, tampering, duplicates, rate limits
- Input validation and rejection of unknown fields

**Platform**
- The ten-step journey from the brief, driven through the HTTP API: create organization, add client member, connect integration, install and configure a template, publish, activate, inbound trigger, execution with AI and messaging steps, client history, admin analytics
- Workflow drafts, optimistic concurrency, versions, restore, test runs
- Cron schedules; application events without feedback loops; event replay
- Agents: tool calling, allowlist enforcement, conversation memory, usage and cost, daily limits, human approval and rejection
- MCP client and single-tool allowlisting
- Provider fallback chain; request mapping for OpenAI-compatible and Gemini
- Template isolation between organizations; configure and update; package install
- Quotas, usage alerts, plan changes, scheduled downgrades, period roll-over
- Notification channels, preferences and delivery log
- Platform administration, settings validation, OpenAPI contents
- Key-rotation CLI, OpenAPI export, migration upgrade/downgrade on SQLite

**Live smoke test** (real API process, embedded worker, migrated SQLite, through the Next.js proxy): sign-in; organization, workflow, publish, activate, execute to `COMPLETED`; dashboard, builder (5 nodes, 4 connections, 49 palette entries) and execution pages rendered with no application console errors.

## 2. Implemented but not externally verified

Code exists and is reasoned to be correct; it has not been run against the real thing.

| Area | What is unverified |
|---|---|
| Anthropic | Tested through a stand-in client shaped like the SDK. No live request. The server-side refusal fallback (`fallbacks: "default"` beta) is enabled by default for supported models and can be turned off in Settings if an account rejects it. |
| OpenAI, OpenAI-compatible, Gemini | Request/response mapping tested with mocked HTTP only |
| Integration adapters | Telegram, OpenAI and MCP are exercised with mocks. Gmail, Google Calendar/Drive/Sheets/Docs, Outlook, Microsoft 365, WhatsApp, Discord, Slack, SMTP, Notion, Airtable, Supabase, PostgreSQL, HubSpot, Pipedrive, Salesforce, Shopify, WooCommerce, Stripe, Meta, LinkedIn, Mailchimp, REST API and custom webhook adapters have **never been called**, mocked or live |
| OAuth | Authorization URL, state handling, code exchange and refresh are implemented; no flow has been completed |
| PostgreSQL | Migrations, Row Level Security policies, `SKIP LOCKED` job claiming, JSONB behaviour |
| Stripe billing | Checkout session creation never called. Webhook signature verification and idempotency are tested; event handling for real payloads is not |
| Email (SMTP) | Platform notifications and the SMTP integration |
| Sentry | Initialization only |
| Docker, compose, backups | Never built or run |
| CI | Never run |
| Frontend | Typechecks and builds; pages render against live data. Interactive flows (dragging and connecting nodes, dialogs, forms) were not driven by a person or by automated tests, and the visual design was not reviewed on screen in this session |
| Templates | 45 of 46 are validated but have not been executed |
| Multi-worker operation | Only one worker has been run |

## 3. Awaiting credentials

Built, and reported in the UI as not ready until configured:

- OAuth apps: Google, Microsoft, Salesforce, LinkedIn (`WAE_OAUTH_<GROUP>_CLIENT_ID` / `_CLIENT_SECRET`)
- AI provider keys (platform-level or per organization)
- Stripe secret key, webhook secret and price ids on plans
- SMTP server
- Sentry DSN
- Per-client credentials for every integration they use

## 4. Incomplete or not built

Relative to the brief:

- **n8n, Temporal, BullMQ, Redis** - evaluated and deliberately not integrated; reasons in WAE_ARCHITECTURE.md. The queue is PostgreSQL-backed.
- **WebSockets** - not implemented; the UI polls.
- **Separate interfaces per role** - one interface filtered by permission rather than four separate ones.
- **Email trigger** - webhook-based only; no mailbox polling.
- **Database event trigger** - webhook-based only; no native listeners.
- **Knowledge retrieval** - keyword (BM25) only; no embeddings.
- **Upload File** - text content only; no binary uploads.
- **Authentication** - no MFA or SSO. (Password reset and invitations were added on 2026-11-12; their emails are untested against a real SMTP server.)
- **API** - response schemas are not typed in OpenAPI.
- **Testing** - browser tests cover the workflow builder only (added 2026-11-12); no OAuth flow tests; no load tests.
- **Rate limiting** - per process, not shared across instances.
- **Per-step hard timeouts** - enforced between steps and by client timeouts, not by interrupting a running step.
- **Data retention** - no clean-up jobs for logs and events.
- **Meta and LinkedIn** - connection test and generic API call only; no purpose-built posting or ads actions.

## 5. Known limitations

- Plan prices and limits are placeholder values to be edited. Non-Anthropic model prices are unset, so their cost is reported as unknown.
- Test runs perform real actions (messages are sent, records are created); they are only excluded from quotas and analytics.
- A manual retry re-runs the whole workflow, including steps that had succeeded.
- The SSRF guard has a DNS-rebinding window; use egress filtering.
- Regular expressions in conditions have no execution timeout.
- The application-event trigger scans an organization's active workflows per event.
- Notification channel delivery is synchronous inside the triggering operation (bounded by 10-second timeouts).
- Python 3.14 was used locally; the Docker image uses 3.13, an untested combination here.

## Recommended first step

Follow section 1 of WAE_IMPLEMENTATION_PLAN.md: bring the stack up on PostgreSQL with Docker and run the tenant-isolation checks. Until that is done, do not put more than one real client on the system.
