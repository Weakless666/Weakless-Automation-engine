# WAE API

REST, JSON, versioned under `/api/v1`. The complete machine-readable reference is generated from the code:

- live: `GET /api/openapi.json` and interactive docs at `/api/docs`
- committed snapshot: [docs/openapi.json](docs/openapi.json) (127 paths, 158 operations) - regenerate with `python -m app.cli export-openapi ../docs/openapi.json`

Request and response bodies are both typed in OpenAPI (198 schemas). Every operation names its success model (for example `WorkflowDetail`, `Page_ExecutionOut_`) and documents 400/401/402/403/404/409/422/429/500 as `ErrorEnvelope`. Response models list the fields a response can contain; a field the server did not set is omitted rather than sent as `null`, and models allow additional fields, so clients should ignore unknown keys. `backend/tests/test_openapi.py` fails the build if an endpoint is added without a typed response.

## Conventions

**Authentication.** `POST /api/v1/auth/login` with `{email, password}` returns `{token, csrf_token, user}` and sets an HttpOnly session cookie.
- API clients: send `Authorization: Bearer <token>`.
- Browsers: the cookie authenticates; every non-GET request must also send `X-WAE-CSRF: <csrf_token>`.

**Tenant.** Tenant-scoped endpoints require `X-Organization-Id: <uuid>`. Access is verified server-side; an organization the caller cannot access returns 404. Staff may omit the header on endpoints marked *platform-wide* to query across organizations.

**Errors.**
```json
{"error": {"code": "validation_error", "message": "Request validation failed", "details": [...], "request_id": "..."}}
```
Common codes: `unauthorized` 401, `forbidden` / `csrf_failed` 403, `not_found` 404, `conflict` / `not_published` / `stale_draft` 409, `validation_error` 422, `quota_exceeded` 402, `rate_limited` 429.

**Pagination.** List endpoints take `limit` (1-200, default 50) and `offset`, and return `{items, total, limit, offset}`.

**Idempotency.** `POST /workflows/{id}/execute` honours an `Idempotency-Key` header: repeating the call returns the same execution. Inbound events are de-duplicated per endpoint.

**Rate limits.** 300 requests/minute per client by default; 20/minute for login; per-endpoint limits on inbound hooks.

## Endpoints

### Auth and account
| Method | Path | Notes |
|---|---|---|
| POST | `/auth/login`, `/auth/logout` | |
| GET, PATCH | `/auth/me` | User, organizations with role and permissions, CSRF token |
| POST | `/auth/change-password` | Revokes other sessions |
| GET, DELETE | `/auth/sessions`, `/auth/sessions/{id}` | |
| GET | `/auth/login-history` | |
| POST | `/auth/password-reset/request` | **Public.** `{email}`. Always `202`; emails a link when the account exists and SMTP is configured. `email_delivery` says whether email is set up at all |
| POST | `/auth/password-reset/confirm` | **Public.** `{token, new_password}` |
| POST | `/auth/invitations/inspect` | **Public.** `{token}` -> email, organization, role, whether a password must be chosen |
| POST | `/auth/invitations/accept` | **Public.** `{token, password, full_name?}`. Starts a session like `/auth/login` |

Invalid, used or expired links return `400` with code `invalid_token`.

**Two-factor sign-in.** When it applies, `/auth/login` and `/auth/invitations/accept` return `{mfa_required: true, mfa_token, email}` or `{mfa_setup_required: true, mfa_token, email}` instead of a session. `mfa_token` is valid for 5 minutes.

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/mfa/verify` | **Public.** `{mfa_token, code}`; `code` is a 6-digit code or a recovery code. Starts a session |
| POST | `/auth/mfa/setup`, `/auth/mfa/setup/confirm` | **Public.** Required set-up at sign-in: `{mfa_token}` returns `{secret, otpauth_url}`; confirm with `{mfa_token, code}` returns a session plus `recovery_codes` |
| GET | `/auth/mfa` | Own status: enabled, required, recovery codes left |
| POST | `/auth/mfa/enroll`, `/auth/mfa/enroll/confirm` | Turn it on while signed in |
| POST | `/auth/mfa/recovery-codes` | `{code}`: replace all recovery codes |
| POST | `/auth/mfa/disable` | `{password, code}`; refused for staff while required |

### Platform administration (staff)
| Method | Path | Permission |
|---|---|---|
| GET, POST, PATCH | `/users`, `/users/{id}` | `platform.users.manage`. POST without `password` sends an invitation |
| POST | `/users/{id}/invitation`, `/users/{id}/reset-link` | `platform.users.manage`. Return `{url, expires_at[, email_sent]}` |
| POST | `/users/{id}/mfa-reset` | `platform.users.manage`. Switch off someone's two-factor sign-in (lost phone) and sign them out |
| GET, PUT | `/roles`, `/roles/{key}` | read: staff; write: `platform.settings.manage` |
| GET, PUT | `/settings`, `/settings/{key}` | `platform.settings.manage` |
| GET, POST | `/dead-letters`, `/dead-letters/{id}/requeue` | `platform.analytics.read`, `executions.manage` |

### Organizations
| Method | Path | Notes |
|---|---|---|
| GET, POST | `/organizations` | List is scoped to the caller; create needs `platform.organizations.create` |
| GET, PATCH, DELETE | `/organizations/{id}` | Delete archives (soft) |
| PUT | `/organizations/{id}/client` | Client profile |
| GET, POST, PATCH, DELETE | `/members`, `/members/{id}` | POST without `password` invites new people (`status: invited`, response includes `invitation`); existing accounts join directly |
| POST | `/members/{id}/invitation` | Issue a new invitation link; the old one stops working |
| GET, POST, DELETE | `/workspaces`, `/workspaces/{id}` | |

### Workflows
| Method | Path | Notes |
|---|---|---|
| GET | `/node-types` | The node registry with JSON Schemas |
| GET, POST | `/workflows` | |
| GET, PATCH, DELETE | `/workflows/{id}` | GET includes the draft definition, validation issues and trigger URLs |
| PUT | `/workflows/{id}/draft` | Save the graph; optional `base_revision` for optimistic concurrency |
| POST | `/workflows/{id}/validate`, `/publish`, `/activate`, `/deactivate` | |
| GET | `/workflows/{id}/versions`, `/versions/{version_id}` | |
| POST | `/workflows/{id}/versions/{version_id}/restore` | Copy a version back into the draft |
| POST | `/workflows/{id}/execute` | Run the published version (202) |
| POST | `/workflows/{id}/test` | Run the draft as a test (202) |

Workflow definition:
```json
{"nodes": [{"id": "trigger", "type": "trigger.webhook", "name": "New order", "config": {}, "settings": {}, "position": {"x": 0, "y": 0}},
           {"id": "notify", "type": "action.send_email", "config": {"integration_id": "...", "to": "{{ trigger.body.email }}", "subject": "Thanks", "body": "..."},
            "settings": {"retry": {"max_attempts": 3, "allow_non_idempotent": true}}}],
 "edges": [{"source": "trigger", "target": "notify", "source_handle": "out"}]}
```
Template scope inside node configuration: `trigger.*`, `nodes.<id>.*`, `input.*` (first upstream output), `vars.*`, `loop.item` / `loop.index`, `execution.*`, `now`. Filters: `default`, `json`, `upper`, `lower`, `trim`, `string`, `number`, `length`, `first`, `last`, `join`, `truncate`, `multiply`, `add`.

### Executions and approvals
| Method | Path | Notes |
|---|---|---|
| GET | `/executions` | Filters: `status`, `workflow_id`, `start`, `end`, `include_tests`. Platform-wide for staff. |
| GET | `/executions/{id}` | Includes steps, waiting nodes, generated documents |
| POST | `/executions/{id}/cancel`, `/retry`, `/resume` | `executions.manage` |
| GET | `/approvals` | Filter `status` |
| POST | `/approvals/{id}/decide` | `{approved, note}` |

Execution statuses: `PENDING`, `QUEUED`, `RUNNING`, `WAITING`, `RETRYING`, `COMPLETED`, `FAILED`, `CANCELLED`.

### AI agents and knowledge
| Method | Path |
|---|---|
| GET | `/agents/meta` (agent types, providers, models, tool catalog) |
| GET, POST | `/agents` |
| GET, PUT, DELETE | `/agents/{id}` |
| POST, PUT, DELETE | `/agents/{id}/tools`, `/agents/{id}/tools/{tool_id}` |
| POST | `/agents/{id}/chat` |
| GET | `/agents/{id}/executions`, `/agents/{id}/conversations`, `/agent-executions/{id}`, `/conversations/{id}/messages` |
| GET, POST, DELETE | `/knowledge-bases`, `/knowledge-bases/{id}` |
| GET, POST, DELETE | `/knowledge-bases/{id}/documents`, `/documents/{doc_id}` |
| POST | `/knowledge-bases/{id}/search` |

### Integrations
| Method | Path | Notes |
|---|---|---|
| GET | `/integrations/providers` | Catalog; `available: false` with a reason when platform OAuth credentials are missing |
| GET, POST | `/integrations` | Create stores encrypted credentials and tests the connection |
| GET, PATCH, DELETE | `/integrations/{id}` | |
| POST | `/integrations/{id}/test`, `/revoke`, `/oauth/start` | |
| GET | `/integrations/oauth/callback` | Provider redirect target |
| GET | `/integrations/{id}/mcp-tools` | Discover tools on an MCP server |
| GET | `/integrations-health` | |

Integration statuses: `awaiting_credentials`, `pending`, `connected`, `error`, `revoked`.

### Triggers and events
| Method | Path | Notes |
|---|---|---|
| GET, POST | `/hooks/{endpoint_key}` | **Public.** Inbound trigger URL |
| GET, POST | `/hooks/resume/{execution_id}/{token}` | **Public.** Resume a Wait node |
| GET | `/webhooks` | Endpoints of the organization |
| POST | `/webhooks/{id}/rotate-secret` | Returns the new secret once |
| PUT | `/webhooks/{id}/secret` | Store a provider-issued secret |
| GET | `/webhooks/{id}/verify-token` | Meta subscription token |
| GET, POST | `/events` | History; POST emits a custom application event |
| GET | `/events/{id}` | |
| POST | `/events/{id}/replay` | |

Signing a request for the default `hmac_sha256` scheme:
```
X-WAE-Timestamp: <unix seconds>
X-WAE-Signature: sha256=<hex HMAC-SHA256(secret, timestamp + "." + raw_body)>
X-WAE-Event-Id: <optional id used for de-duplication>
```

### Templates and industry packages
| Method | Path |
|---|---|
| GET, POST | `/templates` |
| GET, PATCH | `/templates/{id}` |
| POST | `/templates/from-workflow` |
| POST | `/templates/{id}/install` |
| GET | `/installed-templates` |
| PUT, POST, DELETE | `/installed-templates/{id}/config`, `/update`, `/installed-templates/{id}` |
| GET, POST | `/industry-packages` |
| GET, PUT | `/industry-packages/{id}` |
| POST | `/industry-packages/{id}/install` |

### Analytics, usage, billing
| Method | Path | Notes |
|---|---|---|
| GET | `/analytics/overview`, `/timeseries`, `/workflows`, `/ai`, `/integrations` | Filters `start`, `end`, `workflow_id`, `status`. Platform-wide for staff. |
| GET | `/analytics/system` | Queue and database health (staff) |
| GET | `/usage`, `/usage/history` | |
| GET, POST | `/billing/plans` | |
| PUT | `/billing/plans/{id}` | |
| GET | `/billing/subscription`, `/billing/events` | |
| POST | `/billing/change-plan`, `/billing/cancel` | `change-plan` returns `applied`, `scheduled`, `requested` or `checkout` |
| POST | `/billing/webhooks/stripe` | **Public**, Stripe-signed |

### Notifications and audit
| Method | Path |
|---|---|
| GET | `/notifications` |
| POST | `/notifications/{id}/read`, `/notifications/read-all` |
| GET, PUT | `/notifications/preferences` |
| GET, POST | `/notification-channels` |
| PUT, DELETE | `/notification-channels/{id}` |
| POST | `/notification-channels/{id}/test` |
| GET | `/notification-deliveries` |
| GET | `/audit-logs` (filters `action`, `actor`, `start`, `end`; platform-wide for staff) |

### System
`GET /health`, `GET /health/ready`.
