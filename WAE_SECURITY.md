# WAE Security

What is implemented, where, and how it is verified. "Tested" means an automated test in `backend/tests/` exercises it.

## Authentication and sessions
| Control | Implementation | Verified |
|---|---|---|
| Password storage | Argon2id (`argon2-cffi`); minimum 10 characters with an uppercase letter and a digit | Tested |
| Sessions | Server-side row per session + signed token; revocable; 12 h default | Tested (logout revokes) |
| Cookies | `HttpOnly`, `SameSite=Lax`, `Secure` when `WAE_COOKIE_SECURE=true` | Code |
| CSRF | Cookie-authenticated mutations need the per-session token in `X-WAE-CSRF` | Tested |
| Brute force | Per-IP and per-email login rate limit; lock for 15 min after 8 failures; generic error; constant-time dummy hash for unknown accounts | Tested |
| Login history, session list, remote sign-out | `/auth/login-history`, `/auth/sessions` | Code |
| Password change | Revokes all other sessions | Code |
| Password reset | Single-use link, 1 hour, only a SHA-256 of the token stored; a newer link supersedes older ones; the request endpoint answers identically whether or not the account exists; completing it revokes all sessions and clears lockout | Tested |
| Staff-issued reset links | For when email is not configured. `platform.users.manage` only (client admins cannot reset other accounts, because a person can belong to several organizations); only a Super Admin can issue one for a Super Admin; audited | Tested |
| Invitations | Single-use link, 7 days. Invited accounts cannot sign in until accepted. Accepting activates only the inviting organization's membership. An invitation never overwrites an existing password: for an account that already has one, accepting requires it. Resending, disabling or removing the member invalidates the old link; an invited member cannot be switched to active without accepting | Tested |

Residual risk: when platform email is not configured, admins receive the invitation link to pass on. Whoever holds a link for an account that has no password yet can choose that password. The link is shown once and expires in 7 days; configure SMTP so links go straight to the invited person.

| Two-factor sign-in (TOTP) | RFC 6238, 6 digits, 30 s, one step of clock drift. Secret is a vault envelope bound to the user. A code is accepted once (last used step is stored). 10 single-use recovery codes, stored as SHA-256 hashes. Between password and code the browser holds a 5-minute signed challenge token, not a session; it is bound to the password hash, so a password change voids it | Tested |
| Staff requirement | `security.staff_mfa_required` (default on, initial value from `WAE_STAFF_MFA_REQUIRED`). Staff without it are walked through setup at sign-in, including right after accepting an invitation; staff under the requirement cannot turn it off. Client users can opt in | Tested |
| Code brute force | Wrong codes count towards the same 8-attempt lockout as wrong passwords; a correct password does not reset the counter while the code is still outstanding | Tested |
| Lost phone | Staff with `platform.users.manage` can reset someone's two-factor sign-in (Super Admin rules apply; not their own). It signs the person out everywhere, notifies them and is audited. Using a recovery code also notifies the account holder | Tested |
| Password reset | Does not bypass two-factor sign-in | Tested |

Residual risk: required set-up happens at the first sign-in after the requirement applies, so whoever first signs in with a staff member's password enrolls the authenticator (trust on first use). Invite staff by invitation so they choose their own password, and reset two-factor sign-in if an account may have been used by someone else.

Not implemented: SSO/OIDC login, WebAuthn / passkeys.

## Authorization
- **RBAC**: nine roles, 35 permissions, checked by a dependency on every endpoint. Tested per role, including that clients cannot reach platform administration.
- **Privilege escalation**: only a Super Admin can create or modify a Super Admin; client roles cannot be granted `platform.*` permissions; nobody can change their own role or disable themselves; an organization always keeps one active Client Admin; organization status and operating mode can only be changed by staff. Tested.
- **Agency vs SaaS**: client admins can install templates only when the organization is in SaaS mode. Tested.

## Tenant isolation
1. **API layer (tested).** The tenant comes from `X-Organization-Id` only after membership is verified. Every object lookup verifies `organization_id` (`api/common.py: owned`). Foreign tenants and foreign object ids both return 404, so existence is not revealed. Tests cover header spoofing, IDOR on workflows, executions and integrations, list scoping, and using another tenant's integration id inside a workflow.
2. **Database layer (not yet verified).** PostgreSQL Row Level Security with `FORCE`, driven by a per-transaction setting. Written in migration `0002`; never executed against PostgreSQL here.
3. **Cryptographic binding.** Each ciphertext is authenticated with its organization id, so an encrypted credential copied to another tenant's row cannot be decrypted. Tested.

**Before onboarding a second client on PostgreSQL, verify layer 2**, for example:

```sql
-- as the application role (not a superuser)
BEGIN; SELECT set_config('app.current_org_id', '<org A id>', true);
SELECT count(*) FROM workflows WHERE organization_id = '<org B id>';   -- must be 0
INSERT INTO workflows (id, organization_id, ...) VALUES (..., '<org B id>', ...);  -- must fail
ROLLBACK;
BEGIN; SELECT count(*) FROM workflows;  -- no settings: must be 0
ROLLBACK;
```

## Credential vault (`core/vault.py`)
- Envelope encryption: a random 256-bit data key per secret (AES-256-GCM), wrapped by a key-encryption key from a `KeyProvider`. The default provider reads keys from `WAE_MASTER_KEYS`; a cloud KMS can implement the same two-method interface.
- Rotation re-wraps data keys under a new key-encryption key (`python -m app.cli rotate-keys`). Tested, including retiring the old key.
- Covers integration credentials, OAuth tokens, PKCE verifiers, webhook signing secrets and notification channel targets.
- The API never returns a secret. Responses expose only `has_credentials`. Webhook secrets generated by the platform are shown exactly once. Tested: the token does not appear in any API response, in the database in clear text, or in execution logs.
- Secrets are not placed in AI prompts by the platform. (A workflow author can still template a value into a prompt; credentials themselves are not addressable from templates.)

## Redaction (`core/redact.py`)
Step inputs/outputs, audit metadata, event payloads and all log lines pass through redaction: sensitive key names are masked, known token formats are scrubbed, and the exact secret values used by a node are removed from what it stores. Tested.

## Outbound requests - SSRF (`core/http.py`)
All outbound HTTP (workflow nodes, agent tools, adapters, notifications, MCP) goes through one client that allows only http/https, resolves the host and rejects loopback, private, link-local, reserved and non-global addresses, forbids credentials in URLs, validates every redirect hop and drops auth headers on cross-host redirects, and caps responses at 5 MB. Tested for the metadata address and localhost.
Residual risk: DNS rebinding between validation and connection. Use egress filtering in production. `WAE_ALLOW_PRIVATE_NETWORK=true` disables the private-address check globally and should stay off unless local model servers require it.

## Workflow and agent safety
- Workflows are data. Only registered node types can be published or run; an unknown type fails validation. Tested.
- No `eval`, no user code, no shell. The template language is path lookup plus whitelisted filters; regular expressions are length-limited (Python's `re` has no timeout, so pathological patterns remain a theoretical risk for authors with workflow-edit rights).
- Agents can act only through nine controlled tools, each attached explicitly with scoped configuration. A tool the model invents is refused. Tested, including the domain allowlist and method restriction of the HTTP tool and single-tool MCP allowlisting.
- High-impact tools default to human approval; nothing is sent before approval and a rejection is reported to the model. Tested.
- Every resource referenced by an agent tool is checked to belong to the same organization when the tool is attached and again when it runs.

## Inbound webhooks
- Unguessable per-trigger URLs; 1 MB body limit; per-endpoint and per-IP rate limits.
- Signature schemes: HMAC-SHA256 over `timestamp.body` with a 5-minute window, shared secret header, Stripe, Meta (`X-Hub-Signature-256`), Telegram secret token, Slack. Constant-time comparison. An endpoint with a scheme but no stored secret rejects everything.
- Replay protection through the timestamp window and a unique de-duplication key per endpoint.
- Tested: unsigned, wrong secret, tampered body, stale timestamp, duplicate delivery, rate limit, unknown endpoint, Stripe scheme.
- Form triggers are intentionally unauthenticated public endpoints (rate-limited). There is no CAPTCHA.

## API hardening
- Request bodies are Pydantic models that reject unknown fields; ids are parsed as UUIDs; list sizes are capped. Tested.
- All database access goes through SQLAlchemy with bound parameters. The PostgreSQL integration adapter builds statements with `psycopg.sql` identifiers and bound values.
- One error envelope; unhandled exceptions return a generic 500 with a request id and are logged server-side. Tested.
- Security headers on API responses (`nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, restrictive CSP, HSTS when cookies are secure) and on frontend pages.
- CORS is limited to configured origins.
- Rate limiting is per process (see WAE_ARCHITECTURE.md).

## Audit
Sensitive operations write `audit_logs` in the same transaction as the change: authentication events, user/role/setting changes, organization and member changes, workflow create/publish/activate/delete, execution cancel/retry/resume, agent and tool changes, integration lifecycle, webhook secret operations, template and package operations, billing and plan changes, approvals. Tested for the workflow lifecycle and the end-to-end journey.

## Known gaps
- Row Level Security unverified on PostgreSQL (above).
- No SSO or passkeys.
- Rate limiting is not shared across API instances.
- No automated dependency or container scanning is configured.
- The frontend has no Content-Security-Policy header (the API has one).
- No penetration test has been performed.
