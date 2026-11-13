"""Trigger nodes. A trigger does no work at run time: the event that started
the execution becomes its output (``{{ trigger.* }}`` / ``{{ nodes.<id>.* }}``)."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from app.engine.registry import EmptyConfig, NodeConfig, NodeContext, NodeResult, node

_HTTP_OUT = {"body": {"type": "object"}, "headers": {"type": "object"}, "query": {"type": "object"}, "event_id": {"type": "string"}}


def _passthrough(ctx: NodeContext, cfg) -> NodeResult:  # pragma: no cover - the executor seeds trigger output directly
    return NodeResult(output=ctx.scope.get("trigger", {}))


class ResponseMixin(NodeConfig):
    response_mode: Literal["immediately", "respond_step"] = Field(
        "immediately", description="'respond_step': wait for a Respond to Webhook step and return its reply to the caller")
    response_timeout_seconds: int = Field(10, ge=1, le=25, description="How long the caller waits for that reply before getting the default answer")


class WebhookConfig(ResponseMixin):
    signature_scheme: Literal["hmac_sha256", "shared_secret", "none"] = Field("hmac_sha256", description="How inbound requests are authenticated")
    rate_limit_per_minute: int = Field(120, ge=1, le=6000)


class ScheduleConfig(NodeConfig):
    cron: str = Field(..., description="Cron expression (5 fields), e.g. '0 9 * * 1-5'")
    timezone: str = Field("UTC", description="IANA timezone, e.g. Europe/Sofia")

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        from app.services.scheduler import valid_cron

        if not valid_cron(v):
            raise ValueError("must be a valid 5-field cron expression")
        return v


class FormConfig(ResponseMixin):
    required_fields: list[str] = Field(default_factory=list, description="Submissions missing these fields are rejected")
    rate_limit_per_minute: int = Field(30, ge=1, le=6000)


class MessageConfig(NodeConfig):
    provider: Literal["telegram", "whatsapp", "slack"] = "telegram"
    rate_limit_per_minute: int = Field(300, ge=1, le=6000)


class PaymentConfig(NodeConfig):
    provider: Literal["stripe"] = "stripe"
    event_types: list[str] = Field(default_factory=list, description="Only these provider event types start the workflow (empty = all)")


class AppEventConfig(NodeConfig):
    event_type: str = Field(..., min_length=1, description="Event name, e.g. 'workflow.failed' or 'crm.*'")


class MailboxConfig(NodeConfig):
    integration_id: str = Field(..., title="Mailbox", description="IMAP Mailbox integration to watch")
    folder: str = Field("INBOX", min_length=1, max_length=200, description="Folder to watch")
    poll_minutes: int = Field(2, ge=1, le=60, description="How often to check for new mail")
    mark_as_read: bool = Field(False, description="Mark handled messages as read in the mailbox")
    from_contains: str = Field("", max_length=200, description="Only start for senders containing this text (empty = all)")
    subject_contains: str = Field("", max_length=200, description="Only start for subjects containing this text (empty = all)")


class SharedSecretConfig(NodeConfig):
    rate_limit_per_minute: int = Field(120, ge=1, le=6000)


node("trigger.manual", "trigger", "Manual Trigger", "Start the workflow by hand or through the API.", EmptyConfig,
     output_schema={"input": {"type": "object"}}, icon="play")(_passthrough)
node("trigger.webhook", "trigger", "Webhook Trigger", "Start when an HTTP request hits this workflow's unique URL.", WebhookConfig,
     output_schema=_HTTP_OUT, icon="webhook")(_passthrough)
node("trigger.schedule", "trigger", "Schedule Trigger", "Start on a cron schedule.", ScheduleConfig,
     output_schema={"scheduled_for": {"type": "string"}, "cron": {"type": "string"}}, icon="clock")(_passthrough)
node("trigger.form", "trigger", "Form Submission Trigger", "Start when a form posts to this workflow's public form URL.", FormConfig,
     output_schema=_HTTP_OUT, icon="file-input")(_passthrough)
node("trigger.database_event", "trigger", "Database Event Trigger",
     "Start from a database webhook (e.g. Supabase Database Webhooks) authenticated with a shared secret header.", SharedSecretConfig,
     output_schema=_HTTP_OUT, icon="database")(_passthrough)
node("trigger.email", "trigger", "Email Trigger",
     "Start from an inbound-email webhook (mail provider inbound parse / forwarder) authenticated with a shared secret header.",
     SharedSecretConfig, output_schema=_HTTP_OUT, icon="mail")(_passthrough)
node("trigger.mailbox", "trigger", "Mailbox Trigger",
     "Start for each new email in an IMAP mailbox (Gmail, Outlook, any mail host). Only mail that arrives after activation counts.",
     MailboxConfig, providers=["imap"],
     output_schema={"subject": {"type": "string"}, "from": {"type": "object"}, "to": {"type": "array"}, "text": {"type": "string"},
                    "html": {"type": "string"}, "date": {"type": "string"}, "message_id": {"type": "string"}, "attachments": {"type": "array"}},
     icon="inbox")(_passthrough)
node("trigger.message", "trigger", "Incoming Message Trigger", "Start when a chat provider delivers an incoming message webhook.", MessageConfig,
     output_schema={**_HTTP_OUT, "text": {"type": "string"}, "from": {"type": "string"}}, icon="message-circle")(_passthrough)
node("trigger.payment", "trigger", "Payment Event Trigger", "Start on a verified payment provider webhook event.", PaymentConfig,
     output_schema={**_HTTP_OUT, "event_type": {"type": "string"}}, icon="credit-card")(_passthrough)
node("trigger.app_event", "trigger", "Application Event Trigger", "Start on an internal platform or custom application event.", AppEventConfig,
     output_schema={"event_type": {"type": "string"}, "data": {"type": "object"}}, icon="zap")(_passthrough)
