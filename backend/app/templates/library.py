"""Built-in automation template library and industry packages.

Templates are assembled from a small set of reusable workflow *patterns*
(AI message assistant, event notification, delayed follow-up, lead
qualification, ...). An industry package is nothing more than a named
collection of templates, so packages stay configurable data rather than
separate systems.

Install-time configuration becomes workflow variables, referenced by nodes as
``{{ vars.<name> }}``. ``x-integration`` in a config schema tells the UI to
render a picker limited to those providers.
"""
from __future__ import annotations

from typing import Any

# ----------------------------------------------------------------------------- builders


def _n(id: str, type: str, name: str, config: dict | None = None, col: int = 0, row: int = 0, settings: dict | None = None) -> dict:
    return {"id": id, "type": type, "name": name, "config": config or {}, "settings": settings or {},
            "position": {"x": 80 + col * 300, "y": 120 + row * 150}}


def _e(source: str, target: str, handle: str = "out") -> dict:
    return {"source": source, "target": target, "source_handle": handle}


def _integ(title: str, providers: list[str]) -> dict:
    return {"type": "string", "title": title, "x-integration": providers}


def _text(title: str, default: str = "", multiline: bool = False) -> dict:
    return {"type": "string", "title": title, "default": default, **({"x-multiline": True} if multiline else {})}


def _num(title: str, default: float) -> dict:
    return {"type": "number", "title": title, "default": default}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _cond(left: Any, operator: str, right: Any = None) -> dict:
    return {"combinator": "and", "conditions": [{"left": left, "operator": operator, "right": right}]}


EMAIL = ["gmail", "outlook", "microsoft_365", "smtp"]
CRM = ["hubspot", "pipedrive", "salesforce"]
CALENDAR = ["google_calendar", "outlook", "microsoft_365"]
DATA = ["supabase", "postgresql", "airtable", "notion", "google_sheets"]


def _send(provider: str, id: str, name: str, to: str, text: str, col: int, row: int = 0, integration_var: str = "messaging_integration") -> dict:
    ref = "{{ vars.%s }}" % integration_var
    if provider == "whatsapp":
        return _n(id, "action.send_whatsapp", name, {"integration_id": ref, "to": to, "text": text}, col, row)
    if provider == "telegram":
        return _n(id, "action.send_telegram", name, {"integration_id": ref, "chat_id": to, "text": text}, col, row)
    if provider == "slack":
        return _n(id, "action.send_slack", name, {"integration_id": ref, "channel": to, "text": text}, col, row)
    return _n(id, "action.send_email", name, {"integration_id": ref, "to": to, "subject": "{{ vars.email_subject }}", "body": text}, col, row)


def _result(definition: dict, props: dict, required: list[str], integrations: list[str], credentials: list[str] | None = None) -> dict:
    return {"definition": definition, "config_schema": _schema(props, required), "required_integrations": integrations,
            "required_credentials": credentials or []}


# ----------------------------------------------------------------------------- patterns


def assistant(provider: str, role: str, guidance: str) -> dict:
    """Incoming chat message -> AI reply grounded in business information -> reply on the same channel."""
    system = (f"You are the {role} for {{{{ vars.business_name }}}}. {guidance} Be concise, friendly and accurate. "
              "Only state facts found in the business information below; if you do not know, say so and offer to connect the customer "
              "with a team member.\n\nBusiness information:\n{{ vars.business_info }}")
    nodes = [
        _n("trigger", "trigger.message", "Incoming message", {"provider": provider}, 0),
        _n("has_text", "logic.filter", "Has text?", {"condition": _cond("{{ nodes.trigger.text }}", "is_not_empty")}, 1),
        _n("reply", "ai.completion", "Write reply", {"system": system, "prompt": "{{ nodes.trigger.text }}"}, 2),
        _send(provider, "send", "Send reply", "{{ nodes.trigger.from }}", "{{ nodes.reply.text }}", 3),
    ]
    edges = [_e("trigger", "has_text"), _e("has_text", "reply", "pass"), _e("reply", "send")]
    props = {"messaging_integration": _integ(f"{provider.title()} integration", [provider]),
             "business_name": _text("Business name"),
             "business_info": _text("Business information (hours, services, prices, policies)", multiline=True)}
    return _result({"nodes": nodes, "edges": edges}, props, ["messaging_integration", "business_name", "business_info"], [provider], ["AI provider"])


def event_notification(event: str, required: list[str], channel: str, to: str, message: str, subject: str = "") -> dict:
    """Webhook event -> validate -> message the customer and notify staff."""
    rules = [{"field": f, "value": "{{ nodes.trigger.body.%s }}" % f, "required": True, "type": "any"} for f in required]
    nodes = [
        _n("trigger", "trigger.webhook", f"{event} webhook", {}, 0),
        _n("check", "logic.validate", "Validate payload", {"rules": rules}, 1),
        _send(channel, "send", "Message customer", to, message, 2, 0),
        _n("staff", "action.send_notification", "Notify team", {"title": f"{event}", "body": "{{ nodes.trigger.body | json | truncate(600) }}", "severity": "info"}, 2, 1),
        _n("bad", "action.send_notification", "Report invalid payload",
           {"title": f"{event}: invalid payload", "body": "{{ nodes.check.errors | join('; ') }}", "severity": "warning"}, 2, 2),
    ]
    edges = [_e("trigger", "check"), _e("check", "send", "valid"), _e("check", "staff", "valid"), _e("check", "bad", "invalid")]
    props: dict = {"messaging_integration": _integ("Messaging integration", EMAIL if channel == "email" else [channel])}
    req = ["messaging_integration"]
    if channel == "email":
        props["email_subject"] = _text("Email subject", subject or event)
    props["business_name"] = _text("Business name")
    return _result({"nodes": nodes, "edges": edges}, props, req + ["business_name"], [channel if channel != "email" else "email"])


def delayed_followup(event: str, channel: str, to: str, default_hours: float, instruction: str, subject: str = "") -> dict:
    """Webhook event -> durable delay -> AI-personalised message."""
    nodes = [
        _n("trigger", "trigger.webhook", f"{event} webhook", {}, 0),
        _n("wait", "logic.delay", "Wait", {"seconds": "{{ vars.delay_hours | multiply(3600) }}"}, 1),
        _n("write", "ai.completion", "Write message",
           {"system": f"You write short, warm customer messages for {{{{ vars.business_name }}}}. {instruction} "
                      "Output only the message text, no preamble. Extra guidance: {{ vars.message_guidance }}",
            "prompt": "Customer data: {{ nodes.trigger.body | json }}"}, 2),
        _send(channel, "send", "Send message", to, "{{ nodes.write.text }}", 3),
    ]
    edges = [_e("trigger", "wait"), _e("wait", "write"), _e("write", "send")]
    props: dict = {"messaging_integration": _integ("Messaging integration", EMAIL if channel == "email" else [channel]),
                   "business_name": _text("Business name"), "delay_hours": _num("Delay (hours)", default_hours),
                   "message_guidance": _text("Extra guidance for the message (links, offers, tone)", multiline=True)}
    if channel == "email":
        props["email_subject"] = _text("Email subject", subject or event)
    return _result({"nodes": nodes, "edges": edges}, props, ["messaging_integration", "business_name"],
                   [channel if channel != "email" else "email"], ["AI provider"])


def lead_qualification(source: str, criteria: str) -> dict:
    """Form lead -> validate -> AI scoring -> CRM -> alert on hot leads."""
    nodes = [
        _n("trigger", "trigger.form", f"{source} form", {"required_fields": ["email"]}, 0),
        _n("check", "logic.validate", "Validate email", {"rules": [{"field": "email", "value": "{{ nodes.trigger.body.email }}", "type": "email"}]}, 1),
        _n("score", "ai.classify", "Score lead",
           {"text": "{{ nodes.trigger.body | json }}", "categories": ["hot", "warm", "cold"],
            "instructions": f"{criteria} Additional criteria: {{{{ vars.qualification_criteria }}}}"}, 2),
        _n("crm", "action.crm_create_lead", "Create CRM lead",
           {"integration_id": "{{ vars.crm_integration }}", "email": "{{ nodes.trigger.body.email }}",
            "first_name": "{{ nodes.trigger.body.first_name }}", "last_name": "{{ nodes.trigger.body.last_name }}",
            "phone": "{{ nodes.trigger.body.phone }}", "company": "{{ nodes.trigger.body.company }}"}, 3),
        _n("route", "logic.switch", "Lead temperature", {"value": "{{ nodes.score.label }}", "cases": ["hot"]}, 4),
        _n("alert", "action.send_notification", "Alert sales",
           {"title": "Hot lead: {{ nodes.trigger.body.email }}", "body": "{{ nodes.score.reason }}", "severity": "success"}, 5),
    ]
    edges = [_e("trigger", "check"), _e("check", "score", "valid"), _e("score", "crm"), _e("crm", "route"), _e("route", "alert", "hot")]
    props = {"crm_integration": _integ("CRM integration", CRM + ["mailchimp"]),
             "qualification_criteria": _text("What makes a lead hot for you?", multiline=True)}
    return _result({"nodes": nodes, "edges": edges}, props, ["crm_integration"], ["crm"], ["AI provider"])


def threshold_alert(event: str, value_path: str, default_threshold: float, item_path: str) -> dict:
    """Webhook metric -> compare with threshold -> alert."""
    nodes = [
        _n("trigger", "trigger.webhook", f"{event} webhook", {}, 0),
        _n("low", "logic.if", "Below threshold?", {"condition": _cond("{{ nodes.trigger.body.%s }}" % value_path, "lte", "{{ vars.threshold }}")}, 1),
        _n("notify", "action.send_notification", "Alert team",
           {"title": "%s: {{ nodes.trigger.body.%s }}" % (event, item_path),
            "body": "Current value: {{ nodes.trigger.body.%s }} (threshold {{ vars.threshold }})" % value_path, "severity": "warning"}, 2),
        _n("email", "action.send_email", "Email manager",
           {"integration_id": "{{ vars.email_integration }}", "to": "{{ vars.alert_email }}",
            "subject": "%s: {{ nodes.trigger.body.%s }}" % (event, item_path),
            "body": "Current value: {{ nodes.trigger.body.%s }}. Threshold: {{ vars.threshold }}." % value_path}, 2, 1),
    ]
    edges = [_e("trigger", "low"), _e("low", "notify", "true"), _e("low", "email", "true")]
    props = {"email_integration": _integ("Email integration", EMAIL), "alert_email": _text("Send alerts to"), "threshold": _num("Threshold", default_threshold)}
    return _result({"nodes": nodes, "edges": edges}, props, ["email_integration", "alert_email"], ["email"])


def ai_document(source: str, doc_name: str, instruction: str, send_to: str) -> dict:
    """Form -> AI-drafted document -> stored -> emailed."""
    nodes = [
        _n("trigger", "trigger.form", f"{source} form", {}, 0),
        _n("draft", "ai.completion", "Draft document",
           {"system": f"You are a senior consultant at {{{{ vars.business_name }}}}. {instruction} Write in Markdown. Guidance: {{{{ vars.document_guidance }}}}",
            "prompt": "Request details: {{ nodes.trigger.body | json }}"}, 1),
        _n("doc", "action.generate_document", "Store document", {"name": doc_name, "content": "{{ nodes.draft.text }}"}, 2),
        _n("email", "action.send_email", "Email document",
           {"integration_id": "{{ vars.email_integration }}", "to": send_to, "subject": doc_name, "body": "{{ nodes.doc.content }}"}, 3),
    ]
    edges = [_e("trigger", "draft"), _e("draft", "doc"), _e("doc", "email")]
    props = {"email_integration": _integ("Email integration", EMAIL), "business_name": _text("Business name"),
             "review_email": _text("Internal reviewer email"), "document_guidance": _text("Services, pricing and tone to use", multiline=True)}
    return _result({"nodes": nodes, "edges": edges}, props, ["email_integration", "business_name", "review_email"], ["email"], ["AI provider"])


def scheduled_report(title: str, cron: str, instruction: str) -> dict:
    """Schedule -> fetch data over HTTP -> AI summary -> email."""
    nodes = [
        _n("trigger", "trigger.schedule", "Schedule", {"cron": cron, "timezone": "UTC"}, 0),
        _n("fetch", "action.http_request", "Fetch data", {"method": "GET", "url": "{{ vars.data_url }}"}, 1,
           settings={"retry": {"max_attempts": 3, "allow_non_idempotent": True}}),
        _n("summary", "ai.summarize", "Summarize", {"text": "{{ nodes.fetch.body | json }}", "max_words": 250, "style": "bullets", "instructions": instruction}, 2),
        _n("email", "action.send_email", "Email report",
           {"integration_id": "{{ vars.email_integration }}", "to": "{{ vars.report_email }}", "subject": title, "body": "{{ nodes.summary.text }}"}, 3),
    ]
    edges = [_e("trigger", "fetch"), _e("fetch", "summary"), _e("summary", "email")]
    props = {"email_integration": _integ("Email integration", EMAIL), "report_email": _text("Send the report to"),
             "data_url": _text("Data source URL (returns JSON)")}
    return _result({"nodes": nodes, "edges": edges}, props, ["email_integration", "report_email", "data_url"], ["email"], ["AI provider"])


def crm_sync(event: str) -> dict:
    """Webhook -> create CRM lead/contact; failures are reported instead of lost."""
    nodes = [
        _n("trigger", "trigger.webhook", f"{event} webhook", {}, 0),
        _n("crm", "action.crm_create_lead", "Create CRM record",
           {"integration_id": "{{ vars.crm_integration }}", "email": "{{ nodes.trigger.body.email }}",
            "first_name": "{{ nodes.trigger.body.first_name }}", "last_name": "{{ nodes.trigger.body.last_name }}",
            "phone": "{{ nodes.trigger.body.phone }}", "company": "{{ nodes.trigger.body.company }}"}, 1),
        _n("failed", "action.send_notification", "Report sync failure",
           {"title": f"{event}: CRM sync failed", "body": "{{ nodes.crm.error.message }}", "severity": "error"}, 2),
    ]
    edges = [_e("trigger", "crm"), _e("crm", "failed", "error")]
    return _result({"nodes": nodes, "edges": edges}, {"crm_integration": _integ("CRM integration", CRM + ["mailchimp"])}, ["crm_integration"], ["crm"])


def record_capture(event: str, table_title: str) -> dict:
    """Webhook -> store the payload as a record in the organization's data store."""
    nodes = [
        _n("trigger", "trigger.webhook", f"{event} webhook", {}, 0),
        _n("store", "action.db_create_record", "Store record",
           {"integration_id": "{{ vars.data_integration }}", "table": "{{ vars.table }}", "data": "{{ nodes.trigger.body }}"}, 1),
        _n("failed", "action.send_notification", "Report failure",
           {"title": f"{event}: could not store record", "body": "{{ nodes.store.error.message }}", "severity": "error"}, 2),
    ]
    edges = [_e("trigger", "store"), _e("store", "failed", "error")]
    props = {"data_integration": _integ("Data integration", DATA), "table": _text(table_title)}
    return _result({"nodes": nodes, "edges": edges}, props, ["data_integration", "table"], ["database"])


def booking_from_message(provider: str, service_noun: str) -> dict:
    """Chat message -> AI extracts booking details -> calendar event + confirmation, or asks for what is missing."""
    fields = [
        {"name": "customer_name", "type": "string", "description": "Customer's name"},
        {"name": "service", "type": "string", "description": f"Requested {service_noun}"},
        {"name": "start_iso", "type": "string", "description": "Start as ISO 8601 local date-time (YYYY-MM-DDTHH:MM:SS), null if not given"},
        {"name": "end_iso", "type": "string", "description": "End as ISO 8601 local date-time; default one hour after start"},
    ]
    nodes = [
        _n("trigger", "trigger.message", "Incoming message", {"provider": provider}, 0),
        _n("details", "ai.extract", "Extract booking details",
           {"text": "{{ nodes.trigger.text }}", "fields": fields, "instructions": "Current time: {{ now }}. Business timezone: {{ vars.timezone }}."}, 1),
        _n("complete", "logic.if", "Has date and time?", {"condition": _cond("{{ nodes.details.data.start_iso }}", "is_not_empty")}, 2),
        _n("event", "action.create_calendar_event", "Create calendar event",
           {"integration_id": "{{ vars.calendar_integration }}", "title": "{{ nodes.details.data.service | default('Booking') }} - {{ nodes.details.data.customer_name | default('Customer') }}",
            "start": "{{ nodes.details.data.start_iso }}", "end": "{{ nodes.details.data.end_iso }}", "timezone": "{{ vars.timezone }}",
            "description": "Requested via chat: {{ nodes.trigger.text }}"}, 3),
        _send(provider, "confirm", "Confirm booking", "{{ nodes.trigger.from }}",
              "Thank you! Your booking with {{ vars.business_name }} is confirmed for {{ nodes.details.data.start_iso }}.", 4),
        _send(provider, "ask", "Ask for details", "{{ nodes.trigger.from }}",
              "Thanks for contacting {{ vars.business_name }}! Which date and time would you like to book?", 3, 1),
    ]
    edges = [_e("trigger", "details"), _e("details", "complete"), _e("complete", "event", "true"), _e("event", "confirm"), _e("complete", "ask", "false")]
    props = {"messaging_integration": _integ(f"{provider.title()} integration", [provider]), "calendar_integration": _integ("Calendar integration", CALENDAR),
             "business_name": _text("Business name"), "timezone": _text("Business timezone (IANA)", "UTC")}
    return _result({"nodes": nodes, "edges": edges}, props, ["messaging_integration", "calendar_integration", "business_name"], [provider, "calendar"], ["AI provider"])


def approval_flow(source: str, title: str) -> dict:
    """Form request -> human approval -> email the outcome."""
    nodes = [
        _n("trigger", "trigger.form", f"{source} form", {"required_fields": ["email"]}, 0),
        _n("approve", "logic.approval", "Manager approval", {"title": "%s from {{ nodes.trigger.body.email }}" % title,
                                                             "summary": "{{ nodes.trigger.body | json }}", "timeout_hours": 72}, 1),
        _n("yes", "action.send_email", "Email approval",
           {"integration_id": "{{ vars.email_integration }}", "to": "{{ nodes.trigger.body.email }}", "subject": f"{title} approved",
            "body": "Your request was approved. {{ nodes.approve.note }}"}, 2),
        _n("no", "action.send_email", "Email rejection",
           {"integration_id": "{{ vars.email_integration }}", "to": "{{ nodes.trigger.body.email }}", "subject": f"{title} not approved",
            "body": "Your request was not approved. {{ nodes.approve.note }}"}, 2, 1),
    ]
    edges = [_e("trigger", "approve"), _e("approve", "yes", "approved"), _e("approve", "no", "rejected")]
    return _result({"nodes": nodes, "edges": edges}, {"email_integration": _integ("Email integration", EMAIL)}, ["email_integration"], ["email"])


def triage(event: str, categories: list[str], urgent: str) -> dict:
    """Webhook text -> AI category -> notify; escalate the urgent category by email."""
    nodes = [
        _n("trigger", "trigger.webhook", f"{event} webhook", {}, 0),
        _n("classify", "ai.classify", "Classify", {"text": "{{ nodes.trigger.body | json }}", "categories": categories,
                                                   "instructions": "{{ vars.triage_guidance }}"}, 1),
        _n("note", "action.send_notification", "Notify team",
           {"title": "%s: {{ nodes.classify.label }}" % event, "body": "{{ nodes.classify.reason }}", "severity": "info"}, 2),
        _n("route", "logic.switch", "Urgent?", {"value": "{{ nodes.classify.label }}", "cases": [urgent]}, 2, 1),
        _n("escalate", "action.send_email", "Escalate",
           {"integration_id": "{{ vars.email_integration }}", "to": "{{ vars.escalation_email }}", "subject": f"Urgent: {event}",
            "body": "{{ nodes.classify.reason }}\n\n{{ nodes.trigger.body | json }}"}, 3, 1),
    ]
    edges = [_e("trigger", "classify"), _e("classify", "note"), _e("classify", "route"), _e("route", "escalate", urgent)]
    props = {"email_integration": _integ("Email integration", EMAIL), "escalation_email": _text("Escalate urgent items to"),
             "triage_guidance": _text("How should items be categorised?", multiline=True)}
    return _result({"nodes": nodes, "edges": edges}, props, ["email_integration", "escalation_email"], ["email"], ["AI provider"])


def order_processing() -> dict:
    """Verified Stripe payment event -> record the order -> email the customer."""
    nodes = [
        _n("trigger", "trigger.payment", "Stripe event", {"provider": "stripe", "event_types": ["checkout.session.completed"]}, 0),
        _n("store", "action.db_create_record", "Record order",
           {"integration_id": "{{ vars.data_integration }}", "table": "{{ vars.orders_table }}",
            "data": {"stripe_session_id": "{{ nodes.trigger.body.data.object.id }}", "email": "{{ nodes.trigger.body.data.object.customer_details.email }}",
                     "amount_total": "{{ nodes.trigger.body.data.object.amount_total }}", "currency": "{{ nodes.trigger.body.data.object.currency }}"}}, 1),
        _n("email", "action.send_email", "Confirm order",
           {"integration_id": "{{ vars.email_integration }}", "to": "{{ nodes.trigger.body.data.object.customer_details.email }}",
            "subject": "Your order with {{ vars.business_name }}", "body": "Thank you for your order! We have received your payment and are preparing it now."}, 2),
        _n("failed", "action.send_notification", "Report failure",
           {"title": "Order could not be recorded", "body": "{{ nodes.store.error.message }}", "severity": "error"}, 2, 1),
    ]
    edges = [_e("trigger", "store"), _e("store", "email"), _e("store", "failed", "error")]
    props = {"data_integration": _integ("Data integration", DATA), "orders_table": _text("Orders table", "orders"),
             "email_integration": _integ("Email integration", EMAIL), "business_name": _text("Business name")}
    return _result({"nodes": nodes, "edges": edges}, props, ["data_integration", "email_integration", "business_name"], ["stripe", "database", "email"],
                   ["Stripe webhook signing secret"])


def relay(event: str) -> dict:
    """Webhook -> reshape -> forward to another HTTP API."""
    nodes = [
        _n("trigger", "trigger.webhook", f"{event} webhook", {}, 0),
        _n("shape", "logic.transform", "Shape payload", {"fields": {"source": "wae", "received_at": "{{ now }}", "data": "{{ nodes.trigger.body }}"}}, 1),
        _n("forward", "action.http_request", "Forward", {"method": "POST", "url": "{{ vars.target_url }}", "body": "{{ nodes.shape }}"}, 2),
    ]
    return _result({"nodes": nodes, "edges": [_e("trigger", "shape"), _e("shape", "forward")]}, {"target_url": _text("Target URL")}, ["target_url"], [])


def channel_post(event: str, provider: str, text: str) -> dict:
    """Webhook -> post to a team channel."""
    nodes = [_n("trigger", "trigger.webhook", f"{event} webhook", {}, 0),
             _send(provider, "post", "Post update", "{{ vars.channel }}", text, 1)]
    props = {"messaging_integration": _integ(f"{provider.title()} integration", [provider]), "channel": _text("Channel / chat id")}
    return _result({"nodes": nodes, "edges": [_e("trigger", "post")]}, props, ["messaging_integration", "channel"], [provider])


def calendar_scheduling(event: str, title: str) -> dict:
    """Webhook request -> calendar event -> email confirmation."""
    nodes = [
        _n("trigger", "trigger.webhook", f"{event} webhook", {}, 0),
        _n("check", "logic.validate", "Validate request", {"rules": [
            {"field": "email", "value": "{{ nodes.trigger.body.email }}", "type": "email"},
            {"field": "start", "value": "{{ nodes.trigger.body.start }}"}, {"field": "end", "value": "{{ nodes.trigger.body.end }}"}]}, 1),
        _n("event", "action.create_calendar_event", "Create event",
           {"integration_id": "{{ vars.calendar_integration }}", "title": "%s - {{ nodes.trigger.body.name | default('Client') }}" % title,
            "start": "{{ nodes.trigger.body.start }}", "end": "{{ nodes.trigger.body.end }}", "timezone": "{{ vars.timezone }}",
            "attendees": ["{{ nodes.trigger.body.email }}"]}, 2),
        _n("email", "action.send_email", "Confirm by email",
           {"integration_id": "{{ vars.email_integration }}", "to": "{{ nodes.trigger.body.email }}", "subject": f"{title} confirmed",
            "body": "Your {{ vars.business_name }} appointment is confirmed for {{ nodes.trigger.body.start }}."}, 3),
    ]
    edges = [_e("trigger", "check"), _e("check", "event", "valid"), _e("event", "email")]
    props = {"calendar_integration": _integ("Calendar integration", CALENDAR), "email_integration": _integ("Email integration", EMAIL),
             "business_name": _text("Business name"), "timezone": _text("Timezone (IANA)", "UTC")}
    return _result({"nodes": nodes, "edges": edges}, props, ["calendar_integration", "email_integration", "business_name"], ["calendar", "email"])


def text_extraction(event: str, fields: list[tuple[str, str]], table_title: str) -> dict:
    """Webhook document text -> AI extraction -> stored record."""
    nodes = [
        _n("trigger", "trigger.webhook", f"{event} webhook", {}, 0),
        _n("extract", "ai.extract", "Extract fields", {"text": "{{ nodes.trigger.body.text }}",
                                                       "fields": [{"name": n, "type": "string", "description": d} for n, d in fields]}, 1),
        _n("store", "action.db_create_record", "Store result", {"integration_id": "{{ vars.data_integration }}", "table": "{{ vars.table }}",
                                                               "data": "{{ nodes.extract.data }}"}, 2),
    ]
    props = {"data_integration": _integ("Data integration", DATA), "table": _text(table_title)}
    return _result({"nodes": nodes, "edges": [_e("trigger", "extract"), _e("extract", "store")]}, props, ["data_integration", "table"], ["database"], ["AI provider"])


# ----------------------------------------------------------------------------- AI team patterns


def _member(title: str) -> dict:
    """A workflow variable holding one AI team member's agent id (filled in when the team is installed)."""
    return {"type": "string", "title": title, "x-agent": True}


def team_routine(cron: str, member: str, member_title: str, prompt: str, report_title: str) -> dict:
    """Schedule (UTC) -> one team member works -> the result is reported to the organization."""
    var = f"{member}_agent"
    nodes = [
        _n("trigger", "trigger.schedule", "Every week", {"cron": cron, "timezone": "UTC"}, 0),
        _n("work", "ai.agent", member_title, {"agent_id": "{{ vars.%s }}" % var, "input": prompt}, 1),
        _n("report", "action.send_notification", "Report to the team", {"title": report_title, "body": "{{ nodes.work.text | truncate(3000) }}"}, 2),
    ]
    return _result({"nodes": nodes, "edges": [_e("trigger", "work"), _e("work", "report")]}, {var: _member(member_title)}, [var], [], ["AI provider"])


def content_ready() -> dict:
    """content.due (an approved calendar item's time has come) -> hand it to a person to post."""
    nodes = [
        _n("trigger", "trigger.app_event", "Approved content is due", {"event_type": "content.due"}, 0),
        _n("notify", "action.send_notification", "Ask a person to publish",
           {"title": "Ready to publish on {{ nodes.trigger.data.channel }}: {{ nodes.trigger.data.title }}",
            "body": "{{ nodes.trigger.data.body | truncate(2500) }}\n\nMedia brief: {{ nodes.trigger.data.media_notes | default('none') }}\n\n"
                    "After posting it, mark it as published in the Content calendar.", "severity": "info"}, 1),
    ]
    return _result({"nodes": nodes, "edges": [_e("trigger", "notify")]}, {}, [], [])


def lead_intake() -> dict:
    """Website form -> Lead Scorer agent (score + first reply draft) -> alert on hot leads."""
    nodes = [
        _n("trigger", "trigger.form", "Website enquiry", {"required_fields": ["email"]}, 0),
        _n("score", "ai.agent", "Lead Scorer", {"agent_id": "{{ vars.lead_scorer_agent }}",
                                                "input": "New enquiry from the website form:\n{{ nodes.trigger.body | json }}",
                                                "conversation_ref": "{{ nodes.trigger.body.email }}"}, 1),
        _n("hot", "logic.if", "Hot lead?", {"condition": _cond("{{ nodes.score.text }}", "starts_with", "HOT")}, 2),
        _n("alert", "action.send_notification", "Alert the team",
           {"title": "Hot lead: {{ nodes.trigger.body.email }}", "body": "{{ nodes.score.text | truncate(2500) }}", "severity": "success"}, 3),
    ]
    edges = [_e("trigger", "score"), _e("score", "hot"), _e("hot", "alert", "true")]
    return _result({"nodes": nodes, "edges": edges}, {"lead_scorer_agent": _member("Lead Scorer")}, ["lead_scorer_agent"], [], ["AI provider"])


# ----------------------------------------------------------------------------- library

_SETUP = ("1. Connect the required integrations under Integrations.\n2. Install the template and fill in its configuration.\n"
          "3. Open the workflow, review each step, then Test, Publish and Activate.\n"
          "4. For webhook or message triggers, copy the trigger URL from the builder into the source system.")


def _t(key: str, name: str, category: str, industry: str | None, description: str, built: dict) -> dict:
    return {"key": key, "name": name, "category": category, "industry": industry, "description": description,
            "setup_instructions": _SETUP, "pricing": {"model": "included", "setup_fee_cents": None, "monthly_cents": None}, **built}


TEMPLATES: list[dict] = [
    # Hospitality
    _t("hosp-guest-assistant", "AI Guest Assistant", "Hospitality", "hospitality", "Answers guest questions on Telegram around the clock using your property information.",
       assistant("telegram", "guest assistant", "Help guests with check-in, amenities, local tips and house rules.")),
    _t("hosp-whatsapp-concierge", "WhatsApp Concierge", "Hospitality", "hospitality", "A WhatsApp concierge that answers guests and offers to arrange services.",
       assistant("whatsapp", "concierge", "Help with requests such as transfers, late check-out, restaurant suggestions and housekeeping.")),
    _t("hosp-automated-checkin", "Automated Check-in", "Hospitality", "hospitality", "Sends check-in instructions when your PMS reports a new booking.",
       event_notification("New booking", ["email", "guest_name", "check_in"], "email", "{{ nodes.trigger.body.email }}",
                          "Dear {{ nodes.trigger.body.guest_name }},\n\nYour stay at {{ vars.business_name }} starts on {{ nodes.trigger.body.check_in }}. "
                          "Reply to this email if you need anything before you arrive.", "Your check-in details")),
    _t("hosp-booking-notifications", "Booking Notifications", "Hospitality", "hospitality", "Confirms bookings to guests on WhatsApp and alerts the front desk.",
       event_notification("Booking update", ["phone", "guest_name"], "whatsapp", "{{ nodes.trigger.body.phone }}",
                          "Hello {{ nodes.trigger.body.guest_name }}, your booking with {{ vars.business_name }} is confirmed. We look forward to welcoming you!")),
    _t("hosp-guest-followups", "Guest Follow-ups", "Hospitality", "hospitality", "Sends a personal thank-you email after check-out.",
       delayed_followup("Check-out", "email", "{{ nodes.trigger.body.email }}", 4, "Thank the guest for their stay and invite them back.", "Thank you for staying with us")),
    _t("hosp-review-collection", "Review Collection", "Hospitality", "hospitality", "Asks guests for a review one day after check-out.",
       delayed_followup("Check-out", "email", "{{ nodes.trigger.body.email }}", 24, "Politely ask for an online review and include the review link from the guidance.", "How was your stay?")),
    _t("hosp-upselling", "Upselling", "Hospitality", "hospitality", "Offers relevant extras on WhatsApp shortly after a booking.",
       delayed_followup("New booking", "whatsapp", "{{ nodes.trigger.body.phone }}", 2, "Offer one or two relevant paid extras (upgrade, breakfast, transfer) without being pushy.")),
    # Restaurants
    _t("rest-reservation-assistant", "Reservation Assistant", "Restaurants", "restaurants", "Takes table reservations from chat and puts them in your calendar.",
       booking_from_message("whatsapp", "table reservation")),
    _t("rest-customer-support", "Restaurant Customer Support", "Restaurants", "restaurants", "Answers menu, allergen and opening-hours questions on WhatsApp.",
       assistant("whatsapp", "host", "Answer questions about the menu, allergens, opening hours, location and reservations.")),
    _t("rest-order-notifications", "Order Notifications", "Restaurants", "restaurants", "Tells customers on WhatsApp that their order was received and alerts the kitchen.",
       event_notification("New order", ["phone", "order_number"], "whatsapp", "{{ nodes.trigger.body.phone }}",
                          "Thanks for your order #{{ nodes.trigger.body.order_number }} at {{ vars.business_name }}! We are preparing it now.")),
    _t("rest-inventory-alerts", "Inventory Alerts", "Restaurants", "restaurants", "Alerts the manager when stock drops below a threshold.",
       threshold_alert("Low stock", "quantity", 10, "item")),
    _t("rest-customer-retention", "Customer Retention", "Restaurants", "restaurants", "Invites guests back with a personal message a few weeks after their visit.",
       delayed_followup("Visit", "whatsapp", "{{ nodes.trigger.body.phone }}", 504, "Invite the guest back and mention the current offer from the guidance.")),
    _t("rest-review-collection", "Restaurant Review Collection", "Restaurants", "restaurants", "Asks diners for a review the day after their visit.",
       delayed_followup("Visit", "whatsapp", "{{ nodes.trigger.body.phone }}", 20, "Ask for a short online review and include the review link from the guidance.")),
    # Beauty & Salons
    _t("salon-appointment-booking", "Appointment Booking", "Beauty & Salons", "beauty", "Books salon appointments from WhatsApp messages into your calendar.",
       booking_from_message("whatsapp", "salon service")),
    _t("salon-appointment-reminders", "Appointment Reminders", "Beauty & Salons", "beauty", "Reminds clients about upcoming appointments on WhatsApp.",
       event_notification("Appointment reminder", ["phone", "client_name", "appointment_time"], "whatsapp", "{{ nodes.trigger.body.phone }}",
                          "Hi {{ nodes.trigger.body.client_name }}, a reminder of your appointment at {{ vars.business_name }} on {{ nodes.trigger.body.appointment_time }}. Reply if you need to reschedule.")),
    _t("salon-cancellation-management", "Cancellation Management", "Beauty & Salons", "beauty", "Acknowledges cancellations, offers to rebook and tells the team a slot opened.",
       event_notification("Appointment cancelled", ["phone", "client_name"], "whatsapp", "{{ nodes.trigger.body.phone }}",
                          "Hi {{ nodes.trigger.body.client_name }}, we have cancelled your appointment at {{ vars.business_name }}. Reply with a day that suits you and we will find a new time.")),
    _t("salon-customer-crm", "Customer CRM", "Beauty & Salons", "beauty", "Adds every new client to your CRM automatically.", crm_sync("New client")),
    _t("salon-loyalty", "Loyalty Automation", "Beauty & Salons", "beauty", "Thanks clients after a visit with a loyalty message and reward details.",
       delayed_followup("Visit completed", "whatsapp", "{{ nodes.trigger.body.phone }}", 1, "Thank the client and explain the loyalty reward described in the guidance.")),
    _t("salon-followup-campaigns", "Follow-up Campaigns", "Beauty & Salons", "beauty", "Nudges clients to rebook a few weeks after their last visit.",
       delayed_followup("Visit completed", "whatsapp", "{{ nodes.trigger.body.phone }}", 672, "Suggest rebooking the same service and mention any current offer from the guidance.")),
    # Real Estate
    _t("re-lead-qualification", "Lead Qualification", "Real Estate", "real_estate", "Scores property enquiries, creates CRM leads and alerts agents about hot ones.",
       lead_qualification("Property enquiry", "Hot leads have a clear budget, a timeline under three months and a specific property or area in mind.")),
    _t("re-property-matching", "Property Matching", "Real Estate", "real_estate", "Drafts a tailored shortlist email from the buyer's requirements and your listings.",
       ai_document("Buyer requirements", "Property shortlist", "Match the buyer's requirements against the listings in the guidance and present the three best options with reasons.",
                   "{{ vars.review_email }}")),
    _t("re-viewing-scheduling", "Viewing Scheduling", "Real Estate", "real_estate", "Creates calendar events for viewing requests and confirms by email.",
       calendar_scheduling("Viewing request", "Property viewing")),
    _t("re-crm-sync", "CRM Synchronization", "Real Estate", "real_estate", "Pushes portal and website leads into your CRM.", crm_sync("Portal lead")),
    _t("re-lead-followups", "Lead Follow-ups", "Real Estate", "real_estate", "Follows up with enquirers by email two days later.",
       delayed_followup("Enquiry", "email", "{{ nodes.trigger.body.email }}", 48, "Follow up on the property enquiry and offer a call or viewing.", "Following up on your enquiry")),
    _t("re-document-processing", "Document Processing", "Real Estate", "real_estate", "Extracts key terms from contract or listing text into your database.",
       text_extraction("Document", [("property_address", "Address of the property"), ("price", "Price or rent amount"), ("parties", "Names of the parties"),
                                    ("key_dates", "Important dates")], "Table for extracted documents")),
    # E-commerce
    _t("ecom-order-processing", "Order Processing", "E-commerce", "ecommerce", "Records paid Stripe orders and emails the customer a confirmation.", order_processing()),
    _t("ecom-inventory-sync", "Inventory Synchronization", "E-commerce", "ecommerce", "Stores inventory updates from your shop in your own data store.",
       record_capture("Inventory update", "Inventory table")),
    _t("ecom-abandoned-cart", "Abandoned Cart Recovery", "E-commerce", "ecommerce", "Emails shoppers a personal reminder a few hours after they abandon a cart.",
       delayed_followup("Cart abandoned", "email", "{{ nodes.trigger.body.email }}", 3, "Remind the shopper about the items in their cart and invite them to complete the order.", "You left something behind")),
    _t("ecom-customer-support", "E-commerce Customer Support", "E-commerce", "ecommerce", "Answers order, shipping and returns questions on WhatsApp.",
       assistant("whatsapp", "support agent", "Answer questions about shipping, returns, sizing and order status policies.")),
    _t("ecom-review-collection", "Product Review Collection", "E-commerce", "ecommerce", "Asks customers for a product review a week after delivery.",
       delayed_followup("Order delivered", "email", "{{ nodes.trigger.body.email }}", 168, "Ask for a product review and include the review link from the guidance.", "How do you like your order?")),
    _t("ecom-customer-retention", "E-commerce Customer Retention", "E-commerce", "ecommerce", "Wins customers back with a personal email a month after their order.",
       delayed_followup("Order delivered", "email", "{{ nodes.trigger.body.email }}", 720, "Invite the customer back, referencing what they bought and the offer in the guidance.", "We thought you might like this")),
    # Digital Agency
    _t("agency-client-onboarding", "Client Onboarding", "Operations", "digital_agency", "Welcomes new clients by email and tells the team onboarding has started.",
       event_notification("New client", ["email", "name"], "email", "{{ nodes.trigger.body.email }}",
                          "Hi {{ nodes.trigger.body.name }},\n\nWelcome to {{ vars.business_name }}! Your account manager will contact you within one business day to schedule the kick-off.",
                          "Welcome aboard")),
    _t("agency-lead-management", "Lead Management", "Sales", "digital_agency", "Scores inbound agency leads, creates CRM records and alerts sales about hot ones.",
       lead_qualification("Agency enquiry", "Hot leads have a defined project, a stated budget and decision-making authority.")),
    _t("agency-proposal-generation", "Proposal Generation", "Sales", "digital_agency", "Drafts a proposal from a brief and emails it to an internal reviewer.",
       ai_document("Project brief", "Project proposal", "Write a clear proposal with scope, deliverables, timeline and investment.", "{{ vars.review_email }}")),
    _t("agency-project-notifications", "Project Notifications", "Operations", "digital_agency", "Posts project updates from your PM tool to a Slack channel.",
       channel_post("Project update", "slack", "{{ nodes.trigger.body.project | default('Project') }}: {{ nodes.trigger.body.message }}")),
    _t("agency-reporting", "Reporting", "Operations", "digital_agency", "Emails an AI-written weekly summary of your reporting data every Monday.",
       scheduled_report("Weekly performance report", "0 8 * * 1", "Highlight the most important changes and recommended actions for the client.")),
    _t("agency-client-followups", "Client Follow-ups", "Customer Support", "digital_agency", "Checks in with clients by email a week after a delivery.",
       delayed_followup("Delivery", "email", "{{ nodes.trigger.body.email }}", 168, "Check that the client is happy with the delivery and ask for feedback.", "Checking in")),
    # Cross-industry
    _t("support-ticket-triage", "Support Ticket Triage", "Customer Support", None, "Categorises incoming tickets with AI and escalates urgent ones.",
       triage("Support ticket", ["urgent", "billing", "technical", "general"], "urgent")),
    _t("marketing-subscriber-sync", "Newsletter Subscriber Sync", "Marketing", None, "Adds new sign-ups to your marketing audience or CRM.", crm_sync("New subscriber")),
    _t("finance-expense-approval", "Expense Approval", "Finance", None, "Routes expense requests to a manager for approval and emails the outcome.",
       approval_flow("Expense request", "Expense request")),
    _t("hr-leave-approval", "Leave Request Approval", "HR", None, "Routes leave requests to a manager for approval and emails the outcome.",
       approval_flow("Leave request", "Leave request")),
    _t("recruitment-application-screening", "Application Screening", "Recruitment", None, "Scores job applications with AI and escalates strong candidates.",
       triage("Job application", ["strong", "possible", "not_a_fit"], "strong")),
    _t("health-appointment-reminders", "Appointment Reminders (Clinic Admin)", "Healthcare Administration", "healthcare",
       "Sends administrative appointment reminders by email. Do not include clinical details.",
       event_notification("Appointment reminder", ["email", "patient_name", "appointment_time"], "email", "{{ nodes.trigger.body.email }}",
                          "Dear {{ nodes.trigger.body.patient_name }},\n\nThis is a reminder of your appointment at {{ vars.business_name }} on {{ nodes.trigger.body.appointment_time }}.",
                          "Appointment reminder")),
    _t("travel-booking-confirmation", "Trip Booking Confirmation", "Travel & Tourism", "travel", "Confirms trip bookings by email and alerts the operations team.",
       event_notification("Trip booking", ["email", "traveller_name", "trip"], "email", "{{ nodes.trigger.body.email }}",
                          "Dear {{ nodes.trigger.body.traveller_name }},\n\nYour booking for {{ nodes.trigger.body.trip }} with {{ vars.business_name }} is confirmed.",
                          "Your booking is confirmed")),
    _t("ops-daily-digest", "Daily Operations Digest", "Operations", None, "Emails an AI summary of your operations data every morning.",
       scheduled_report("Daily operations digest", "0 7 * * *", "Summarise what needs attention today.")),
    _t("custom-webhook-relay", "Webhook Relay", "Custom Integrations", None, "Receives a webhook, reshapes the payload and forwards it to another API.", relay("Inbound")),
    # AI Marketing Team (installed with the team: the *_agent variables are filled in automatically)
    _t("mkt-weekly-plan", "Weekly Content Plan", "Marketing", "marketing",
       "Every Monday the Marketing Director briefs the specialists and fills the content calendar for approval.",
       team_routine("0 7 * * 1", "director", "Marketing Director",
                    "A new week starts ({{ nodes.trigger.scheduled_for }}). Check the playbook and last week's learnings, then plan this week's "
                    "content: get themes from the Trend Spotter, the audience's questions from the Audience Researcher and phrases from the "
                    "Keyword Finder, then have the Post Writer and SEO Writer put the pieces in the calendar for approval. Summarise the plan.",
                    "This week's content plan is ready for approval")),
    _t("mkt-newsletter", "Weekly Newsletter Draft", "Marketing", "marketing",
       "Every Wednesday the Email Writer drafts the newsletter and puts it in the calendar for approval.",
       team_routine("0 9 * * 3", "email_writer", "Email Writer",
                    "Draft this week's newsletter from the playbook and this week's calendar. Schedule it for Thursday 09:00 UTC.",
                    "Newsletter draft ready for approval")),
    _t("mkt-content-ready", "Publish Approved Content", "Marketing", "marketing",
       "When approved content is due, sends it to the team ready to post, with the media brief.", content_ready()),
    _t("mkt-lead-intake", "Lead Intake and Scoring", "Marketing", "marketing",
       "Scores website enquiries with the Lead Scorer, drafts the first reply and alerts the team about hot leads.", lead_intake()),
    _t("mkt-weekly-review", "Weekly Performance Review", "Marketing", "marketing",
       "Every Friday the Performance Analyst reviews the week and saves learnings that shape next week's plan.",
       team_routine("0 15 * * 5", "analyst", "Performance Analyst",
                    "Review the past 7 days of the content calendar and record what the team should repeat, drop and test next.",
                    "Weekly marketing review")),
]

PACKAGES: list[dict] = [
    {"key": "hospitality", "name": "Hospitality Automation", "industry": "hospitality",
     "description": "Guest communication from booking to review for hotels, villas and rentals.",
     "templates": ["hosp-guest-assistant", "hosp-whatsapp-concierge", "hosp-automated-checkin", "hosp-booking-notifications",
                   "hosp-guest-followups", "hosp-review-collection", "hosp-upselling"]},
    {"key": "restaurant", "name": "Restaurant Automation", "industry": "restaurants",
     "description": "Reservations, guest support, order updates and retention for restaurants.",
     "templates": ["rest-reservation-assistant", "rest-customer-support", "rest-order-notifications", "rest-inventory-alerts",
                   "rest-customer-retention", "rest-review-collection"]},
    {"key": "beauty-salon", "name": "Beauty & Salon Automation", "industry": "beauty",
     "description": "Booking, reminders, cancellations and loyalty for salons and studios.",
     "templates": ["salon-appointment-booking", "salon-appointment-reminders", "salon-cancellation-management", "salon-customer-crm",
                   "salon-loyalty", "salon-followup-campaigns"]},
    {"key": "real-estate", "name": "Real Estate Automation", "industry": "real_estate",
     "description": "Lead handling, matching, viewings and documents for agencies and brokers.",
     "templates": ["re-lead-qualification", "re-property-matching", "re-viewing-scheduling", "re-crm-sync", "re-lead-followups", "re-document-processing"]},
    {"key": "ecommerce", "name": "E-commerce Automation", "industry": "ecommerce",
     "description": "Orders, inventory, cart recovery, support and retention for online shops.",
     "templates": ["ecom-order-processing", "ecom-inventory-sync", "ecom-abandoned-cart", "ecom-customer-support", "ecom-review-collection",
                   "ecom-customer-retention"]},
    {"key": "digital-agency", "name": "Digital Agency Automation", "industry": "digital_agency",
     "description": "Onboarding, lead management, proposals, notifications and reporting for agencies.",
     "templates": ["agency-client-onboarding", "agency-lead-management", "agency-proposal-generation", "agency-project-notifications",
                   "agency-reporting", "agency-client-followups"]},
    {"key": "ai-marketing-team", "name": "AI Marketing Team", "industry": "marketing", "team": "marketing",
     "description": "A Marketing Director agent that briefs nine specialists (research, posts, email, SEO, leads, booking, analytics), "
                    "a content calendar where you approve every piece before it goes out, and a weekly plan-publish-review rhythm.",
     "templates": ["mkt-weekly-plan", "mkt-newsletter", "mkt-content-ready", "mkt-lead-intake", "mkt-weekly-review"]},
]

CATEGORIES = ["Customer Support", "Sales", "Marketing", "Finance", "HR", "Operations", "E-commerce", "Hospitality", "Restaurants",
              "Beauty & Salons", "Real Estate", "Healthcare Administration", "Travel & Tourism", "Recruitment", "Custom Integrations"]
