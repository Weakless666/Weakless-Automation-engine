"""SQLAlchemy models - the complete WAE schema.

Conventions: UUID primary keys, ``organization_id`` on every tenant-owned
table, timezone-aware UTC timestamps, JSONB (JSON on SQLite) for extensible
configuration. ``TENANT_TABLES`` lists the tables protected by PostgreSQL Row
Level Security (see migration 0002).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, JSONType, UTCDateTime, utcnow

PLATFORM_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AUTOMATION_ENGINEER", "SALES", "SUPPORT")
CLIENT_ROLES = ("CLIENT_ADMIN", "CLIENT_MEMBER", "VIEWER")
ALL_ROLES = PLATFORM_ROLES + CLIENT_ROLES

EXECUTION_STATUSES = ("PENDING", "QUEUED", "RUNNING", "WAITING", "COMPLETED", "FAILED", "CANCELLED", "RETRYING")
TERMINAL_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")
STEP_STATUSES = ("PENDING", "RUNNING", "WAITING", "COMPLETED", "FAILED", "SKIPPED")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE", index: bool = True) -> Mapped[Any]:
    return mapped_column(Uuid, ForeignKey(target, ondelete=ondelete), nullable=nullable, index=index)


def org_fk(nullable: bool = False) -> Mapped[Any]:
    return fk("organizations.id", nullable=nullable)


def js(default=dict) -> Mapped[Any]:
    return mapped_column(JSONType, default=default, nullable=False)


def ts(nullable: bool = True) -> Mapped[Any]:
    return mapped_column(UTCDateTime, nullable=nullable)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --------------------------------------------------------------------------- identity & access


class User(Base, Timestamps):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("platform_role IS NULL OR " + _in("platform_role", PLATFORM_ROLES), name="platform_role"),)
    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    platform_role: Mapped[str | None] = mapped_column(String(40))  # Weakless staff role; NULL for client users
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = ts()
    last_login_at: Mapped[datetime | None] = ts()
    deleted_at: Mapped[datetime | None] = ts()
    # Two-factor sign-in (TOTP). Secrets are vault envelopes bound to the user; recovery codes are SHA-256 hashes.
    mfa_secret_envelope: Mapped[str | None] = mapped_column(Text)
    mfa_pending_envelope: Mapped[str | None] = mapped_column(Text)  # enrollment started, not confirmed yet
    mfa_enabled_at: Mapped[datetime | None] = ts()
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger)  # last accepted time step: a code works once
    mfa_recovery_hashes: Mapped[list | None] = mapped_column(JSONType)


class UserSession(Base, Timestamps):
    __tablename__ = "user_sessions"
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk("users.id")
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = ts(nullable=False)
    last_seen_at: Mapped[datetime | None] = ts()
    revoked_at: Mapped[datetime | None] = ts()


class UserToken(Base):
    """Single-use account links: password resets and invitations. Only a SHA-256 of the
    token is stored; the raw token exists only in the link sent to the person."""

    __tablename__ = "user_tokens"
    __table_args__ = (
        CheckConstraint("purpose IN ('password_reset', 'invitation')", name="purpose"),
        Index("ix_user_tokens_user_purpose", "user_id", "purpose"),
    )
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk("users.id", index=False)
    organization_id: Mapped[uuid.UUID | None] = org_fk(nullable=True)  # invitation target; NULL for resets and staff invites
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = ts(nullable=False)
    used_at: Mapped[datetime | None] = ts()
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class LoginHistory(Base):
    __tablename__ = "login_history"
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(100))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False, index=True)


class Role(Base, Timestamps):
    __tablename__ = "roles"
    __table_args__ = (CheckConstraint("scope IN ('platform', 'organization')", name="scope"),)
    id: Mapped[uuid.UUID] = pk()
    key: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Permission(Base, Timestamps):
    __tablename__ = "permissions"
    id: Mapped[uuid.UUID] = pk()
    key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id"),)
    id: Mapped[uuid.UUID] = pk()
    role_id: Mapped[uuid.UUID] = fk("roles.id")
    permission_id: Mapped[uuid.UUID] = fk("permissions.id")


# --------------------------------------------------------------------------- tenancy


class Organization(Base, Timestamps):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended', 'archived')", name="status"),
        CheckConstraint("mode IN ('agency', 'saas')", name="mode"),
    )
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="agency", nullable=False)  # agency-managed vs self-serve SaaS
    industry: Mapped[str | None] = mapped_column(String(80))
    settings: Mapped[dict] = js()
    deleted_at: Mapped[datetime | None] = ts()


class OrganizationMember(Base, Timestamps):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        CheckConstraint(_in("role", CLIENT_ROLES), name="role"),
        CheckConstraint("status IN ('active', 'invited', 'disabled')", name="status"),
    )
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    user_id: Mapped[uuid.UUID] = fk("users.id")
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    user: Mapped[User] = relationship(lazy="joined")


class Client(Base, Timestamps):
    """Business profile of the customer behind an organization (one per organization)."""

    __tablename__ = "clients"
    __table_args__ = (CheckConstraint("status IN ('lead', 'onboarding', 'active', 'paused', 'churned')", name="status"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False)
    legal_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    contact_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    contact_email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    contact_phone: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    country: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="onboarding", nullable=False)
    account_manager_id: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")


class Workspace(Base, Timestamps):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# --------------------------------------------------------------------------- workflows


class Workflow(Base, Timestamps):
    __tablename__ = "workflows"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="status"),
        Index("ix_workflows_org_active", "organization_id", "is_active"),
    )
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    workspace_id: Mapped[uuid.UUID] = fk("workspaces.id")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("workflow_versions.id", ondelete="SET NULL", use_alter=True))
    draft_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    variables: Mapped[dict] = js()  # available to nodes as {{ vars.* }}
    settings: Mapped[dict] = js()  # timeout_seconds, max_concurrency
    installed_template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    deleted_at: Mapped[datetime | None] = ts()


class WorkflowVersion(Base):
    """Immutable published snapshot of a workflow graph."""

    __tablename__ = "workflow_versions"
    __table_args__ = (UniqueConstraint("workflow_id", "version"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    workflow_id: Mapped[uuid.UUID] = fk("workflows.id")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = js()
    variables: Mapped[dict] = js()
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    published_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class WorkflowNode(Base, Timestamps):
    """A node of the editable draft graph."""

    __tablename__ = "workflow_nodes"
    __table_args__ = (UniqueConstraint("workflow_id", "node_key"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    workflow_id: Mapped[uuid.UUID] = fk("workflows.id")
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    config: Mapped[dict] = js()
    settings: Mapped[dict] = js()  # retry policy, timeout
    position_x: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    position_y: Mapped[float] = mapped_column(Float, default=0, nullable=False)


class WorkflowConnection(Base):
    __tablename__ = "workflow_connections"
    __table_args__ = (UniqueConstraint("workflow_id", "source_key", "source_handle", "target_key"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    workflow_id: Mapped[uuid.UUID] = fk("workflows.id")
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_handle: Mapped[str] = mapped_column(String(64), default="out", nullable=False)
    target_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class WorkflowExecution(Base, Timestamps):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        CheckConstraint(_in("status", EXECUTION_STATUSES), name="status"),
        UniqueConstraint("workflow_id", "idempotency_key"),
        Index("ix_workflow_executions_org_created", "organization_id", "created_at"),
        Index("ix_workflow_executions_org_status", "organization_id", "status"),
        Index("ix_workflow_executions_status_finished", "status", "finished_at"),  # retention sweeps
    )
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    workflow_id: Mapped[uuid.UUID] = fk("workflows.id")
    workflow_version_id: Mapped[uuid.UUID | None] = fk("workflow_versions.id", nullable=True, ondelete="SET NULL")
    definition: Mapped[dict | None] = mapped_column(JSONType)  # snapshot for draft test runs (no version)
    variables: Mapped[dict] = js()
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(80), default="manual", nullable=False)
    trigger_node: Mapped[str | None] = mapped_column(String(64))
    trigger_payload: Mapped[dict] = js()
    state: Mapped[dict] = js()  # persisted interpreter state (outputs, edge states, waits)
    output: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[dict | None] = mapped_column(JSONType)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    retry_of_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resume_token: Mapped[str | None] = mapped_column(String(64))
    deadline_at: Mapped[datetime | None] = ts()
    started_at: Mapped[datetime | None] = ts()
    finished_at: Mapped[datetime | None] = ts()
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)
    details_purged_at: Mapped[datetime | None] = ts()  # step log and payloads removed by the retention policy


class WorkflowExecutionStep(Base):
    __tablename__ = "workflow_execution_steps"
    __table_args__ = (
        CheckConstraint(_in("status", STEP_STATUSES), name="status"),
        Index("ix_workflow_execution_steps_exec_seq", "execution_id", "sequence"),
    )
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    execution_id: Mapped[uuid.UUID] = fk("workflow_executions.id")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(80), nullable=False)
    node_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    iteration: Mapped[str | None] = mapped_column(String(64))  # loop iteration path, e.g. "loop1:3"
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    input: Mapped[dict | None] = mapped_column(JSONType)
    output: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[dict | None] = mapped_column(JSONType)
    started_at: Mapped[datetime | None] = ts()
    finished_at: Mapped[datetime | None] = ts()
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class WorkflowSchedule(Base, Timestamps):
    __tablename__ = "workflow_schedules"
    __table_args__ = (UniqueConstraint("workflow_id", "node_key"), Index("ix_workflow_schedules_due", "is_active", "next_run_at"))
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    workflow_id: Mapped[uuid.UUID] = fk("workflows.id")
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    cron: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime | None] = ts()
    last_run_at: Mapped[datetime | None] = ts()


class ExecutionQueueJob(Base, Timestamps):
    """Durable job queue (table ``execution_queues``). Workers claim rows with
    ``FOR UPDATE SKIP LOCKED`` on PostgreSQL; ``DEAD`` rows are the dead-letter queue."""

    __tablename__ = "execution_queues"
    __table_args__ = (
        CheckConstraint("status IN ('QUEUED', 'CLAIMED', 'DONE', 'DEAD')", name="status"),
        Index("ix_execution_queues_ready", "status", "run_at"),
    )
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    kind: Mapped[str] = mapped_column(String(40), default="execution", nullable=False)
    execution_id: Mapped[uuid.UUID | None] = fk("workflow_executions.id", nullable=True)
    payload: Mapped[dict] = js()
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", nullable=False)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None] = ts()
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = ts()


# --------------------------------------------------------------------------- templates & packages


class AutomationTemplate(Base, Timestamps):
    __tablename__ = "automation_templates"
    __table_args__ = (CheckConstraint("status IN ('draft', 'published', 'deprecated')", name="status"),)
    id: Mapped[uuid.UUID] = pk()
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    industry: Mapped[str | None] = mapped_column(String(80), index=True)
    required_integrations: Mapped[list] = js(list)
    required_credentials: Mapped[list] = js(list)
    pricing: Mapped[dict] = js()
    setup_instructions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="published", nullable=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("template_versions.id", ondelete="SET NULL", use_alter=True))


class TemplateVersion(Base):
    __tablename__ = "template_versions"
    __table_args__ = (UniqueConstraint("template_id", "version"),)
    id: Mapped[uuid.UUID] = pk()
    template_id: Mapped[uuid.UUID] = fk("automation_templates.id")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = js()
    config_schema: Mapped[dict] = js()  # JSON Schema of install-time configuration (becomes workflow variables)
    changelog: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class InstalledTemplate(Base, Timestamps):
    __tablename__ = "installed_templates"
    __table_args__ = (CheckConstraint("status IN ('installed', 'active', 'disabled', 'removed')", name="status"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    template_id: Mapped[uuid.UUID] = fk("automation_templates.id", ondelete="RESTRICT")
    template_version_id: Mapped[uuid.UUID] = fk("template_versions.id", ondelete="RESTRICT")
    workflow_id: Mapped[uuid.UUID | None] = fk("workflows.id", nullable=True, ondelete="SET NULL")
    package_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    config: Mapped[dict] = js()
    status: Mapped[str] = mapped_column(String(20), default="installed", nullable=False)
    installed_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)


class IndustryPackage(Base, Timestamps):
    __tablename__ = "industry_packages"
    id: Mapped[uuid.UUID] = pk()
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    industry: Mapped[str] = mapped_column(String(80), nullable=False)
    pricing: Mapped[dict] = js()
    status: Mapped[str] = mapped_column(String(20), default="published", nullable=False)


class IndustryPackageItem(Base):
    __tablename__ = "industry_package_items"
    __table_args__ = (UniqueConstraint("package_id", "template_id"),)
    id: Mapped[uuid.UUID] = pk()
    package_id: Mapped[uuid.UUID] = fk("industry_packages.id")
    template_id: Mapped[uuid.UUID] = fk("automation_templates.id")
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# --------------------------------------------------------------------------- AI


class AIAgent(Base, Timestamps):
    __tablename__ = "ai_agents"
    __table_args__ = (CheckConstraint("status IN ('active', 'disabled')", name="status"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    agent_type: Mapped[str] = mapped_column(String(60), default="custom", nullable=False)
    system_instructions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    fallbacks: Mapped[list] = js(list)  # [{provider, model}] tried in order when the primary fails
    temperature: Mapped[float | None] = mapped_column(Float)
    max_tokens: Mapped[int] = mapped_column(Integer, default=16000, nullable=False)
    max_iterations: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    provider_options: Mapped[dict] = js()  # provider-specific knobs, e.g. {"effort": "high"} for Claude
    limits: Mapped[dict] = js()  # max_executions_per_day, max_tokens_per_day
    knowledge_base_ids: Mapped[list] = js(list)
    permissions: Mapped[dict] = js()
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    deleted_at: Mapped[datetime | None] = ts()


class AIAgentTool(Base, Timestamps):
    """Explicit per-agent tool allowlist. An agent can only call tools listed here."""

    __tablename__ = "ai_agent_tools"
    __table_args__ = (UniqueConstraint("agent_id", "name"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    agent_id: Mapped[uuid.UUID] = fk("ai_agents.id")
    name: Mapped[str] = mapped_column(String(64), nullable=False)  # name exposed to the model
    tool_key: Mapped[str] = mapped_column(String(80), nullable=False)  # key in the controlled tool registry
    config: Mapped[dict] = js()
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AIConversation(Base, Timestamps):
    __tablename__ = "ai_conversations"
    __table_args__ = (Index("ix_ai_conversations_ref", "agent_id", "external_ref"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    agent_id: Mapped[uuid.UUID] = fk("ai_agents.id")
    external_ref: Mapped[str | None] = mapped_column(String(200))  # e.g. chat id of the end customer
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    meta: Mapped[dict] = js()


class AIMessage(Base):
    __tablename__ = "ai_messages"
    __table_args__ = (CheckConstraint("role IN ('user', 'assistant', 'tool')", name="role"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    conversation_id: Mapped[uuid.UUID] = fk("ai_conversations.id")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[dict] = js()  # normalized: {text, tool_calls:[...]} or {tool_results:[...]}
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class AIAgentExecution(Base, Timestamps):
    __tablename__ = "ai_agent_executions"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'CANCELLED')", name="status"),
        Index("ix_ai_agent_executions_org_created", "organization_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    agent_id: Mapped[uuid.UUID | None] = fk("ai_agents.id", nullable=True, ondelete="SET NULL")
    conversation_id: Mapped[uuid.UUID | None] = fk("ai_conversations.id", nullable=True, ondelete="SET NULL")
    workflow_execution_id: Mapped[uuid.UUID | None] = fk("workflow_executions.id", nullable=True, ondelete="SET NULL")
    workflow_node_key: Mapped[str | None] = mapped_column(String(64))  # node to resume when a paused agent finishes
    kind: Mapped[str] = mapped_column(String(30), default="agent", nullable=False)  # agent | completion
    status: Mapped[str] = mapped_column(String(20), default="RUNNING", nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(120))
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))  # NULL when the model has no configured price
    iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_text: Mapped[str | None] = mapped_column(Text)
    trace: Mapped[list] = js(list)  # tool calls and provider attempts
    pending: Mapped[dict | None] = mapped_column(JSONType)  # tool calls awaiting human approval
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = ts()


class KnowledgeBase(Base, Timestamps):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class KnowledgeDocument(Base, Timestamps):
    __tablename__ = "knowledge_documents"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    knowledge_base_id: Mapped[uuid.UUID] = fk("knowledge_bases.id")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = js()


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    knowledge_base_id: Mapped[uuid.UUID] = fk("knowledge_bases.id")
    document_id: Mapped[uuid.UUID] = fk("knowledge_documents.id")
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    terms: Mapped[dict] = js()  # term -> frequency, for BM25 ranking
    length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# --------------------------------------------------------------------------- integrations & vault


class IntegrationProvider(Base, Timestamps):
    """Catalog row, synced from the code-defined provider catalog at startup."""

    __tablename__ = "integration_providers"
    id: Mapped[uuid.UUID] = pk()
    key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(30), nullable=False)
    config: Mapped[dict] = js()  # fields, scopes, docs
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Integration(Base, Timestamps):
    __tablename__ = "integrations"
    __table_args__ = (
        CheckConstraint("status IN ('awaiting_credentials', 'pending', 'connected', 'error', 'revoked')", name="status"),
        UniqueConstraint("organization_id", "provider_key", "name"),
    )
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    provider_key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="awaiting_credentials", nullable=False)
    config: Mapped[dict] = js()  # non-secret settings only
    scopes: Mapped[list] = js(list)
    last_checked_at: Mapped[datetime | None] = ts()
    last_ok_at: Mapped[datetime | None] = ts()
    last_error: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = ts()
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)
    deleted_at: Mapped[datetime | None] = ts()


class IntegrationCredential(Base, Timestamps):
    """Envelope-encrypted secret material for an integration. Never returned by the API."""

    __tablename__ = "integration_credentials"
    __table_args__ = (UniqueConstraint("integration_id", "kind"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    integration_id: Mapped[uuid.UUID] = fk("integrations.id")
    kind: Mapped[str] = mapped_column(String(30), default="primary", nullable=False)
    envelope: Mapped[str] = mapped_column(Text, nullable=False)
    key_id: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    rotated_at: Mapped[datetime | None] = ts()


class OAuthState(Base):
    __tablename__ = "oauth_states"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    integration_id: Mapped[uuid.UUID] = fk("integrations.id")
    user_id: Mapped[uuid.UUID] = fk("users.id", index=False)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    verifier_envelope: Mapped[str | None] = mapped_column(Text)  # encrypted PKCE verifier
    expires_at: Mapped[datetime] = ts(nullable=False)
    used_at: Mapped[datetime | None] = ts()
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- triggers & events


class WebhookEndpoint(Base, Timestamps):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (UniqueConstraint("workflow_id", "node_key"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    workflow_id: Mapped[uuid.UUID] = fk("workflows.id")
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # unguessable URL token
    kind: Mapped[str] = mapped_column(String(40), default="webhook", nullable=False)
    signature_scheme: Mapped[str] = mapped_column(String(30), default="hmac_sha256", nullable=False)
    secret_envelope: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    last_received_at: Mapped[datetime | None] = ts()


class WebhookEvent(Base):
    """Central event log: inbound webhooks, form posts, application events, schedules."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "dedupe_key"),
        CheckConstraint("status IN ('received', 'rejected', 'duplicate', 'processed', 'failed', 'ignored')", name="status"),
        Index("ix_webhook_events_org_received", "organization_id", "received_at"),
        Index("ix_webhook_events_received", "received_at"),
    )
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    endpoint_id: Mapped[uuid.UUID | None] = fk("webhook_endpoints.id", nullable=True, ondelete="SET NULL")
    source: Mapped[str] = mapped_column(String(40), nullable=False)  # webhook | form | app | schedule | replay
    event_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    headers: Mapped[dict] = js()
    payload: Mapped[dict] = js()
    status: Mapped[str] = mapped_column(String(20), default="received", nullable=False)
    reason: Mapped[str | None] = mapped_column(String(300))
    execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    replay_of_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- governance


class ApprovalRequest(Base, Timestamps):
    __tablename__ = "approval_requests"
    __table_args__ = (CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')", name="status"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # workflow_step | agent_tool
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    payload: Mapped[dict] = js()
    execution_id: Mapped[uuid.UUID | None] = fk("workflow_executions.id", nullable=True)
    node_key: Mapped[str | None] = mapped_column(String(64))
    agent_execution_id: Mapped[uuid.UUID | None] = fk("ai_agent_executions.id", nullable=True)
    decided_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)
    decided_at: Mapped[datetime | None] = ts()
    decision_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    expires_at: Mapped[datetime | None] = ts()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_org_created", "organization_id", "created_at"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID | None] = org_fk(nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    actor_email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    resource_id: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    meta: Mapped[dict] = js()
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_read", "user_id", "read_at"), Index("ix_notifications_created", "created_at"))
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID | None] = org_fk(nullable=True)
    user_id: Mapped[uuid.UUID] = fk("users.id")
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="info", nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data: Mapped[dict] = js()
    read_at: Mapped[datetime | None] = ts()
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class NotificationPreference(Base, Timestamps):
    __tablename__ = "notification_preferences"
    __table_args__ = (UniqueConstraint("user_id", "event_type"),)
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = fk("users.id")
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    in_app: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class NotificationChannel(Base, Timestamps):
    """Organization-level outbound channel (webhook, Slack, Discord, Telegram, email)."""

    __tablename__ = "notification_channels"
    __table_args__ = (CheckConstraint("kind IN ('webhook', 'slack', 'discord', 'telegram', 'email')", name="kind"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    config: Mapped[dict] = js()  # non-secret (e.g. telegram chat_id, email address)
    secret_envelope: Mapped[str | None] = mapped_column(Text)  # webhook URL / bot token
    events: Mapped[list] = js(list)  # subscribed event types; empty = all
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID | None] = org_fk(nullable=True)
    channel_id: Mapped[uuid.UUID | None] = fk("notification_channels.id", nullable=True, ondelete="SET NULL")
    channel_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # sent | failed | skipped
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- billing & usage


class SubscriptionPlan(Base, Timestamps):
    __tablename__ = "subscription_plans"
    __table_args__ = (CheckConstraint("price_monthly_cents >= 0", name="price_non_negative"),)
    id: Mapped[uuid.UUID] = pk()
    key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    price_monthly_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="usd", nullable=False)
    limits: Mapped[dict] = js()  # executions_per_month, ai_tokens_per_month, integrations, workflows, concurrency (null = unlimited)
    features: Mapped[list] = js(list)
    provider_price_id: Mapped[str | None] = mapped_column(String(120))  # e.g. Stripe price id
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Subscription(Base, Timestamps):
    __tablename__ = "subscriptions"
    __table_args__ = (CheckConstraint("status IN ('trialing', 'active', 'past_due', 'canceled')", name="status"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False)
    plan_id: Mapped[uuid.UUID] = fk("subscription_plans.id", ondelete="RESTRICT")
    pending_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # downgrade scheduled for period end
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    current_period_start: Mapped[datetime] = ts(nullable=False)
    current_period_end: Mapped[datetime] = ts(nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)
    provider_customer_id: Mapped[str | None] = mapped_column(String(120))
    provider_subscription_id: Mapped[str | None] = mapped_column(String(120), index=True)
    alert_state: Mapped[dict] = js()  # usage thresholds already notified per period


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (Index("ix_usage_records_org_period_metric", "organization_id", "period", "metric"),
                      Index("ix_usage_records_occurred", "occurred_at"))
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    metric: Mapped[str] = mapped_column(String(60), nullable=False)  # executions | ai_requests | ai_input_tokens | ai_output_tokens | ai_cost_usd
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    meta: Mapped[dict] = js()
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class BillingEvent(Base):
    __tablename__ = "billing_events"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID | None] = org_fk(nullable=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(120), unique=True)
    amount_cents: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(3))
    data: Mapped[dict] = js()
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- misc


class GeneratedDocument(Base):
    """Output of the Generate Document node."""

    __tablename__ = "generated_documents"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    execution_id: Mapped[uuid.UUID | None] = fk("workflow_executions.id", nullable=True, ondelete="SET NULL")
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), default="text/markdown", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class SystemSetting(Base, Timestamps):
    __tablename__ = "system_settings"
    id: Mapped[uuid.UUID] = pk()
    key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    value: Mapped[Any] = mapped_column(JSONType, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL", index=False)


# Tables guarded by PostgreSQL Row Level Security (strict organization_id match).
# organizations / organization_members are enforced in the API layer because the
# auth layer must read memberships before a tenant context exists.
TENANT_TABLES = (
    "clients", "workspaces", "workflows", "workflow_versions", "workflow_nodes", "workflow_connections",
    "workflow_executions", "workflow_execution_steps", "workflow_schedules", "execution_queues",
    "installed_templates", "ai_agents", "ai_agent_tools", "ai_conversations", "ai_messages",
    "ai_agent_executions", "knowledge_bases", "knowledge_documents", "knowledge_chunks",
    "integrations", "integration_credentials", "oauth_states", "webhook_endpoints", "webhook_events",
    "approval_requests", "notification_channels", "subscriptions", "usage_records", "generated_documents",
)
