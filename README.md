# Weakless Automation Engine (WAE)

Multi-tenant automation infrastructure for Weakless.pro: visual workflows, a durable execution engine, AI agents, an integration hub, triggers and events, a template marketplace with industry packages, usage metering and subscription-ready billing.

- **Backend** - Python, FastAPI, SQLAlchemy, PostgreSQL (SQLite for local development and tests), Alembic
- **Frontend** - Next.js (App Router), React, TypeScript, Tailwind CSS, TanStack Query, React Flow
- **Runtime** - API + queue worker(s) + PostgreSQL. No Redis or external workflow engine is required.

Start with [WAE_IMPLEMENTATION_REPORT.md](WAE_IMPLEMENTATION_REPORT.md) for an honest account of what is tested, what is implemented but unverified, and what is not built.

| Document | Contents |
|---|---|
| [WAE_ARCHITECTURE.md](WAE_ARCHITECTURE.md) | Components, execution model, technology decisions |
| [WAE_DATABASE_SCHEMA.md](WAE_DATABASE_SCHEMA.md) | Tables, constraints, tenancy, migrations |
| [WAE_SECURITY.md](WAE_SECURITY.md) | Threat model and controls |
| [WAE_API_DOCUMENTATION.md](WAE_API_DOCUMENTATION.md) | REST API guide ([docs/openapi.json](docs/openapi.json) is the full reference) |
| [WAE_IMPLEMENTATION_PLAN.md](WAE_IMPLEMENTATION_PLAN.md) | Phases and what remains |
| [WAE_PROGRESS.md](WAE_PROGRESS.md) | Status per module |

## Repository layout

```
backend/
  app/
    api/            REST routers (/api/v1)
    core/           vault, SSRF-guarded HTTP, redaction, rate limiting, errors, security helpers
    engine/         node registry, graph validation, executor, durable queue + worker, node library
    ai/             provider abstraction, agent loop, controlled tools, MCP client, knowledge bases
    integrations/   provider catalog + adapters, OAuth, connection lifecycle
    services/       workflows, executions, webhooks, events, scheduler, approvals, usage, billing, analytics, notifications
    templates/      template library, industry packages, install/update service
    models.py       complete schema (54 tables)
    cli.py          operational commands
  alembic/          migrations (0001 schema, 0002 PostgreSQL Row Level Security, 0003 account links, 0004 two-factor, 0005 data retention, 0006 event index and usage counters, 0007 shared rate limits, 0008 mailbox watches)
  tests/            83 tests: engine, security, accounts, two-factor, platform/e2e, operations
frontend/           Next.js application
deploy/             PostgreSQL role bootstrap, backup script
docker-compose.yml  single-host deployment
```

## Local development

Requirements: Python 3.11+ and Node.js 20+.

### 1. Backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Create `backend/.env` (never commit it):

```
WAE_ENV=development
WAE_BOOTSTRAP_ADMIN_EMAIL=you@weakless.pro
WAE_BOOTSTRAP_ADMIN_PASSWORD=<at least 10 chars, an uppercase letter and a digit>
WAE_PUBLIC_BASE_URL=http://localhost:3000
```

In development the session secret and vault master key are generated once and stored in `backend/.dev-secrets.json` (git-ignored), and the database is SQLite (`backend/wae.db`).

```bash
alembic upgrade head
uvicorn app.main:app --port 8000
```

The API seeds reference data on start (roles, provider catalog, plans, templates) and creates the bootstrap Super Admin if none exists. With `WAE_EMBEDDED_WORKER=true` (the development default) the queue worker runs inside the API process. Interactive API docs: http://localhost:8000/api/docs

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000 and sign in with the bootstrap admin. The frontend proxies `/api/*` to `BACKEND_URL` (default `http://localhost:8000`), so the browser only talks to one origin.

### 3. Tests

```bash
cd backend && pytest -q          # 104 tests on SQLite, no network access needed (RLS tests are skipped)
# The same suite on PostgreSQL, under Row Level Security: the role must own the database and be
# neither SUPERUSER nor BYPASSRLS. The schema is built from the migrations; tables are emptied per test.
WAE_TEST_DATABASE_URL=postgresql://wae:wae@localhost:5432/wae_test pytest -q
cd frontend && npm run typecheck && npm run build
cd frontend && npm run e2e       # browser tests of the workflow builder (below)
```

### 4. Browser tests

`npm run e2e` (in `frontend/`) runs 24 Playwright tests that use the app like a person would:
- **Builder:** create, add and drag-connect steps, configure, save, reload, test-run, publish, activate; validation; duplicate/delete; switch outputs; concurrent edits.
- **Accounts:** invite a member and accept as them in a separate browser; staff reset link; forgot password; generic sign-in error.
- **Templates:** preview, install, configure; install an industry package.
- **Agents:** create, configure, add a scoped tool and require approval; the honest error without an AI key; knowledge base add and search.
- **Platform:** an integration is tested, never shows its secret, and is revoked; a client member sees only what their role allows; every main screen opens without an error.
- **Insights:** analytics from real runs and the automation filter; creating and applying plans, scheduled downgrades, client upgrade requests that never charge; audit filters.
- **Two-factor:** turn it on with a real computed code, sign in with a code and a recovery code, admin reset; required set-up at sign-in.
- **Operations:** filter executions, read the failing step and retry; resume a waiting run; notifications inbox, preferences and channels; inviting a staff member; the platform settings never show secrets. It starts its own throwaway API on port 8100 (fresh `backend/e2e.db`, fixed test-only secrets, its own admin) and a production build of the web app on port 3100, so it never touches your development data and can run while the dev servers are up.

- **Windows:** uses the installed Microsoft Edge; nothing to download. The backend virtual environment must exist (`backend/.venv`).
- **macOS / Linux / CI:** run `npx playwright install chromium` once. Set `E2E_PYTHON` if `python` is not the interpreter with the backend requirements.
- On failure, screenshots and a trace are in `frontend/test-results/`; open a trace with `npx playwright show-trace <path>/trace.zip`.

CI runs these tests in the `browser tests` job.

## Database setup

- **Development / tests:** SQLite, zero setup. Row Level Security does not exist on SQLite; tenant isolation is enforced by the API layer (and tested there).
- **Production:** PostgreSQL 14+. Two roles are used on purpose:
  - an **owner** role that runs migrations (`alembic upgrade head`);
  - an **application** role (`wae_app`) with DML rights only, **not** a superuser and **without** `BYPASSRLS`, used by the API and workers. Superusers bypass Row Level Security, so never run the application as one.
- `deploy/postgres-init.sh` creates the application role and default privileges; `docker-compose.yml` wires both roles.
- Migrations: `0001` creates the schema, `0002` enables and forces RLS on every tenant table (PostgreSQL only).

To create a new migration after changing `app/models.py`: `alembic revision --autogenerate -m "describe change"`, review the file, then `alembic upgrade head`. `alembic check` fails when models and migrations drift.

## Deployment with Docker Compose

```bash
cp .env.example .env
cd backend && python -m app.cli generate-keys    # paste the three values into .env
# fill in WAE_DB_OWNER_PASSWORD, WAE_DB_PASSWORD, WAE_BOOTSTRAP_ADMIN_*, URLs
docker compose up -d --build
```

Services: `postgres`, `migrate` (one-shot), `api`, `worker` (scale with `WAE_WORKER_REPLICAS`), `web` (the only published port), `backup` (daily `pg_dump` to `./backups`, 14-day retention by default).

Put a TLS-terminating reverse proxy in front of `web`, set `WAE_PUBLIC_BASE_URL` / `WAE_FRONTEND_URL` to the public HTTPS origin and `WAE_COOKIE_SECURE=true`.

> The Docker images and compose file were written but could not be built or run in the development environment (no Docker available). Treat the first deployment as a verification step.

## Environment variables

All backend settings use the `WAE_` prefix.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `WAE_ENV` | yes (prod) | `development` | `development`, `test` or `production`. Production refuses to start without secrets. |
| `WAE_DATABASE_URL` | yes (prod) | SQLite file | `postgresql://user:pass@host:5432/db` |
| `WAE_SECRET_KEY` | yes (prod) | generated in dev | Signs session tokens |
| `WAE_MASTER_KEYS` | yes (prod) | generated in dev | Vault key ring: `id:base64(32 bytes)[,id2:...]` |
| `WAE_ACTIVE_KEY_ID` | no | first key | Key used for new secrets |
| `WAE_PUBLIC_BASE_URL` | yes (prod) | `http://localhost:8000` | Public origin used in webhook URLs and OAuth redirect URIs |
| `WAE_FRONTEND_URL` | yes (prod) | `http://localhost:3000` | Redirect target after OAuth / checkout |
| `WAE_CORS_ORIGINS` | no | `http://localhost:3000` | Comma-separated allowed origins |
| `WAE_COOKIE_SECURE` | prod | `false` | `true` behind HTTPS (Secure cookie + HSTS) |
| `WAE_SESSION_HOURS` | no | `12` | Session lifetime |
| `WAE_BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` | first run | - | Creates the first Super Admin when none exists |
| `WAE_STAFF_MFA_REQUIRED` | no | `true` | Initial value of the platform setting that requires two-factor sign-in for Weakless staff (changeable under Settings -> Platform) |
| `WAE_EMBEDDED_WORKER` | no | `true` | Run the worker inside the API process. Set `false` when running dedicated workers. |
| `WAE_WORKER_POLL_SECONDS` | no | `1.0` | Idle polling interval |
| `WAE_JOB_VISIBILITY_TIMEOUT_SECONDS` | no | `300` | After this, a job held by a dead worker is recovered |
| `WAE_JOB_MAX_ATTEMPTS` | no | `3` | Infrastructure retries before the dead-letter queue |
| `WAE_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | no | `900` | Per-execution active time limit |
| `WAE_DEFAULT_ORG_CONCURRENCY` | no | `5` | Concurrent executions per organization without a plan limit |
| `WAE_ALLOW_PRIVATE_NETWORK` | no | `false` | Allow workflow HTTP calls to private addresses (needed for local model servers) |
| `WAE_RATE_LIMIT_PER_MINUTE` / `WAE_AUTH_RATE_LIMIT_PER_MINUTE` | no | `300` / `20` | Per-client API and login limits |
| `WAE_RATE_LIMIT_STORE` | no | `database` | Where sign-in, reset, two-factor, invitation and hook limits live: `database` (shared by every instance) or `memory` (single instance) |
| `WAE_ANTHROPIC_API_KEY`, `WAE_OPENAI_API_KEY`, `WAE_GEMINI_API_KEY` | no | - | Platform-level AI keys (fallback when an organization has no AI integration) |
| `WAE_SMTP_HOST`, `_PORT`, `_USERNAME`, `_PASSWORD`, `_FROM`, `_STARTTLS` | no | - | Platform email for notifications |
| `WAE_STRIPE_SECRET_KEY`, `WAE_STRIPE_WEBHOOK_SECRET` | no | - | Enables Stripe Checkout and the Stripe webhook |
| `WAE_OAUTH_<GROUP>_CLIENT_ID` / `_CLIENT_SECRET` | no | - | OAuth apps. Groups: `GOOGLE`, `MICROSOFT`, `SALESFORCE`, `LINKEDIN` |
| `WAE_SENTRY_DSN` | no | - | Error tracking |
| `WAE_LOG_LEVEL` | no | `INFO` | Log level (logs are JSON lines with secrets redacted) |

Frontend: `BACKEND_URL` (build time for Docker, run time for `npm run dev`), default `http://localhost:8000`.

## Configuring external providers

Nothing is reported as connected until a live check against the provider succeeds. Providers that cannot work yet say why.

- **API-key providers** (Telegram, Slack, WhatsApp Cloud API, HubSpot, Pipedrive, Notion, Airtable, Shopify, WooCommerce, Stripe, Mailchimp, Supabase, PostgreSQL, SMTP, IMAP mailbox (for the Mailbox Trigger; use an app password for Gmail/Outlook), REST API, MCP server, Anthropic, OpenAI, Gemini, OpenAI-compatible): an engineer or client admin enters the credentials under **Integrations -> Catalog**. They are encrypted before storage and tested immediately.
- **OAuth providers** (Gmail, Google Calendar/Drive/Sheets/Docs, Outlook, Microsoft 365, Salesforce, LinkedIn): create an OAuth app with the provider, register the redirect URI `<WAE_PUBLIC_BASE_URL>/api/v1/integrations/oauth/callback`, and set `WAE_OAUTH_<GROUP>_CLIENT_ID` / `_CLIENT_SECRET`. Until then these providers show **Awaiting platform credentials**.
- **AI**: connect an AI provider per organization, or set a platform key. The default model is `claude-opus-5-5`; change it per organization, per agent or per node. Enter prices for non-Anthropic models under **Settings -> Platform -> Model prices**, otherwise their cost is shown as unknown rather than guessed.
- **Inbound triggers**: each webhook-type trigger gets a unique URL shown in the builder. For Stripe, Meta (WhatsApp) and Slack, store the provider's signing secret on the trigger; for Telegram, pass the generated secret as `secret_token` to `setWebhook`.
- **Stripe billing**: set the two Stripe variables, give each sellable plan a Stripe price id (Usage & Billing -> Plans -> Edit), and point a Stripe webhook at `<WAE_PUBLIC_BASE_URL>/api/v1/billing/webhooks/stripe`. Without this, client upgrade requests are recorded for staff and nobody is charged.

## Operations

```bash
python -m app.cli generate-keys             # new secret + vault key
python -m app.cli create-admin EMAIL        # password from WAE_ADMIN_PASSWORD or prompt
python -m app.cli worker                    # dedicated queue worker
python -m app.cli rotate-keys               # re-wrap all stored secrets under WAE_ACTIVE_KEY_ID
python -m app.cli retention                 # apply the data retention policy now (workers do it hourly)
python -m app.cli export-openapi PATH
```

**Data retention:** how long execution history, events, usage detail, notifications, queue jobs and audit entries are kept is set under Settings -> Platform; see WAE_DATABASE_SCHEMA.md.

**Vault key rotation:** add a new key to `WAE_MASTER_KEYS`, set `WAE_ACTIVE_KEY_ID` to it, restart, run `rotate-keys`, then remove the old key. Losing every master key makes stored credentials unrecoverable: back the keys up separately from the database.

Health checks: `GET /api/v1/health` (liveness), `GET /api/v1/health/ready` (database).

## Deployment checklist

- [ ] `WAE_ENV=production`; `WAE_SECRET_KEY` and `WAE_MASTER_KEYS` generated, stored in a secret manager, backed up separately from the database
- [ ] PostgreSQL with separate owner and application roles; the application role is not a superuser and has no `BYPASSRLS`
- [ ] `alembic upgrade head` succeeded, including the Row Level Security migrations; the cross-tenant check in WAE_SECURITY.md returns 0 rows as the application role
- [ ] HTTPS in front of the web service; `WAE_COOKIE_SECURE=true`; public URLs set
- [ ] `WAE_EMBEDDED_WORKER=false` on the API and at least one dedicated worker running
- [ ] Bootstrap admin created, then the bootstrap variables removed and the password changed; two-factor sign-in set up (required for staff by default)
- [ ] Outbound egress filtering in place (defence in depth for SSRF); `WAE_ALLOW_PRIVATE_NETWORK` left `false` unless required
- [ ] With several API instances, optionally a gateway limit for the general per-client budget (sign-in, code and hook limits are already shared through the database; the general budget is per process)
- [ ] Backups running and a restore rehearsed; `./backups` shipped off-host
- [ ] Sentry DSN and log shipping configured
- [ ] Plans, prices and limits reviewed (seeded values are placeholders); model prices entered for non-Anthropic models
- [ ] OAuth apps, Stripe and SMTP configured as needed and verified with real accounts
- [ ] One workflow per critical integration run end to end against the real provider
