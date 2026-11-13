"""Response models: the documented shape of every API response.

Models are permissive (``extra="allow"``): they type-check and document the fields they
declare and pass any additional field through unchanged, so a response can never lose
data by accident. A declared field with the wrong type fails loudly (HTTP 500) and is
caught by the test suites.

Timestamps are ISO 8601 strings in UTC. Ids are UUID strings.
"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")
ISO = str | None


class Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class Page(Out, Generic[T]):
    items: list[T]
    total: int = Field(description="Total matching items, across all pages")
    limit: int
    offset: int


class Items(Out, Generic[T]):
    items: list[T]


class Ok(Out):
    ok: bool


class ErrorBody(Out):
    code: str = Field(description="Machine-readable error code, e.g. not_found, validation_error, quota_exceeded")
    message: str = Field(description="Human-readable message, safe to show to the user")
    request_id: str | None = Field(None, description="Quote this when reporting a problem")
    details: Any = None


class ErrorEnvelope(Out):
    error: ErrorBody


# ----------------------------------------------------------------------------- system


class Health(Out):
    status: str
    version: str | None = None


# ----------------------------------------------------------------------------- auth & users


class UserOut(Out):
    id: str
    email: str
    full_name: str
    platform_role: str | None = Field(description="Weakless staff role; null for client users")
    is_active: bool
    invitation_pending: bool = False
    mfa_enabled: bool = False
    last_login_at: ISO = None
    created_at: ISO = None


class IssuedLinkOut(Out):
    url: str = Field(description="Single-use link to give the person")
    expires_at: str
    email_sent: bool | None = Field(None, description="Whether the platform emailed the link (false when SMTP is not configured)")


class StaffCreated(UserOut):
    invitation: IssuedLinkOut | None = None


class LoginResult(Out):
    """Either a session (``token``) or the two-factor step (``mfa_required`` / ``mfa_setup_required``)."""

    token: str | None = Field(None, description="Session token for API clients (browsers use the HttpOnly cookie)")
    csrf_token: str | None = Field(None, description="Send as X-WAE-CSRF on cookie-authenticated mutations")
    expires_at: ISO = None
    user: UserOut | None = None
    mfa_required: bool | None = None
    mfa_setup_required: bool | None = None
    mfa_token: str | None = Field(None, description="5-minute challenge token for the /auth/mfa/* endpoints")
    email: str | None = None


class SessionWithRecoveryCodes(LoginResult):
    recovery_codes: list[str]


class MfaStatus(Out):
    enabled: bool
    required: bool
    enabled_at: ISO = None
    recovery_codes_left: int


class MfaStart(Out):
    secret: str = Field(description="Base32 key for manual entry")
    otpauth_url: str = Field(description="otpauth:// URI to render as a QR code")


class RecoveryCodesOut(Out):
    recovery_codes: list[str] = Field(description="Shown once; each code signs in once")


class MfaEnrolled(RecoveryCodesOut):
    status: MfaStatus


class OrgMembership(Out):
    id: str
    name: str
    slug: str
    status: str
    mode: str
    role: str
    permissions: list[str]


class MeOut(Out):
    user: UserOut
    is_staff: bool
    platform_permissions: list[str]
    organizations: list[OrgMembership]
    csrf_token: str
    mfa: MfaStatus


class SessionInfo(Out):
    id: str
    ip: str | None
    user_agent: str | None
    created_at: ISO
    last_seen_at: ISO
    expires_at: ISO
    current: bool


class LoginEntry(Out):
    id: str
    success: bool
    reason: str | None
    ip: str | None
    user_agent: str | None
    created_at: ISO


class ResetRequested(Out):
    status: str
    email_delivery: bool = Field(description="Whether platform email is configured at all")


class InvitationInfo(Out):
    email: str
    full_name: str
    organization_name: str | None
    role: str | None
    invited_by: str | None
    needs_password: bool = Field(description="True for a new account; false when the person must enter their existing password")
    expires_at: str


class RoleOut(Out):
    key: str
    name: str
    scope: str
    permissions: list[str]


class PermissionOut(Out):
    key: str
    description: str


class RolesOut(Out):
    roles: list[RoleOut]
    permissions: list[PermissionOut]


class RoleGrantsOut(Out):
    key: str
    permissions: list[str]


class SettingOut(Out):
    key: str
    value: Any
    description: str | None = None
    updated_at: ISO = None


class SettingsOut(Out):
    items: list[SettingOut]
    defaults: dict[str, Any]
    environment: dict[str, Any] = Field(description="Which integrations are configured. Never contains secret values")


class ContentItemOut(Out):
    id: str
    title: str
    channel: str = Field(description="linkedin, instagram, facebook, tiktok, youtube, x, blog, newsletter, email or other")
    body: str
    media_notes: str
    hashtags: list[str]
    scheduled_for: ISO
    status: str = Field(description="idea, draft, awaiting_approval, approved, published, rejected or failed")
    created_by_agent_id: str | None
    decided_at: ISO
    decision_note: str
    published_at: ISO
    published_url: str | None
    meta: dict[str, Any]
    created_at: ISO
    updated_at: ISO


class TeamMemberOut(Out):
    id: str
    name: str
    description: str
    status: str
    module: str | None
    runs_7d: int
    delegated_7d: int = Field(description="Tasks this member received from another agent in the last 7 days")
    last_run_at: ISO
    tokens_7d: int
    cost_usd_7d: float


class TeamModuleOut(Out):
    name: str
    agents: list[TeamMemberOut]


class TeamOut(Out):
    key: str
    name: str
    lead: TeamMemberOut | None
    modules: list[TeamModuleOut]
    kpis: dict[str, float]


class RetentionRun(Out):
    policy: dict[str, int | None] = Field(description="Days kept per category; null keeps that data forever")
    result: dict[str, int] = Field(description="Rows deleted, cleared or merged per category in this run")


# ----------------------------------------------------------------------------- organizations


class ClientProfile(Out):
    legal_name: str
    contact_name: str
    contact_email: str
    contact_phone: str
    country: str
    timezone: str
    notes: str
    status: str
    account_manager_id: str | None = None


class OrganizationOut(Out):
    id: str
    name: str
    slug: str
    status: str
    mode: str = Field(description="agency (managed by Weakless) or saas (self-serve)")
    industry: str | None
    settings: dict[str, Any]
    created_at: ISO
    member_count: int | None = None
    active_workflows: int | None = None
    client_status: str | None = None


class OrganizationDetail(OrganizationOut):
    client: ClientProfile | None
    role: str = Field(description="The caller's role in this organization")
    permissions: list[str]


class MemberOut(Out):
    id: str
    user_id: str
    email: str
    full_name: str
    role: str
    status: str = Field(description="active, invited or disabled")
    last_login_at: ISO
    created_at: ISO


class MemberCreated(MemberOut):
    invitation: IssuedLinkOut | None = Field(None, description="Present when an invitation was sent instead of adding the account directly")


class WorkspaceOut(Out):
    id: str
    name: str
    slug: str
    is_default: bool
    created_at: ISO


class LocateOut(Out):
    organization_id: str


# ----------------------------------------------------------------------------- workflows & executions


class NodeTypeOut(Out):
    type: str
    category: str
    label: str
    description: str
    icon: str
    config_schema: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handles: list[str]
    dynamic_handles: dict[str, Any] | None
    supports_error_handle: bool
    idempotent: bool = Field(description="Safe to run again automatically after a transient failure")
    providers: list[str]
    high_impact: bool


class Issue(Out):
    level: str = Field(description="error or warning")
    node_id: str | None
    message: str


class TriggerEndpointOut(Out):
    id: str
    node_id: str
    kind: str
    signature_scheme: str
    url: str
    has_secret: bool
    is_active: bool
    rate_limit_per_minute: int
    last_received_at: ISO


class ScheduleOut(Out):
    node_id: str
    cron: str
    timezone: str
    is_active: bool
    next_run_at: ISO
    last_run_at: ISO


class MailboxWatchOut(Out):
    node_id: str
    integration_id: str
    folder: str
    is_active: bool
    poll_minutes: int
    next_poll_at: ISO
    last_polled_at: ISO
    watching: bool = Field(description="True once the first poll has recorded where new mail starts")
    last_error: str | None


class TriggersOut(Out):
    endpoints: list[TriggerEndpointOut]
    schedules: list[ScheduleOut]
    mailboxes: list[MailboxWatchOut] = Field(default_factory=list)


class WorkflowOut(Out):
    id: str
    name: str
    description: str
    status: str
    is_active: bool
    workspace_id: str
    current_version_id: str | None
    draft_revision: int
    variables: dict[str, Any]
    settings: dict[str, Any]
    installed_template_id: str | None
    created_at: ISO
    updated_at: ISO
    executions: int | None = None
    last_execution_at: ISO = None
    version: int | None = None


class WorkflowDetail(WorkflowOut):
    definition: dict[str, Any] = Field(description="The draft graph: {nodes, edges}")
    issues: list[Issue]
    triggers: TriggersOut
    published_version: int | None
    has_unpublished_changes: bool


class DraftSaved(Out):
    draft_revision: int
    issues: list[Issue]
    triggers: TriggersOut


class IssuesOut(Out):
    issues: list[Issue]


class VersionOut(Out):
    id: str
    version: int
    notes: str
    created_at: ISO
    published_by: str | None
    node_count: int
    is_current: bool | None = None


class VersionDetail(VersionOut):
    definition: dict[str, Any]
    variables: dict[str, Any]


class DraftRevision(Out):
    draft_revision: int


class ExecutionOut(Out):
    id: str = Field(description="Traceable execution id")
    workflow_id: str
    workflow_name: str | None = None
    organization_id: str
    organization_name: str | None = None
    status: str = Field(description="PENDING, QUEUED, RUNNING, WAITING, RETRYING, COMPLETED, FAILED or CANCELLED")
    trigger_type: str
    is_test: bool
    error: dict[str, Any] | None
    retry_of_id: str | None
    created_at: ISO
    started_at: ISO
    finished_at: ISO
    duration_ms: int | None


class StepOut(Out):
    id: str
    sequence: int
    node_id: str
    node_type: str
    node_name: str
    iteration: str | None
    status: str
    attempt: int
    input: Any
    output: Any
    error: Any
    started_at: ISO
    finished_at: ISO
    duration_ms: int | None


class WaitingOut(Out):
    node_id: str
    kind: str | None
    resume_at: ISO


class DocumentOut(Out):
    id: str
    name: str
    content_type: str
    content: str


class ExecutionDetail(ExecutionOut):
    version: int | None
    trigger_node: str | None
    trigger_payload: dict[str, Any]
    output: Any
    waiting: list[WaitingOut]
    steps: list[StepOut]
    documents: list[DocumentOut]
    details_purged_at: ISO = Field(None, description="When the step log and payloads were removed by the data retention policy")


class ResumedOut(Out):
    ok: bool
    node_id: str


class DeadLetterOut(Out):
    id: str
    organization_id: str
    execution_id: str | None
    kind: str
    attempts: int
    last_error: str | None
    finished_at: ISO


class ApprovalOut(Out):
    id: str
    kind: str
    status: str
    title: str
    summary: str
    payload: dict[str, Any]
    execution_id: str | None
    agent_execution_id: str | None
    decided_at: ISO
    decision_note: str
    expires_at: ISO
    created_at: ISO


# ----------------------------------------------------------------------------- agents & knowledge


class AgentToolOut(Out):
    id: str
    name: str
    tool_key: str
    config: dict[str, Any]
    requires_approval: bool
    high_impact: bool


class AgentOut(Out):
    id: str
    name: str
    description: str
    agent_type: str
    system_instructions: str
    provider: str
    model: str
    fallbacks: list[dict[str, Any]]
    temperature: float | None
    max_tokens: int
    max_iterations: int
    provider_options: dict[str, Any]
    limits: dict[str, Any]
    knowledge_base_ids: list[str]
    status: str
    team_key: str | None = None
    team_module: str | None = None
    team_lead: bool = False
    created_at: ISO
    updated_at: ISO
    tools: list[AgentToolOut] | None = None
    tool_count: int | None = None


class AgentsMeta(Out):
    agent_types: list[dict[str, str]]
    providers: list[str]
    anthropic_models: list[str]
    default_model: str
    tools: list[dict[str, Any]]


class AgentExecutionOut(Out):
    id: str
    agent_id: str | None
    kind: str
    status: str = Field(description="RUNNING, WAITING_APPROVAL, COMPLETED, FAILED or CANCELLED")
    conversation_id: str | None
    provider: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = Field(description="Estimated; null when the model has no configured price")
    iterations: int
    output_text: str | None
    error: str | None
    trace: list[dict[str, Any]]
    awaiting_approval_id: str | None
    created_at: ISO
    finished_at: ISO


class ConversationOut(Out):
    id: str
    title: str
    external_ref: str | None
    status: str
    created_at: ISO
    updated_at: ISO


class MessageOut(Out):
    id: str
    role: str
    text: str
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    created_at: ISO


class KnowledgeBaseOut(Out):
    id: str
    name: str
    description: str
    documents: int
    chunks: int
    created_at: ISO


class KnowledgeDocumentOut(Out):
    id: str
    title: str
    source: str
    characters: int
    created_at: ISO


class DocumentCreated(Out):
    id: str
    title: str


class SearchHit(Out):
    score: float
    content: str
    document_id: str
    title: str
    source: str


class SearchResults(Out):
    results: list[SearchHit]


# ----------------------------------------------------------------------------- integrations


class ProviderOut(Out):
    key: str
    name: str
    category: str
    auth_type: str
    fields: list[dict[str, Any]]
    scopes: list[str]
    oauth_group: str | None
    actions: list[str]
    setup: str
    docs_url: str
    available: bool = Field(description="False when platform OAuth credentials for this provider are missing")
    unavailable_reason: str | None


class IntegrationOut(Out):
    id: str
    provider_key: str
    provider_name: str
    category: str
    auth_type: str
    name: str
    status: str = Field(description="awaiting_credentials, pending, connected, error or revoked")
    config: dict[str, Any] = Field(description="Non-secret settings only. Credentials are never returned")
    scopes: list[str]
    has_credentials: bool
    oauth_ready: bool | None
    last_checked_at: ISO
    last_ok_at: ISO
    last_error: str | None
    token_expires_at: ISO
    created_at: ISO


class IntegrationTested(Out):
    result: dict[str, Any]
    integration: IntegrationOut


class AuthorizationUrl(Out):
    authorization_url: str


class McpToolOut(Out):
    name: str
    description: str | None = None
    input_schema: dict[str, Any]


class IntegrationHealthItem(Out):
    id: str
    name: str
    provider_key: str
    status: str
    last_checked_at: ISO
    last_ok_at: ISO
    last_error: str | None
    organization_id: str | None = None


# ----------------------------------------------------------------------------- triggers & events


class HookResult(Out):
    status: str = Field(description="processed, duplicate, ignored, rejected or failed")
    event_id: str | None = None
    execution_id: str | None = None
    reason: str | None = None


class StatusOut(Out):
    status: str


class EndpointOut(Out):
    id: str
    workflow_id: str
    node_id: str
    kind: str
    signature_scheme: str
    url: str
    has_secret: bool
    is_active: bool
    rate_limit_per_minute: int
    last_received_at: ISO


class SecretRotated(Out):
    secret: str = Field(description="Shown once")
    endpoint: EndpointOut


class VerifyToken(Out):
    verify_token: str


class EventOut(Out):
    id: str
    endpoint_id: str | None
    source: str
    event_type: str
    status: str
    reason: str | None
    execution_id: str | None
    replay_of_id: str | None
    received_at: ISO
    payload: dict[str, Any] | None = None
    headers: dict[str, Any] | None = None


# ----------------------------------------------------------------------------- templates & packages


class TemplateOut(Out):
    id: str
    key: str
    name: str
    description: str
    category: str
    industry: str | None
    required_integrations: list[str]
    required_credentials: list[str]
    pricing: dict[str, Any]
    status: str
    version: int | None
    updated_at: ISO
    setup_instructions: str | None = None
    definition: dict[str, Any] | None = None
    config_schema: dict[str, Any] | None = None
    is_required: bool | None = None
    position: int | None = None


class TemplatePage(Page[TemplateOut]):
    categories: list[str]


class InstalledTemplateOut(Out):
    id: str
    template_id: str
    template_key: str | None
    template_name: str | None
    category: str | None
    workflow_id: str | None = Field(description="The organization's own workflow created from the template")
    workflow_active: bool
    workflow_published: bool
    version: int | None
    latest_version: int | None
    update_available: bool
    config: dict[str, Any]
    config_schema: dict[str, Any]
    missing_config: list[str]
    status: str
    installed_at: ISO


class InstalledUpdated(Out):
    updated: bool
    installed: InstalledTemplateOut


class PackageOut(Out):
    id: str
    key: str
    name: str
    description: str
    industry: str
    pricing: dict[str, Any]
    status: str
    template_count: int
    template_names: list[str] | None = None
    templates: list[TemplateOut] | None = None
    team: dict[str, Any] | None = Field(None, description="AI team installed with the package: name, members and its install form (config_schema)")


class PackageInstalled(Out):
    installed: list[InstalledTemplateOut]
    team: dict[str, Any] | None = Field(None, description="When the package has an AI team: its agents and playbook")


# ----------------------------------------------------------------------------- analytics


class ExecutionSummary(Out):
    total: int
    completed: int
    failed: int
    running: int
    cancelled: int
    success_rate: float | None
    failure_rate: float | None
    avg_duration_ms: int | None


class AiSummary(Out):
    requests: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    unpriced_requests: int = Field(description="Runs on models without a configured price, excluded from the estimate")


class IntegrationSummary(Out):
    total: int
    connected: int
    error: int
    awaiting_credentials: int
    pending: int
    revoked: int
    uptime_percent: float | None


class ActivityRow(Out):
    id: str
    workflow_id: str
    workflow_name: str
    organization_id: str
    organization_name: str
    status: str
    trigger_type: str
    created_at: ISO
    duration_ms: int | None
    error: str | None


class QueueStats(Out):
    ready: int
    scheduled: int
    claimed: int
    dead: int
    oldest_ready_age_seconds: float


class ClientActivity(Out):
    organization_id: str
    organization_name: str
    executions: int
    failed: int
    last_execution_at: ISO


class OverviewOut(Out):
    range: dict[str, str]
    workflows: dict[str, int]
    executions: ExecutionSummary
    ai: AiSummary
    integrations: IntegrationSummary
    recent_activity: list[ActivityRow]
    recent_errors: list[ActivityRow]
    organizations: dict[str, int] | None = Field(None, description="Platform-wide view only")
    clients: dict[str, int] | None = None
    queue: QueueStats | None = None
    client_activity: list[ClientActivity] | None = None


class DayPoint(Out):
    date: str
    total: int
    completed: int
    failed: int
    avg_duration_ms: int | None
    ai_tokens: int


class WorkflowStats(Out):
    workflow_id: str
    workflow_name: str
    executions: int
    completed: int
    failed: int
    success_rate: float | None
    avg_duration_ms: int | None
    last_execution_at: ISO


class ModelUsage(Out):
    provider: str | None
    model: str | None
    runs: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    failed: int


class AiBreakdown(Out):
    summary: AiSummary
    by_model: list[ModelUsage]


class IntegrationBreakdown(Out):
    summary: IntegrationSummary
    items: list[IntegrationHealthItem]


class SystemHealth(Out):
    database: str
    queue: QueueStats
    queue_status: str = Field(description="ok, degraded or stalled")
    checked_at: ISO


# ----------------------------------------------------------------------------- usage & billing


class Quota(Out):
    used: float
    limit: float | None = Field(description="null means unlimited")
    percent: float | None
    exceeded: bool


class UsageOut(Out):
    period: str = Field(description="YYYY-MM")
    totals: dict[str, float]
    quotas: dict[str, Quota]


class PlanOut(Out):
    id: str
    key: str
    name: str
    description: str
    price_monthly_cents: int
    currency: str
    limits: dict[str, Any]
    features: list[str]
    is_active: bool
    is_public: bool
    provider_price_id: str | None = None
    sort_order: int


class SubscriptionInfo(Out):
    status: str
    provider: str
    current_period_start: ISO
    current_period_end: ISO
    cancel_at_period_end: bool
    pending_plan: str | None


class SubscriptionOut(Out):
    subscription: SubscriptionInfo | None
    plan: PlanOut | None
    quotas: dict[str, Quota]
    payment_provider: str | None = Field(description="stripe when online payment is enabled; null otherwise")


class PlanChangeOut(Out):
    result: str = Field(description="applied, scheduled, requested (sent to staff, nothing charged) or checkout")
    checkout_url: str | None


class CancelOut(Out):
    cancel_at_period_end: bool
    effective: ISO


class BillingEventOut(Out):
    id: str
    type: str
    provider: str
    amount_cents: int | None
    currency: str | None
    data: dict[str, Any]
    created_at: ISO


# ----------------------------------------------------------------------------- notifications & audit


class NotificationOut(Out):
    id: str
    organization_id: str | None
    event_type: str
    severity: str
    title: str
    body: str
    data: dict[str, Any]
    read: bool
    created_at: ISO


class NotificationPage(Page[NotificationOut]):
    unread: int


class UpdatedCount(Out):
    updated: int


class PreferenceOut(Out):
    event_type: str
    label: str
    in_app: bool
    email: bool


class ChannelOut(Out):
    id: str
    kind: str
    name: str
    config: dict[str, Any]
    events: list[str]
    is_active: bool
    has_secret: bool
    created_at: ISO


class ChannelsOut(Out):
    items: list[ChannelOut]
    event_types: list[dict[str, str]]


class DeliveryResult(Out):
    status: str
    error: str | None


class DeliveryOut(Out):
    id: str
    channel_kind: str
    event_type: str
    status: str
    error: str | None
    created_at: ISO


class AuditEntryOut(Out):
    id: str
    organization_id: str | None
    actor_email: str
    action: str
    resource_type: str
    resource_id: str
    meta: dict[str, Any]
    ip: str | None
    created_at: ISO


class FlexibleRow(Out):
    """Free-form row (e.g. a month of usage, keyed by metric)."""


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope, "description": text}
    for code, text in {
        400: "Bad request", 401: "Not signed in, or the session expired", 402: "Plan quota exceeded",
        403: "Not allowed (permission, CSRF, or organization suspended)", 404: "Not found (also returned for other tenants' resources)",
        409: "Conflict with the current state", 422: "Validation failed", 429: "Rate limited", 500: "Unexpected server error",
    }.items()
}
