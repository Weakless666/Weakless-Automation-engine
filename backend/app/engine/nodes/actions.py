"""Action nodes. Anything that changes an external system is marked
``idempotent=False`` so the engine never repeats it automatically."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core import http
from app.core.errors import NodeError
from app.engine.registry import NodeConfig, NodeContext, NodeResult, node

_ID = Field(..., description="Connected integration to use")


def _run(ctx: NodeContext, integration_id: str, action: str, params: dict, providers: list[str]) -> dict:
    from app.integrations.service import get_integration, run_action

    integration = get_integration(ctx.db, ctx.organization_id, integration_id)
    if providers and integration.provider_key not in providers:
        raise NodeError(f"Integration '{integration.name}' ({integration.provider_key}) cannot be used by this node", code="invalid_config")
    return run_action(ctx.db, ctx.organization_id, integration.id, action, params, secrets_out=ctx.secrets)


# ----------------------------------------------------------------------------- messaging

EMAIL_PROVIDERS = ["gmail", "outlook", "microsoft_365", "smtp"]


MAX_ATTACHMENTS_BYTES = 20 * 1024 * 1024


def _load_file(ctx: NodeContext, file: Any):
    """A stored file of this organization, from an id or a file reference produced by an earlier step."""
    from app.core.errors import AppError
    from app.services.files import get_file

    try:
        return get_file(ctx.db, ctx.organization_id, file, with_data=True)
    except AppError:
        raise NodeError("File not found in this organization (it may have expired)", code="file_not_found") from None


def _store(ctx: NodeContext, name: str, content: bytes, content_type: str) -> dict:
    from app.core.errors import AppError
    from app.services.files import ref, save_file, workflow_file_expiry

    try:
        f = save_file(ctx.db, ctx.organization_id, name, content, content_type=content_type, source="workflow",
                      execution_id=ctx.execution.id, expires_at=workflow_file_expiry(ctx.db))
    except AppError as exc:
        raise NodeError(exc.message, code=exc.code) from exc
    return ref(f)


class SendEmailConfig(NodeConfig):
    integration_id: str = _ID
    to: str = Field(..., min_length=3, description="Recipient(s), comma separated")
    subject: str = Field(..., min_length=1, max_length=500)
    body: str = Field("", description="Plain-text body")
    html: str | None = Field(None, description="Optional HTML body")
    cc: str | None = None
    attachments: list[Any] = Field(default_factory=list, max_length=10,
                                   description="Stored files to attach: file ids or file outputs of earlier steps, e.g. {{ nodes.download.file }}")


@node("action.send_email", "action", "Send Email", "Send an email through Gmail, Outlook or SMTP, with optional file attachments.", SendEmailConfig,
      providers=EMAIL_PROVIDERS, idempotent=False, output_schema={"message_id": {"type": "string"}}, icon="mail")
def send_email(ctx: NodeContext, cfg: SendEmailConfig) -> NodeResult:
    params = cfg.model_dump(exclude={"integration_id", "attachments"})
    files = [_load_file(ctx, a) for a in cfg.attachments if a not in (None, "")]
    if sum(f.size for f in files) > MAX_ATTACHMENTS_BYTES:
        raise NodeError("Attachments may total at most 20 MB per email", code="attachment_too_large")
    if files:
        params["attachments"] = [{"name": f.name, "content_type": f.content_type, "content": f.data} for f in files]
    out = _run(ctx, cfg.integration_id, "send_email", params, EMAIL_PROVIDERS)
    return NodeResult(output={**out, "attachments": [f.name for f in files]} if files else out)


class WhatsAppConfig(NodeConfig):
    integration_id: str = _ID
    to: str = Field(..., min_length=5, description="Recipient phone number in international format")
    text: str = Field(..., min_length=1, max_length=4096)


@node("action.send_whatsapp", "action", "Send WhatsApp Message", "Send a text message with the WhatsApp Business Cloud API.", WhatsAppConfig,
      providers=["whatsapp"], idempotent=False, output_schema={"message_id": {"type": "string"}}, icon="message-circle")
def send_whatsapp(ctx: NodeContext, cfg: WhatsAppConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "send_message", {"to": cfg.to, "text": cfg.text}, ["whatsapp"]))


class TelegramConfig(NodeConfig):
    integration_id: str = _ID
    chat_id: str | int = Field(..., description="Chat id or @channel")
    text: str = Field(..., min_length=1, max_length=4096)
    parse_mode: Literal["", "Markdown", "MarkdownV2", "HTML"] = ""


@node("action.send_telegram", "action", "Send Telegram Message", "Send a message through a Telegram bot.", TelegramConfig,
      providers=["telegram"], idempotent=False, output_schema={"message_id": {"type": "integer"}}, icon="send")
def send_telegram(ctx: NodeContext, cfg: TelegramConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "send_message", {"to": cfg.chat_id, "text": cfg.text, "parse_mode": cfg.parse_mode}, ["telegram"]))


class DiscordConfig(NodeConfig):
    integration_id: str = _ID
    text: str = Field(..., min_length=1, max_length=2000)


@node("action.send_discord", "action", "Send Discord Message", "Post a message to a Discord channel webhook.", DiscordConfig,
      providers=["discord"], idempotent=False, output_schema={"message_id": {"type": "string"}}, icon="hash")
def send_discord(ctx: NodeContext, cfg: DiscordConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "send_message", {"text": cfg.text}, ["discord"]))


class SlackConfig(NodeConfig):
    integration_id: str = _ID
    channel: str = Field(..., min_length=1, description="Channel id or name")
    text: str = Field(..., min_length=1)


@node("action.send_slack", "action", "Send Slack Message", "Post a message to a Slack channel.", SlackConfig,
      providers=["slack"], idempotent=False, output_schema={"message_id": {"type": "string"}}, icon="slack")
def send_slack(ctx: NodeContext, cfg: SlackConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "send_message", {"to": cfg.channel, "text": cfg.text}, ["slack"]))


class ContentOutcomeConfig(NodeConfig):
    item_id: str = Field(..., title="Content item", description="Usually {{ trigger.data.id }} in a workflow started by content.due")
    status: Literal["published", "failed"] = "published"
    published_url: str = Field("", max_length=1000, description="Link to the live post, when known")
    note: str = Field("", max_length=2000)


@node("action.update_content", "action", "Update Content Item", "Record in the content calendar that an item was published or failed.",
      ContentOutcomeConfig, output_schema={"status": {"type": "string"}}, icon="calendar-check")
def update_content(ctx: NodeContext, cfg: ContentOutcomeConfig) -> NodeResult:
    from app.core.errors import AppError
    from app.services.content import get_item, mark

    try:
        item = mark(ctx.db, get_item(ctx.db, ctx.organization_id, cfg.item_id), status=cfg.status, url=cfg.published_url or None, note=cfg.note)
    except AppError as exc:
        raise NodeError(exc.message, code=exc.code) from exc
    return NodeResult(output={"status": item.status, "item_id": str(item.id)})


SOCIAL_PROVIDERS = ["linkedin", "meta", "x", "telegram"]


class PublishPostConfig(NodeConfig):
    integration_id: str = _ID
    network: Literal["", "facebook", "instagram"] = Field("", description="Only for a Meta integration: Facebook Page (default) or Instagram")
    text: str = Field("", max_length=20000, description="The post text, e.g. {{ trigger.data.body }}")
    link: str = Field("", max_length=1000, description="A link to add to the post (optional)")
    media: list[Any] = Field(default_factory=list, max_length=4,
                             description="Stored image or video files: file ids or file outputs of earlier steps, e.g. {{ nodes.image.file }}")
    target: str = Field("", max_length=200, description="Override where to post: page id, company URN or Telegram channel (default: the integration's)")


@node("action.publish_post", "action", "Publish Social Post", "Publish a post to LinkedIn, a Facebook Page, Instagram, X or a Telegram channel.",
      PublishPostConfig, providers=SOCIAL_PROVIDERS, idempotent=False,
      output_schema={"post_id": {"type": "string"}, "url": {"type": "string"}}, icon="megaphone")
def publish_post(ctx: NodeContext, cfg: PublishPostConfig) -> NodeResult:
    from app.services.publishing import load_media

    if not cfg.text.strip() and not cfg.media:
        raise NodeError("A post needs text or media", code="invalid_config")
    params = {"channel": cfg.network or "facebook", "text": cfg.text, "link": cfg.link, "target": cfg.target,
              "media": load_media(ctx.db, ctx.organization_id, cfg.media)}
    return NodeResult(output=_run(ctx, cfg.integration_id, "publish_post", params, SOCIAL_PROVIDERS))


class NotificationConfig(NodeConfig):
    title: str = Field(..., min_length=1, max_length=300)
    body: str = ""
    severity: Literal["info", "success", "warning", "error"] = "info"


@node("action.send_notification", "action", "Send Notification", "Notify the organization's admins in-app and on its notification channels.",
      NotificationConfig, idempotent=False, output_schema={"in_app": {"type": "integer"}}, icon="bell")
def send_notification(ctx: NodeContext, cfg: NotificationConfig) -> NodeResult:
    from app.services.notifications import notify

    count = notify(ctx.db, "workflow.notification", ctx.organization_id, title=cfg.title, body=cfg.body, severity=cfg.severity,
                   data={"execution_id": str(ctx.execution.id), "workflow_id": str(ctx.execution.workflow_id)})
    return NodeResult(output={"in_app": count})


# ----------------------------------------------------------------------------- CRM & data

CRM_PROVIDERS = ["hubspot", "pipedrive", "salesforce", "mailchimp"]
DB_PROVIDERS = ["supabase", "postgresql", "airtable", "notion", "google_sheets"]


class CreateLeadConfig(NodeConfig):
    integration_id: str = _ID
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    company: str | None = None
    title: str | None = Field(None, description="Lead title (Pipedrive)")
    properties: dict[str, Any] = Field(default_factory=dict, description="Additional provider-specific properties")


@node("action.crm_create_lead", "action", "Create CRM Lead", "Create a lead or contact in HubSpot, Pipedrive, Salesforce or Mailchimp.",
      CreateLeadConfig, providers=CRM_PROVIDERS, idempotent=False, output_schema={"record_id": {"type": "string"}}, icon="user-plus")
def crm_create_lead(ctx: NodeContext, cfg: CreateLeadConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "create_lead", cfg.model_dump(exclude={"integration_id"}), CRM_PROVIDERS))


class UpdateCrmConfig(NodeConfig):
    integration_id: str = _ID
    record_id: str = Field(..., min_length=1)
    object_type: str | None = Field(None, description="e.g. contacts / deals (HubSpot), persons (Pipedrive), Lead (Salesforce)")
    data: dict[str, Any] = Field(default_factory=dict, min_length=1)


@node("action.crm_update_record", "action", "Update CRM Record", "Update a CRM record by id.", UpdateCrmConfig,
      providers=["hubspot", "pipedrive", "salesforce"], idempotent=True, output_schema={"record_id": {"type": "string"}}, icon="user-cog")
def crm_update_record(ctx: NodeContext, cfg: UpdateCrmConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "update_record", cfg.model_dump(exclude={"integration_id"}), ["hubspot", "pipedrive", "salesforce"]))


class CreateRecordConfig(NodeConfig):
    integration_id: str = _ID
    table: str = Field(..., min_length=1, description="Table / sheet range / Notion database id")
    database: str | None = Field(None, description="Airtable base id or spreadsheet id (overrides the integration default)")
    data: dict[str, Any] = Field(default_factory=dict, min_length=1)


@node("action.db_create_record", "action", "Create Database Record", "Insert a record into Supabase, PostgreSQL, Airtable, Notion or Google Sheets.",
      CreateRecordConfig, providers=DB_PROVIDERS, idempotent=False, output_schema={"record": {"type": "object"}}, icon="database")
def db_create_record(ctx: NodeContext, cfg: CreateRecordConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "create_record", cfg.model_dump(exclude={"integration_id"}), DB_PROVIDERS))


class UpdateRecordConfig(CreateRecordConfig):
    record_id: str = Field(..., min_length=1, description="Record id, key value, or sheet range (e.g. A2:C2)")
    key_column: str | None = Field(None, description="Key column for SQL-style providers (default: id)")


@node("action.db_update_record", "action", "Update Database Record", "Update a record by id.", UpdateRecordConfig,
      providers=DB_PROVIDERS, idempotent=True, output_schema={"updated": {"type": "integer"}}, icon="database-zap")
def db_update_record(ctx: NodeContext, cfg: UpdateRecordConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "update_record", cfg.model_dump(exclude={"integration_id"}), DB_PROVIDERS))


class FindCondition(BaseModel):
    field: str = Field(..., min_length=1, max_length=100, description="Column or property name")
    operator: Literal["equals", "not_equals", "contains", "gt", "gte", "lt", "lte", "is_empty", "is_not_empty"] = "equals"
    value: Any = None


class _FindBase(NodeConfig):
    integration_id: str = _ID
    filters: list[FindCondition] = Field(default_factory=list, max_length=10, description="All conditions must match")
    sort_by: str = Field("", description="Field to sort by (optional)")
    order: Literal["ascending", "descending"] = "ascending"
    limit: int = Field(25, ge=1, le=200)


_FOUND = {"records": {"type": "array"}, "count": {"type": "integer"}, "found": {"type": "boolean"}, "first": {"type": "object"}}


class FindCrmConfig(_FindBase):
    object_type: str = Field("contacts", description="contacts / companies / deals (HubSpot), persons / deals (Pipedrive), Lead / Contact (Salesforce)")
    properties: list[str] = Field(default_factory=list, description="Fields to return (HubSpot, Salesforce); empty = defaults")


@node("action.crm_find", "action", "Find CRM Records", "Search HubSpot, Pipedrive or Salesforce, e.g. find the contact with this email.",
      FindCrmConfig, providers=["hubspot", "pipedrive", "salesforce"], output_schema=_FOUND, icon="user-search")
def crm_find(ctx: NodeContext, cfg: FindCrmConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "find_records", cfg.model_dump(exclude={"integration_id"}), ["hubspot", "pipedrive", "salesforce"]))


class FindRecordsConfig(_FindBase):
    table: str = Field(..., min_length=1, description="Table / sheet range (e.g. Sheet1!A:F) / Notion database id")
    database: str | None = Field(None, description="Airtable base id or spreadsheet id (overrides the integration default)")


@node("action.db_find", "action", "Find Database Records",
      "Read matching rows from Supabase, PostgreSQL, Airtable, Notion or Google Sheets.", FindRecordsConfig,
      providers=DB_PROVIDERS, output_schema=_FOUND, icon="search")
def db_find(ctx: NodeContext, cfg: FindRecordsConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "find_records", cfg.model_dump(exclude={"integration_id"}), DB_PROVIDERS))


# ----------------------------------------------------------------------------- HTTP


class HttpRequestConfig(NodeConfig):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(..., min_length=8, description="Public http(s) URL")
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = Field(None, description="JSON body for non-GET requests")
    timeout_seconds: float = Field(30, ge=1, le=120)
    fail_on_error_status: bool = Field(True, description="Treat HTTP 4xx/5xx as a failure")
    save_response_as_file: bool = Field(False, description="Store the response (a PDF, image, export...) as a file instead of reading it as text")
    file_name: str = Field("", max_length=300, description="Name for the saved file (default: taken from the response or URL)")


def _download_name(resp, url: str, wanted: str) -> str:
    import re as _re
    from urllib.parse import unquote, urlsplit

    if wanted:
        return wanted
    m = _re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", resp.headers.get("content-disposition", ""), _re.I)
    return unquote(m.group(1)) if m else (unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "download")


@node("action.http_request", "action", "HTTP Request", "Call any public HTTP endpoint, or download a file from one. Internal addresses are blocked.", HttpRequestConfig,
      idempotent=False, output_schema={"status": {"type": "integer"}, "body": {}, "headers": {"type": "object"}, "file": {"type": "object"}}, icon="globe")
def http_request(ctx: NodeContext, cfg: HttpRequestConfig) -> NodeResult:
    for k, v in cfg.headers.items():
        if k.lower() in ("authorization", "x-api-key", "api-key", "cookie") and len(v) >= 6:
            ctx.secrets.add(v)
            ctx.secrets.add(v.split(" ", 1)[-1])
    resp = http.request(cfg.method, cfg.url, headers=cfg.headers or None, params=cfg.query or None,
                        json=cfg.body if cfg.method != "GET" and cfg.body not in (None, "") else None, timeout=cfg.timeout_seconds)
    if cfg.fail_on_error_status:
        http.raise_for_status(resp, "HTTP request")
    kept = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "location", "retry-after", "etag")}
    if cfg.save_response_as_file:
        if not resp.content:
            raise NodeError("The response was empty, so there is no file to save", code="empty_response")
        file = _store(ctx, _download_name(resp, cfg.url, cfg.file_name), resp.content, resp.headers.get("content-type", ""))
        return NodeResult(output={"status": resp.status_code, "file": file, "headers": kept})
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:100_000]
    return NodeResult(output={"status": resp.status_code, "body": body,
                              "headers": {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "location", "retry-after", "etag")}})


class CallApiConfig(NodeConfig):
    integration_id: str = _ID
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field(..., min_length=1, pattern=r"^/", description="Path on the provider's API, e.g. /v1/customers")
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = None


@node("action.call_api", "action", "Call External API", "Call a connected provider's API with its stored credentials (path only - the host is fixed).",
      CallApiConfig, idempotent=False, output_schema={"status": {"type": "integer"}, "body": {}}, icon="plug")
def call_api(ctx: NodeContext, cfg: CallApiConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "api_request", cfg.model_dump(exclude={"integration_id"}), []))


# ----------------------------------------------------------------------------- documents, files, calendar


class GenerateDocumentConfig(NodeConfig):
    name: str = Field(..., min_length=1, max_length=300)
    content: str = Field(..., min_length=1, description="Document body; templates are filled from workflow data")
    content_type: Literal["text/markdown", "text/plain", "text/html"] = "text/markdown"


@node("action.generate_document", "action", "Generate Document", "Render a document from a template and store it with the execution.",
      GenerateDocumentConfig, output_schema={"document_id": {"type": "string"}, "content": {"type": "string"}}, icon="file-text")
def generate_document(ctx: NodeContext, cfg: GenerateDocumentConfig) -> NodeResult:
    from app.models import GeneratedDocument

    doc = GeneratedDocument(organization_id=ctx.organization_id, execution_id=ctx.execution.id, name=cfg.name,
                            content_type=cfg.content_type, content=cfg.content[:1_000_000])
    ctx.db.add(doc)
    ctx.db.flush()
    return NodeResult(output={"document_id": str(doc.id), "name": doc.name, "content_type": doc.content_type, "content": doc.content[:50_000]})


FILE_PROVIDERS = ["google_drive", "google_docs", "microsoft_365"]


class UploadFileConfig(NodeConfig):
    integration_id: str = _ID
    file: Any = Field(None, description="A stored file to upload: a file id or a file output of an earlier step. Leave empty to upload text")
    name: str = Field("", max_length=300, description="File name (default: the stored file's name)")
    content: str = Field("", description="Text content when no stored file is given, e.g. {{ nodes.doc.content }}")
    content_type: str = "text/plain"
    folder_id: str | None = None


@node("action.upload_file", "action", "Upload File", "Upload a stored file or text content to Google Drive, Google Docs or OneDrive.", UploadFileConfig,
      providers=FILE_PROVIDERS, idempotent=False, output_schema={"file_id": {"type": "string"}, "link": {"type": "string"}}, icon="upload")
def upload_file(ctx: NodeContext, cfg: UploadFileConfig) -> NodeResult:
    params = cfg.model_dump(exclude={"integration_id", "file"})
    if cfg.file not in (None, ""):
        f = _load_file(ctx, cfg.file)
        params.update(name=cfg.name or f.name, content=f.data, content_type=f.content_type)
    elif not cfg.name or not cfg.content:
        raise NodeError("Give a stored file, or a name and text content", code="invalid_config")
    return NodeResult(output=_run(ctx, cfg.integration_id, "upload_file", params, FILE_PROVIDERS))


class SaveFileConfig(NodeConfig):
    name: str = Field(..., min_length=1, max_length=300, description="File name, e.g. report-{{ now | truncate(10, '') }}.csv")
    content: str = Field(..., min_length=1, description="Text content (CSV, JSON, HTML, Markdown...) built from workflow data")
    content_type: str = Field("text/plain", max_length=200)


@node("action.save_file", "action", "Save as File", "Turn text built in the workflow (a CSV, report or JSON export) into a stored file you can attach, upload or download.",
      SaveFileConfig, output_schema={"file": {"type": "object"}}, icon="file-plus")
def save_file_node(ctx: NodeContext, cfg: SaveFileConfig) -> NodeResult:
    return NodeResult(output={"file": _store(ctx, cfg.name, cfg.content.encode(), cfg.content_type)})


CALENDAR_PROVIDERS = ["google_calendar", "outlook", "microsoft_365"]


class CalendarEventConfig(NodeConfig):
    integration_id: str = _ID
    title: str = Field(..., min_length=1)
    start: str = Field(..., description="ISO 8601 start, e.g. 2026-12-01T10:00:00")
    end: str = Field(..., description="ISO 8601 end")
    timezone: str = "UTC"
    description: str = ""
    location: str = ""
    attendees: list[str] = Field(default_factory=list)
    calendar_id: str | None = None


@node("action.create_calendar_event", "action", "Create Calendar Event", "Create an event in Google Calendar or Outlook.", CalendarEventConfig,
      providers=CALENDAR_PROVIDERS, idempotent=False, output_schema={"event_id": {"type": "string"}, "link": {"type": "string"}}, icon="calendar-plus")
def create_calendar_event(ctx: NodeContext, cfg: CalendarEventConfig) -> NodeResult:
    return NodeResult(output=_run(ctx, cfg.integration_id, "create_calendar_event", cfg.model_dump(exclude={"integration_id"}), CALENDAR_PROVIDERS))
