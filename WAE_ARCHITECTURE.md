# WAE Architecture

This document describes what is implemented in this repository. Where something is designed but unverified, or deliberately not built, it says so.

## Starting point

The repository was empty (a `.gitattributes` file and no commits), so there was no existing stack, schema, auth or deployment to preserve. Everything here is new.

## System overview

```
Browser ──> Next.js (web)  ──/api/* proxy──>  FastAPI (api)  ──>  PostgreSQL
                                                   │                  ▲
 Providers / webhooks ──> /api/v1/hooks/{key} ─────┘                  │
                                                                      │
                                    Worker process(es) ───────────────┘
                                    claim jobs from the execution_queues table,
                                    run the interpreter, schedules and maintenance
```

Four deployable units: `web`, `api`, `worker` (1..n) and PostgreSQL. In development the worker runs as a thread inside the API (`WAE_EMBEDDED_WORKER=true`) and the database is SQLite.

Both operating modes use the same infrastructure. An organization has `mode = agency | saas`. In **agency** mode Weakless staff build and operate everything and client users observe; in **saas** mode client admins may additionally install templates. The difference is one authorization rule, not a second system.

## Technology decisions

| Topic | Decision | Reason |
|---|---|---|
| Backend | FastAPI + SQLAlchemy 2 (sync) + Pydantic 2 | The recommended stack; sync sessions keep transaction and tenant handling simple and predictable. |
| Workflow engine | **Own interpreter** over a controlled node registry | Workflows must be tenant-isolated data validated against typed schemas, with engine-enforced retry/idempotency rules. This is the product's core, so it is owned rather than delegated. |
| n8n | **Not integrated** | It would be a second workflow engine with its own storage, credentials and tenancy model beside ours - the "competing workflow engines" the brief warns against. Integrations are implemented as adapters instead. |
| Temporal | **Not integrated** | Durable waits, retries and crash recovery are covered by persisted interpreter state plus the job queue. Temporal would add a cluster to operate for capabilities already present. Revisit if workflows need sub-second fan-out at very large scale. |
| BullMQ / Redis | **Not used** | BullMQ is a Node.js library; the backend is Python. The queue is a PostgreSQL table claimed with `FOR UPDATE SKIP LOCKED`, which gives transactional enqueue (a job and its execution commit together) with one less service to run. |
| Rate limiting | In-process sliding window | Correct for one API instance. For several instances use a gateway limit or implement the same `allow()` interface on Redis (`app/core/ratelimit.py`). |
| Real-time updates | Polling (TanStack Query intervals) | Execution views refresh every 1-10 s. WebSockets are not implemented. |
| Frontend | Next.js App Router, client-rendered pages behind a session check | An authenticated admin tool: no SEO or server-rendering requirement. `/api` is proxied so the session cookie stays first-party. |
| UI kit | Hand-written shadcn-style primitives on Tailwind tokens | Same approach as shadcn/ui (owned components) without running its generator. |

## Backend modules

### Multi-tenancy and access (`deps.py`, `rbac.py`, `db.py`)
- Every tenant-owned table carries `organization_id`.
- The client names a tenant with `X-Organization-Id`. That is only a request: the API verifies that the user is Weakless staff or an active member, and answers "not found" otherwise. Only then is the database session bound to the tenant.
- Two layers of isolation: (1) every lookup goes through `owned()` or an explicit `organization_id` filter; (2) on PostgreSQL, Row Level Security policies compare `organization_id` with a per-transaction setting. Layer 1 is covered by tests. **Layer 2 has not been executed against a real PostgreSQL** (see the report).
- Roles: six platform roles on `users.platform_role` apply to all organizations; three client roles live on `organization_members`. Permissions are rows in `roles` / `permissions` / `role_permissions`, editable by a Super Admin.

### Workflow model (`engine/graph.py`, `services/workflows.py`)
- A workflow has an editable **draft** (`workflow_nodes`, `workflow_connections`) and immutable **published versions** (`workflow_versions.definition`). Executions always run a published version; editing a draft never affects live runs. Test runs execute a snapshot of the draft.
- Publishing validates the graph: known node types, configuration schemas, valid output handles, at least one trigger, no cycles, loop-body rules.
- Variables (`{{ vars.* }}`) are stored on the workflow and apply to new runs immediately.

### Node registry (`engine/registry.py`, `engine/nodes/`)
45 node types: 9 triggers, 16 actions, 11 logic, 9 AI. Each declares a Pydantic configuration model (also served to the UI as JSON Schema, which generates the forms), an output schema, its output handles and whether it is **idempotent**. Handlers are functions in this codebase; workflow configuration can never introduce code. Templates use a path-and-filter expression language (`engine/expressions.py`) with no `eval`.

### Execution engine (`engine/executor.py`)
An interpreter that persists its state after every node.

- **Branching:** each edge becomes `taken` or `skipped`. A node runs when all incoming edges are resolved and at least one was taken; otherwise it is skipped and the skip propagates. This yields if/else, switch, router and merge semantics on a DAG.
- **Loops:** the Loop node runs the sub-graph on its `each` output once per item, then continues from `done`.
- **Waiting:** Delay, Wait (external signal) and Human Approval return a wait instruction. The execution becomes `WAITING`, a timer job is queued where relevant, and any worker can resume it later - including after a restart.
- **Failure:** an error follows the node's `error` output if connected, otherwise fails the execution. Retries (exponential back-off with jitter, scheduled through the queue) happen only for transient errors on nodes that are safe to repeat, or when the engineer explicitly allows it per node.
- **Crash recovery:** the node in flight is recorded before it runs. If a worker dies, the recovered job re-runs an idempotent node but fails the execution safely on a non-idempotent one, so an email or charge is never silently repeated.
- **Controls:** idempotency keys, cancellation, an active-time limit per execution, per-organization concurrency limits, monthly quotas.

Known limit: a node handler cannot be forcibly interrupted. Time limits are enforced between nodes and through HTTP/AI client timeouts.

### Queue and worker (`engine/queue.py`)
Jobs are rows in `execution_queues`. Workers claim them atomically, heartbeat implicitly through a visibility timeout, retry infrastructure failures with back-off, and move jobs to `DEAD` (the dead-letter queue) after `max_attempts`. The same loop runs maintenance: stale-job recovery, cron schedules, approval expiry and billing period roll-over. Multi-worker claiming on PostgreSQL is implemented but only the single-worker path is exercised by tests.

### Triggers and events (`services/webhooks.py`, `services/events.py`, `services/scheduler.py`)
- Endpoint-backed triggers (webhook, form, payment, message, email, database event) get an unguessable URL. Requests are size-limited, rate-limited per endpoint, verified (HMAC with timestamp, shared secret, Stripe, Meta, Telegram or Slack schemes), de-duplicated and recorded in `webhook_events`; events can be replayed.
- Schedule triggers use cron expressions with time zones; each tick is consumed with a compare-and-swap so it fires once.
- Application events: lifecycle events (`workflow.completed`, `workflow.failed`) and custom events emitted through the API start subscribed workflows, with a guard against feedback loops.
- Email and database triggers are webhook-based (an inbound-parse service or database webhooks call the URL). Mailbox polling and native database listeners are not implemented.

### AI (`ai/`)
- `providers.py` normalizes Anthropic (official SDK), OpenAI, OpenAI-compatible servers and Gemini behind one request/response shape. Nothing else in the codebase imports a vendor SDK.
- `service.py` resolves credentials (organization integration, then platform key), applies a provider fallback chain, enforces the AI quota, meters tokens and estimates cost from an editable price table.
- `agents.py` is a persisted tool-calling loop with conversation history, per-agent limits and human approval for selected tools.
- `tools.py` is the only way an agent can act: nine controlled tools, each scoped by configuration (which integration, which domains, which workflow, which MCP tool). There are no shell, filesystem, database or payment tools.
- `mcp.py` is a Streamable HTTP MCP client (initialize, tools/list, tools/call). MCP tools are allowlisted one by one.
- `knowledge.py` provides BM25 keyword retrieval. Vector search is not implemented; the `search()` function is the extension point.

### Integrations (`integrations/`)
32 providers in a code-defined catalog, mirrored to `integration_providers`. Adapters call the providers' real APIs through the guarded HTTP client. Requests are pinned to the provider's host: the generic "call API" action accepts a path, never a URL. OAuth uses the authorization-code flow with single-use hashed state and PKCE where supported, with token refresh.

### Templates and packages (`templates/`)
46 templates are generated from 17 reusable workflow patterns; six industry packages are collections of templates. Installing a template deep-copies its definition into a new workflow owned by the organization. Template updates replace only the draft of an installed workflow.

### Billing and usage (`services/usage.py`, `services/billing.py`)
Metered records (`usage_records`) drive quotas, 80%/100% alerts and reports. Plans are editable. A provider abstraction separates manual billing (default; never charges) from Stripe Checkout (only when configured and the plan has a price id). No card data touches the system.

## Frontend

`frontend/app/(app)` holds 15 screens behind a session guard; `components/schema-form.tsx` renders forms from backend JSON Schemas (node configuration, template configuration); `components/builder/` is the React Flow editor. One navigation model is filtered by the user's permissions, so Super Admins, engineers, client admins and client members see different applications without separate codebases.

## Observability

- JSON log lines with a redaction filter; optional Sentry.
- Every execution has an id, a step log with resolved inputs/outputs/errors and timings, and the event that triggered it.
- `audit_logs` for sensitive actions; `/analytics/*` for metrics; `/analytics/system` and `/health/ready` for health.
