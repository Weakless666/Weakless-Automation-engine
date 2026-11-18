# WAE API

REST, JSON, versioned under `/api/v1`. The complete machine-readable reference is generated from the code:

- live: `GET /api/openapi.json` and interactive docs at `/api/docs`
- committed snapshot: [docs/openapi.json](docs/openapi.json) (128 paths, 159 operations) - regenerate with `python -m app.cli export-openapi ../docs/openapi.json`

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
| GET, PUT | `/settings`, `/settings/{key}` | `platform.settings.manage`. `data.retention` takes `{execution_details_days, executions_days, events_days, usage_detail_days, notifications_days, queue_jobs_days, audit_days}`: whole days, or `null` to keep forever (422 on invalid values) |
| POST | `/retention/run` | `platform.settings.manage`. Applies the retention policy now and returns `{policy, result}` with the rows affected per category |
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
| GET | `/executions/{id}/stream` | Server-Sent Events (`text/event-stream`). Event `execution`: the same body as the plain read, sent at once and again on every change. Event `end`: `{"reason": "finished"}` when the execution reaches a final status, or `{"reason": "recycle"}` after 10 minutes (reconnect). Comment lines every 15 s keep the connection open. |
| POST | `/executions/{id}/cancel`, `/retry`, `/resume` | `executions.manage`. `/retry?from_failed_step=true` continues at the failed step on the same version, keeping earlier results (the execution's `can_resume` says whether that is possible) |

Webhook and form triggers accept `response_mode: "respond_step"` (with `response_timeout_seconds`, 1-25): the request waits for a Respond to Webhook step and returns its status, body and content type; without a reply in time it gets the usual `202 {status, execution_id}`.
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
| GET | `/teams` (`agents.read`): agents installed as a team, grouped by module, with 7-day runs, delegations, cost and content KPIs |

Agent tools added for teams: `delegate_to_agent` (config `agent_id`, `description`; runs that member synchronously in a fresh conversation; at most 2 levels deep, never back up the chain), `content_calendar_add` (config `channels` allowlist; always creates `awaiting_approval`), `content_calendar_list`, `knowledge_note` (config `knowledge_base_id`).

### Content calendar
| Method | Path | Permission |
|---|---|---|
| GET, POST | `/content` (filters `start`, `end`, `status`, `channel`) | `content.read` / `content.write` |
| GET, PATCH, DELETE | `/content/{id}` (edit only before approval; published items cannot be deleted) | `content.read` / `content.write` |
| POST | `/content/{id}/approve`, `/content/{id}/reject` (`{note}`) | `content.approve` |
| POST | `/content/{id}/outcome` (`{status: published|failed, url, note}`) | `content.write` |

An approved item emits the `content.due` application event at its scheduled time (immediately if unscheduled or overdue). `content.*` and `workflow.*` event types are reserved for the platform. The "Update Content Item" workflow step records the publishing outcome.

Packages: `GET /industry-packages/{id}` includes `team` (members and the install form) for packages with an AI team; installing such a package requires that form in `config` and returns `team.agents`.

### Publishing and Creative Studio
| Method | Path | Notes |
|---|---|---|
| GET | `/content/publishing` | `content.read`. Every channel: `native` (can the platform publish to it), accepted `providers`, and the connected `integration_id` / name / status |
| PUT | `/content/publishing` | `content.approve`. `{channel, integration_id}`; `null` disconnects. 422 if the integration is of another network or the channel has no native publishing |
| POST | `/content/{id}/publish` | `content.approve`. Publishes an approved or failed item now through the connected account. 200 with the item: `published` and `published_url`, or `failed` with the reason in `decision_note`. 409 without a connected account or while it is already being published |
| GET | `/studio/meta` | `content.read`. Media providers, whether each has credentials, suggested models, aspects and video lengths |
| GET | `/studio/generations` | Paged; filter `kind` |
| POST | `/studio/generations` | `content.write`. `{kind: image/video, prompt, aspect, seconds, provider?, model?}`. 202 with the queued request; 422 when no provider can make it |
| GET, DELETE | `/studio/generations/{id}` | `status` queued/running/completed/failed, `error`, and `file` (a file reference) once completed. Delete also removes the file unless `delete_file=false`; 409 while the file is attached to a planned post |
| GET | `/public/files/{id}?exp=&sig=` | No sign-in: a signed, expiring link to one image or video, created by the platform for networks that fetch media themselves |

Content items accept and return `media`: on write a list of up to four file ids (images or videos), on read file references. With a connected channel an approved item is published automatically at `scheduled_for` (or within seconds without one) and the events `content.published` / `content.failed` are emitted; without one `content.due` is emitted as before.

Workflow steps: Publish Social Post (`action.publish_post`: integration, text, link, media, optional target) and Generate Image (`ai.generate_image`: prompt, aspect, provider, model; output `file`).

### Files
| Method | Path | Notes |
|---|---|---|
| GET | `/files` | `files.read`. Filters `q` (name), `source` (`upload`, `workflow`, `mailbox`); paged; also returns `storage` (`used_bytes`, `limit_bytes`, `max_file_bytes`) |
| POST | `/files` | `files.write`. `multipart/form-data` with one `file` part. 413 over the per-file limit, 409 when the organization's storage is full |
| GET | `/files/{id}` | Metadata |
| GET | `/files/{id}/content` | The bytes, always as an `application/octet-stream` attachment |
| DELETE | `/files/{id}` | `files.write` |

In workflow data a file is a reference `{file_id, name, content_type, size}`. Steps that take a file accept that reference (for example `{{steps.download.file}}`) or a file id copied from the Files page.

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
