"""Integration provider catalog and adapters.

Each provider declares how it authenticates (fields / OAuth), how a connection
is tested, and which actions it supports. Adapters make real calls to the
providers' public APIs through the SSRF-guarded HTTP client; credentials are
injected here and never returned to callers. An integration is only reported
as connected after its ``test`` call succeeds against the provider.

Requests are pinned to the provider's own host (``base_url``): the generic
``api_request`` action accepts a *path*, never a full URL, so stored
credentials cannot be sent to another host.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Callable

from app.core import http
from app.core.errors import NodeError


@dataclass
class Field:
    name: str
    label: str
    secret: bool = False
    required: bool = True
    help: str = ""
    placeholder: str = ""


@dataclass
class OAuthDef:
    group: str  # env: WAE_OAUTH_<GROUP>_CLIENT_ID / _CLIENT_SECRET
    authorize_url: str
    token_url: str
    scopes: list[str]
    extra_params: dict = field(default_factory=dict)
    pkce: bool = True
    revoke_url: str = ""


@dataclass
class Conn:
    provider_key: str
    config: dict
    secret: dict

    def get(self, name: str, default: Any = "") -> Any:
        v = self.secret.get(name)
        return v if v not in (None, "") else self.config.get(name, default)

    def secrets(self) -> set[str]:
        return {str(v) for v in self.secret.values() if isinstance(v, str) and len(v) >= 6}


class Adapter:
    service = "Service"
    base_url = ""
    timeout = 30.0

    def base(self, c: Conn) -> str:
        return self.base_url

    def headers(self, c: Conn) -> dict:
        token = c.get("access_token") or c.get("api_key")
        return {"Authorization": f"Bearer {token}"} if token else {}

    def call(self, c: Conn, method: str, path: str, *, ok: tuple[int, ...] = (), **kw) -> Any:
        if not path.startswith("/"):
            raise NodeError("API path must start with '/'", code="invalid_config")
        headers = {**self.headers(c), **(kw.pop("headers", None) or {})}
        resp = http.request(method, self.base(c).rstrip("/") + path, headers=headers, timeout=kw.pop("timeout", self.timeout), **kw)
        if resp.status_code not in ok:
            http.raise_for_status(resp, self.service)
        return resp

    def json(self, c: Conn, method: str, path: str, **kw) -> Any:
        resp = self.call(c, method, path, **kw)
        try:
            return resp.json() if resp.content else {}
        except ValueError:
            return {"text": resp.text[:2000]}

    def test(self, c: Conn) -> dict:
        raise NodeError(f"{self.service} does not support connection testing", code="not_supported")

    def actions(self) -> dict[str, Callable[[Conn, dict], dict]]:
        return {"api_request": self.api_request}

    def api_request(self, c: Conn, p: dict) -> dict:
        resp = self.call(c, str(p.get("method") or "GET"), str(p.get("path") or "/"), params=p.get("query") or None,
                         json=p.get("body") if p.get("body") not in (None, "") else None)
        try:
            body = resp.json() if resp.content else None
        except ValueError:
            body = resp.text[:20000]
        return {"status": resp.status_code, "body": body}


def _mime(p: dict, sender: str = "") -> EmailMessage:
    msg = EmailMessage()
    if sender:
        msg["From"] = sender
    msg["To"] = p["to"] if isinstance(p["to"], str) else ", ".join(p["to"])
    if p.get("cc"):
        msg["Cc"] = p["cc"]
    msg["Subject"] = str(p.get("subject") or "").replace("\n", " ").replace("\r", " ")
    msg.set_content(str(p.get("body") or ""))
    if p.get("html"):
        msg.add_alternative(str(p["html"]), subtype="html")
    return msg


def _need(p: dict, *names: str) -> None:
    missing = [n for n in names if p.get(n) in (None, "")]
    if missing:
        raise NodeError(f"Missing required field(s): {', '.join(missing)}", code="invalid_config")


# ------------------------------------------------------------------ Google


class Google(Adapter):
    service = "Google"

    def test(self, c: Conn) -> dict:
        resp = http.request("GET", "https://www.googleapis.com/oauth2/v3/userinfo", headers=self.headers(c), timeout=15)
        http.raise_for_status(resp, "Google")
        return {"account": resp.json().get("email")}


class Gmail(Google):
    service, base_url = "Gmail", "https://gmail.googleapis.com"

    def actions(self):
        return {**super().actions(), "send_email": self.send_email}

    def send_email(self, c: Conn, p: dict) -> dict:
        _need(p, "to", "subject")
        raw = base64.urlsafe_b64encode(_mime(p).as_bytes()).decode()
        data = self.json(c, "POST", "/gmail/v1/users/me/messages/send", json={"raw": raw})
        return {"message_id": data.get("id"), "thread_id": data.get("threadId")}


class GoogleCalendar(Google):
    service, base_url = "Google Calendar", "https://www.googleapis.com"

    def actions(self):
        return {**super().actions(), "create_calendar_event": self.create_event}

    def create_event(self, c: Conn, p: dict) -> dict:
        _need(p, "title", "start", "end")
        tz = p.get("timezone") or "UTC"
        body = {"summary": p["title"], "description": p.get("description") or "", "location": p.get("location") or "",
                "start": {"dateTime": p["start"], "timeZone": tz}, "end": {"dateTime": p["end"], "timeZone": tz}}
        if p.get("attendees"):
            body["attendees"] = [{"email": e} for e in p["attendees"]]
        cal = p.get("calendar_id") or "primary"
        data = self.json(c, "POST", f"/calendar/v3/calendars/{cal}/events", json=body)
        return {"event_id": data.get("id"), "link": data.get("htmlLink")}


class GoogleDrive(Google):
    service, base_url = "Google Drive", "https://www.googleapis.com"

    def actions(self):
        return {**super().actions(), "upload_file": self.upload_file}

    def upload_file(self, c: Conn, p: dict) -> dict:
        _need(p, "name", "content")
        import json as _json

        meta = {"name": p["name"]}
        if p.get("folder_id"):
            meta["parents"] = [p["folder_id"]]
        files = {
            "metadata": (None, _json.dumps(meta), "application/json; charset=UTF-8"),
            "file": (p["name"], str(p["content"]).encode(), p.get("content_type") or "text/plain"),
        }
        data = self.json(c, "POST", "/upload/drive/v3/files", params={"uploadType": "multipart", "fields": "id,name,webViewLink"}, files=files)
        return {"file_id": data.get("id"), "name": data.get("name"), "link": data.get("webViewLink")}


class GoogleSheets(Google):
    service, base_url = "Google Sheets", "https://sheets.googleapis.com"

    def actions(self):
        return {**super().actions(), "create_record": self.append_row, "update_record": self.update_range}

    def append_row(self, c: Conn, p: dict) -> dict:
        _need(p, "table", "data")
        sheet_id = p.get("database") or c.get("spreadsheet_id")
        if not sheet_id:
            raise NodeError("A spreadsheet id is required (node 'database' field or integration setting)", code="invalid_config")
        row = list(p["data"].values()) if isinstance(p["data"], dict) else list(p["data"])
        data = self.json(c, "POST", f"/v4/spreadsheets/{sheet_id}/values/{p['table']}:append",
                         params={"valueInputOption": "USER_ENTERED"}, json={"values": [row]})
        return {"updated_range": (data.get("updates") or {}).get("updatedRange")}

    def update_range(self, c: Conn, p: dict) -> dict:
        _need(p, "table", "record_id", "data")
        sheet_id = p.get("database") or c.get("spreadsheet_id")
        row = list(p["data"].values()) if isinstance(p["data"], dict) else list(p["data"])
        rng = f"{p['table']}!{p['record_id']}"
        data = self.json(c, "PUT", f"/v4/spreadsheets/{sheet_id}/values/{rng}", params={"valueInputOption": "USER_ENTERED"}, json={"values": [row]})
        return {"updated_range": data.get("updatedRange")}


class GoogleDocs(Google):
    service, base_url = "Google Docs", "https://docs.googleapis.com"

    def actions(self):
        return {**super().actions(), "upload_file": self.create_document}

    def create_document(self, c: Conn, p: dict) -> dict:
        _need(p, "name")
        doc = self.json(c, "POST", "/v1/documents", json={"title": p["name"]})
        if p.get("content"):
            self.json(c, "POST", f"/v1/documents/{doc['documentId']}:batchUpdate",
                      json={"requests": [{"insertText": {"location": {"index": 1}, "text": str(p["content"])}}]})
        return {"file_id": doc.get("documentId"), "name": p["name"], "link": f"https://docs.google.com/document/d/{doc.get('documentId')}/edit"}


# ------------------------------------------------------------------ Microsoft


class Microsoft(Adapter):
    service, base_url = "Microsoft Graph", "https://graph.microsoft.com"

    def test(self, c: Conn) -> dict:
        me = self.json(c, "GET", "/v1.0/me")
        return {"account": me.get("mail") or me.get("userPrincipalName")}

    def actions(self):
        return {**super().actions(), "send_email": self.send_email, "create_calendar_event": self.create_event, "upload_file": self.upload_file}

    def send_email(self, c: Conn, p: dict) -> dict:
        _need(p, "to", "subject")
        to = [p["to"]] if isinstance(p["to"], str) else list(p["to"])
        body = {"message": {
            "subject": p["subject"],
            "body": {"contentType": "HTML" if p.get("html") else "Text", "content": p.get("html") or p.get("body") or ""},
            "toRecipients": [{"emailAddress": {"address": a.strip()}} for a in ",".join(to).split(",") if a.strip()],
        }, "saveToSentItems": True}
        self.call(c, "POST", "/v1.0/me/sendMail", json=body)
        return {"accepted": True}

    def create_event(self, c: Conn, p: dict) -> dict:
        _need(p, "title", "start", "end")
        tz = p.get("timezone") or "UTC"
        body = {"subject": p["title"], "body": {"contentType": "Text", "content": p.get("description") or ""},
                "start": {"dateTime": p["start"], "timeZone": tz}, "end": {"dateTime": p["end"], "timeZone": tz},
                "location": {"displayName": p.get("location") or ""},
                "attendees": [{"emailAddress": {"address": e}, "type": "required"} for e in (p.get("attendees") or [])]}
        data = self.json(c, "POST", "/v1.0/me/events", json=body)
        return {"event_id": data.get("id"), "link": data.get("webLink")}

    def upload_file(self, c: Conn, p: dict) -> dict:
        _need(p, "name", "content")
        name = re.sub(r"[\\/:*?\"<>|]", "_", str(p["name"]))
        data = self.json(c, "PUT", f"/v1.0/me/drive/root:/{name}:/content", content=str(p["content"]).encode(),
                         headers={"Content-Type": p.get("content_type") or "text/plain"})
        return {"file_id": data.get("id"), "name": data.get("name"), "link": data.get("webUrl")}


# ------------------------------------------------------------------ messaging


class WhatsApp(Adapter):
    service, base_url = "WhatsApp Cloud API", "https://graph.facebook.com/v20.0"

    def test(self, c: Conn) -> dict:
        data = self.json(c, "GET", f"/{c.get('phone_number_id')}", params={"fields": "display_phone_number,verified_name"})
        return {"account": data.get("display_phone_number"), "name": data.get("verified_name")}

    def actions(self):
        return {**super().actions(), "send_message": self.send_message}

    def send_message(self, c: Conn, p: dict) -> dict:
        _need(p, "to", "text")
        body = {"messaging_product": "whatsapp", "to": re.sub(r"[^\d]", "", str(p["to"])), "type": "text", "text": {"body": str(p["text"])[:4096]}}
        data = self.json(c, "POST", f"/{c.get('phone_number_id')}/messages", json=body)
        return {"message_id": ((data.get("messages") or [{}])[0]).get("id")}


class Telegram(Adapter):
    service = "Telegram"

    def base(self, c: Conn) -> str:
        return f"https://api.telegram.org/bot{c.get('bot_token')}"

    def headers(self, c: Conn) -> dict:
        return {}

    def test(self, c: Conn) -> dict:
        data = self.json(c, "GET", "/getMe")
        return {"account": "@" + str((data.get("result") or {}).get("username"))}

    def actions(self):
        return {"send_message": self.send_message}

    def send_message(self, c: Conn, p: dict) -> dict:
        _need(p, "to", "text")
        body = {"chat_id": p["to"], "text": str(p["text"])[:4096]}
        if p.get("parse_mode"):
            body["parse_mode"] = p["parse_mode"]
        data = self.json(c, "POST", "/sendMessage", json=body)
        return {"message_id": (data.get("result") or {}).get("message_id")}


class Discord(Adapter):
    service = "Discord"

    def _url(self, c: Conn) -> str:
        url = str(c.get("webhook_url"))
        if not re.match(r"^https://(discord(app)?\.com)/api/webhooks/\d+/[\w-]+$", url):
            raise NodeError("Discord webhook URL is not valid", code="invalid_config")
        return url

    def test(self, c: Conn) -> dict:
        resp = http.request("GET", self._url(c), timeout=15)
        http.raise_for_status(resp, "Discord")
        return {"account": resp.json().get("name")}

    def actions(self):
        return {"send_message": self.send_message}

    def send_message(self, c: Conn, p: dict) -> dict:
        _need(p, "text")
        resp = http.request("POST", self._url(c), params={"wait": "true"}, json={"content": str(p["text"])[:2000]}, timeout=15)
        http.raise_for_status(resp, "Discord")
        return {"message_id": resp.json().get("id") if resp.content else None}


class Slack(Adapter):
    service, base_url = "Slack", "https://slack.com/api"

    def headers(self, c: Conn) -> dict:
        return {"Authorization": f"Bearer {c.get('bot_token')}"}

    def _ok(self, data: dict) -> dict:
        if not data.get("ok"):
            raise NodeError(f"Slack error: {data.get('error', 'unknown')}", code="slack_error", retryable=data.get("error") == "ratelimited")
        return data

    def test(self, c: Conn) -> dict:
        data = self._ok(self.json(c, "POST", "/auth.test"))
        return {"account": data.get("team"), "user": data.get("user")}

    def actions(self):
        return {"send_message": self.send_message}

    def send_message(self, c: Conn, p: dict) -> dict:
        _need(p, "to", "text")
        data = self._ok(self.json(c, "POST", "/chat.postMessage", json={"channel": p["to"], "text": str(p["text"])}))
        return {"message_id": data.get("ts"), "channel": data.get("channel")}


class Smtp(Adapter):
    service = "SMTP"

    def _connect(self, c: Conn):
        import smtplib

        host, port = str(c.get("host")), int(c.get("port") or 587)
        http.assert_url_allowed(f"https://{host}")  # same SSRF rules as HTTP
        try:
            smtp = smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)
            if port != 465:
                smtp.starttls()
            if c.get("username"):
                smtp.login(str(c.get("username")), str(c.get("password")))
            return smtp
        except smtplib.SMTPAuthenticationError as exc:
            raise NodeError("SMTP authentication failed", code="auth_failed") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise NodeError(f"SMTP connection failed: {type(exc).__name__}", code="smtp_error", retryable=True) from exc

    def test(self, c: Conn) -> dict:
        self._connect(c).quit()
        return {"account": c.get("from_address") or c.get("username")}

    def actions(self):
        return {"send_email": self.send_email}

    def send_email(self, c: Conn, p: dict) -> dict:
        _need(p, "to", "subject")
        import smtplib

        smtp = self._connect(c)
        try:
            smtp.send_message(_mime(p, sender=str(c.get("from_address") or c.get("username"))))
        except smtplib.SMTPException as exc:
            raise NodeError(f"SMTP delivery failed: {type(exc).__name__}", code="smtp_error", retryable=True) from exc
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass
        return {"accepted": True}


class Imap(Adapter):
    """Mailbox access for the Mailbox Trigger (app/integrations/imap.py); no node actions."""

    service = "IMAP"

    def test(self, c: Conn) -> dict:
        from app.integrations import imap

        return imap.test(c)

    def actions(self):
        return {}


# ------------------------------------------------------------------ data & CRM


class Notion(Adapter):
    service, base_url = "Notion", "https://api.notion.com"

    def headers(self, c: Conn) -> dict:
        return {"Authorization": f"Bearer {c.get('api_key')}", "Notion-Version": "2022-06-28"}

    def test(self, c: Conn) -> dict:
        return {"account": self.json(c, "GET", "/v1/users/me").get("name")}

    def actions(self):
        return {**super().actions(), "create_record": self.create, "update_record": self.update}

    def create(self, c: Conn, p: dict) -> dict:
        _need(p, "table", "data")
        data = self.json(c, "POST", "/v1/pages", json={"parent": {"database_id": p["table"]}, "properties": p["data"]})
        return {"record_id": data.get("id"), "link": data.get("url")}

    def update(self, c: Conn, p: dict) -> dict:
        _need(p, "record_id", "data")
        data = self.json(c, "PATCH", f"/v1/pages/{p['record_id']}", json={"properties": p["data"]})
        return {"record_id": data.get("id")}


class Airtable(Adapter):
    service, base_url = "Airtable", "https://api.airtable.com"

    def test(self, c: Conn) -> dict:
        return {"account": self.json(c, "GET", "/v0/meta/whoami").get("id")}

    def actions(self):
        return {**super().actions(), "create_record": self.create, "update_record": self.update}

    def _base(self, c: Conn, p: dict) -> str:
        base = p.get("database") or c.get("base_id")
        if not base:
            raise NodeError("An Airtable base id is required", code="invalid_config")
        return str(base)

    def create(self, c: Conn, p: dict) -> dict:
        _need(p, "table", "data")
        data = self.json(c, "POST", f"/v0/{self._base(c, p)}/{p['table']}", json={"fields": p["data"], "typecast": True})
        return {"record_id": data.get("id"), "fields": data.get("fields")}

    def update(self, c: Conn, p: dict) -> dict:
        _need(p, "table", "record_id", "data")
        data = self.json(c, "PATCH", f"/v0/{self._base(c, p)}/{p['table']}/{p['record_id']}", json={"fields": p["data"], "typecast": True})
        return {"record_id": data.get("id"), "fields": data.get("fields")}


class Supabase(Adapter):
    service = "Supabase"

    def base(self, c: Conn) -> str:
        url = str(c.get("project_url")).rstrip("/")
        if not url.startswith("https://"):
            raise NodeError("Supabase project URL must start with https://", code="invalid_config")
        return url

    def headers(self, c: Conn) -> dict:
        return {"apikey": str(c.get("api_key")), "Authorization": f"Bearer {c.get('api_key')}"}

    def test(self, c: Conn) -> dict:
        self.call(c, "GET", "/rest/v1/")
        return {"account": self.base(c)}

    def actions(self):
        return {**super().actions(), "create_record": self.create, "update_record": self.update}

    def create(self, c: Conn, p: dict) -> dict:
        _need(p, "table", "data")
        rows = self.json(c, "POST", f"/rest/v1/{p['table']}", json=p["data"], headers={"Prefer": "return=representation"})
        return {"record": rows[0] if isinstance(rows, list) and rows else rows}

    def update(self, c: Conn, p: dict) -> dict:
        _need(p, "table", "record_id", "data")
        key = p.get("key_column") or "id"
        rows = self.json(c, "PATCH", f"/rest/v1/{p['table']}", params={key: f"eq.{p['record_id']}"}, json=p["data"],
                         headers={"Prefer": "return=representation"})
        return {"updated": len(rows) if isinstance(rows, list) else 0, "record": rows[0] if isinstance(rows, list) and rows else None}


class PostgreSQL(Adapter):
    """Direct PostgreSQL access. Only parameterised INSERT / UPDATE on a named
    table are exposed; identifiers are quoted by psycopg, values are bound."""

    service = "PostgreSQL"

    def _connect(self, c: Conn):
        import psycopg

        host = str(c.get("host"))
        http.assert_url_allowed(f"https://{host}")
        try:
            return psycopg.connect(host=host, port=int(c.get("port") or 5432), dbname=str(c.get("database")),
                                   user=str(c.get("username")), password=str(c.get("password")),
                                   sslmode=str(c.get("sslmode") or "require"), connect_timeout=10)
        except psycopg.OperationalError as exc:
            raise NodeError("Could not connect to PostgreSQL", code="connection_failed", retryable=True) from exc

    def test(self, c: Conn) -> dict:
        with self._connect(c) as conn:
            return {"account": conn.execute("select current_database()").fetchone()[0]}

    def actions(self):
        return {"create_record": self.create, "update_record": self.update}

    @staticmethod
    def _table(name: str):
        from psycopg import sql

        return sql.Identifier(*str(name).split(".")[:2])

    def create(self, c: Conn, p: dict) -> dict:
        _need(p, "table", "data")
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row

        cols = list(p["data"].keys())
        q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            self._table(p["table"]), sql.SQL(", ").join(map(sql.Identifier, cols)), sql.SQL(", ").join(sql.Placeholder() * len(cols)))
        try:
            with self._connect(c) as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(q, [p["data"][k] for k in cols])
                return {"record": {k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v)) for k, v in (cur.fetchone() or {}).items()}}
        except psycopg.Error as exc:
            raise NodeError(f"PostgreSQL error: {type(exc).__name__}", code="database_error") from exc

    def update(self, c: Conn, p: dict) -> dict:
        _need(p, "table", "record_id", "data")
        import psycopg
        from psycopg import sql

        cols = list(p["data"].keys())
        q = sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(
            self._table(p["table"]),
            sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in cols),
            sql.Identifier(str(p.get("key_column") or "id")))
        try:
            with self._connect(c) as conn:
                cur = conn.execute(q, [p["data"][k] for k in cols] + [p["record_id"]])
                return {"updated": cur.rowcount}
        except psycopg.Error as exc:
            raise NodeError(f"PostgreSQL error: {type(exc).__name__}", code="database_error") from exc


class HubSpot(Adapter):
    service, base_url = "HubSpot", "https://api.hubapi.com"

    def test(self, c: Conn) -> dict:
        return {"account": str(self.json(c, "GET", "/account-info/v3/details").get("portalId"))}

    def actions(self):
        return {**super().actions(), "create_lead": self.create_lead, "update_record": self.update}

    def create_lead(self, c: Conn, p: dict) -> dict:
        props = {k: v for k, v in {"email": p.get("email"), "firstname": p.get("first_name"), "lastname": p.get("last_name"),
                                   "phone": p.get("phone"), "company": p.get("company")}.items() if v}
        props.update(p.get("properties") or {})
        if not props:
            raise NodeError("At least one lead field is required", code="invalid_config")
        data = self.json(c, "POST", "/crm/v3/objects/contacts", json={"properties": props})
        return {"record_id": data.get("id")}

    def update(self, c: Conn, p: dict) -> dict:
        _need(p, "record_id", "data")
        obj = p.get("object_type") or "contacts"
        data = self.json(c, "PATCH", f"/crm/v3/objects/{obj}/{p['record_id']}", json={"properties": p["data"]})
        return {"record_id": data.get("id")}


class Pipedrive(Adapter):
    service = "Pipedrive"

    def base(self, c: Conn) -> str:
        domain = str(c.get("company_domain"))
        if not re.match(r"^[a-z0-9-]+$", domain):
            raise NodeError("Pipedrive company domain is not valid", code="invalid_config")
        return f"https://{domain}.pipedrive.com/api"

    def headers(self, c: Conn) -> dict:
        return {"x-api-token": str(c.get("api_key"))}

    def test(self, c: Conn) -> dict:
        return {"account": ((self.json(c, "GET", "/v1/users/me").get("data")) or {}).get("email")}

    def actions(self):
        return {**super().actions(), "create_lead": self.create_lead, "update_record": self.update}

    def create_lead(self, c: Conn, p: dict) -> dict:
        name = " ".join(x for x in [p.get("first_name"), p.get("last_name")] if x) or p.get("email") or "New lead"
        person_body = {"name": name}
        if p.get("email"):
            person_body["email"] = [{"value": p["email"], "primary": True}]
        if p.get("phone"):
            person_body["phone"] = [{"value": p["phone"], "primary": True}]
        person = (self.json(c, "POST", "/v1/persons", json=person_body).get("data")) or {}
        lead = (self.json(c, "POST", "/v1/leads", json={"title": p.get("title") or f"Lead: {name}", "person_id": person.get("id")}).get("data")) or {}
        return {"record_id": lead.get("id"), "person_id": person.get("id")}

    def update(self, c: Conn, p: dict) -> dict:
        _need(p, "record_id", "data")
        obj = p.get("object_type") or "persons"
        data = (self.json(c, "PUT", f"/v1/{obj}/{p['record_id']}", json=p["data"]).get("data")) or {}
        return {"record_id": data.get("id")}


class Salesforce(Adapter):
    service = "Salesforce"
    api = "/services/data/v60.0"

    def base(self, c: Conn) -> str:
        url = str(c.get("instance_url")).rstrip("/")
        if not re.match(r"^https://[a-zA-Z0-9.-]+\.(salesforce|force)\.com$", url):
            raise NodeError("Salesforce instance URL is not valid", code="invalid_config")
        return url

    def test(self, c: Conn) -> dict:
        return {"account": self.json(c, "GET", "/services/oauth2/userinfo").get("email")}

    def actions(self):
        return {**super().actions(), "create_lead": self.create_lead, "update_record": self.update}

    def create_lead(self, c: Conn, p: dict) -> dict:
        body = {k: v for k, v in {"Email": p.get("email"), "FirstName": p.get("first_name"), "LastName": p.get("last_name") or "Unknown",
                                  "Phone": p.get("phone"), "Company": p.get("company") or "Unknown"}.items() if v}
        body.update(p.get("properties") or {})
        data = self.json(c, "POST", f"{self.api}/sobjects/Lead", json=body)
        return {"record_id": data.get("id")}

    def update(self, c: Conn, p: dict) -> dict:
        _need(p, "record_id", "data")
        obj = re.sub(r"[^A-Za-z0-9_]", "", str(p.get("object_type") or "Lead"))
        self.call(c, "PATCH", f"{self.api}/sobjects/{obj}/{p['record_id']}", json=p["data"])
        return {"record_id": p["record_id"]}


# ------------------------------------------------------------------ commerce & marketing


class Shopify(Adapter):
    service = "Shopify"

    def base(self, c: Conn) -> str:
        shop = str(c.get("shop_domain")).lower().strip()
        if not re.match(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$", shop):
            raise NodeError("Shop domain must look like 'your-store.myshopify.com'", code="invalid_config")
        return f"https://{shop}/admin/api/2024-10"

    def headers(self, c: Conn) -> dict:
        return {"X-Shopify-Access-Token": str(c.get("api_key"))}

    def test(self, c: Conn) -> dict:
        return {"account": (self.json(c, "GET", "/shop.json").get("shop") or {}).get("name")}


class WooCommerce(Adapter):
    service = "WooCommerce"

    def base(self, c: Conn) -> str:
        url = str(c.get("store_url")).rstrip("/")
        if not url.startswith("https://"):
            raise NodeError("Store URL must start with https://", code="invalid_config")
        return url + "/wp-json/wc/v3"

    def headers(self, c: Conn) -> dict:
        raw = f"{c.get('consumer_key')}:{c.get('consumer_secret')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def test(self, c: Conn) -> dict:
        self.json(c, "GET", "/orders", params={"per_page": 1})
        return {"account": str(c.get("store_url"))}


class Stripe(Adapter):
    service, base_url = "Stripe", "https://api.stripe.com"

    def test(self, c: Conn) -> dict:
        acct = self.json(c, "GET", "/v1/account")
        return {"account": acct.get("id"), "livemode": not str(c.get("api_key")).startswith(("sk_test_", "rk_test_"))}

    def api_request(self, c: Conn, p: dict) -> dict:
        # Stripe takes form-encoded bodies.
        resp = self.call(c, str(p.get("method") or "GET"), str(p.get("path") or "/"), params=p.get("query") or None,
                         data=p.get("body") if isinstance(p.get("body"), dict) else None)
        return {"status": resp.status_code, "body": resp.json() if resp.content else None}


class Meta(Adapter):
    service, base_url = "Meta Graph API", "https://graph.facebook.com/v20.0"

    def test(self, c: Conn) -> dict:
        me = self.json(c, "GET", "/me", params={"fields": "id,name"})
        return {"account": me.get("name") or me.get("id")}


class LinkedIn(Adapter):
    service, base_url = "LinkedIn", "https://api.linkedin.com"

    def test(self, c: Conn) -> dict:
        me = self.json(c, "GET", "/v2/userinfo")
        return {"account": me.get("email") or me.get("name")}


class Mailchimp(Adapter):
    service = "Mailchimp"

    def base(self, c: Conn) -> str:
        key = str(c.get("api_key"))
        dc = key.rsplit("-", 1)[-1] if "-" in key else ""
        if not re.match(r"^[a-z]{2}\d{1,3}$", dc):
            raise NodeError("Mailchimp API key must end with a data-center suffix such as '-us21'", code="invalid_config")
        return f"https://{dc}.api.mailchimp.com/3.0"

    def headers(self, c: Conn) -> dict:
        return {"Authorization": "Basic " + base64.b64encode(f"wae:{c.get('api_key')}".encode()).decode()}

    def test(self, c: Conn) -> dict:
        self.json(c, "GET", "/ping")
        return {"account": "mailchimp"}

    def actions(self):
        return {**super().actions(), "create_lead": self.add_subscriber}

    def add_subscriber(self, c: Conn, p: dict) -> dict:
        _need(p, "email")
        list_id = p.get("list_id") or c.get("list_id")
        if not list_id:
            raise NodeError("A Mailchimp audience (list) id is required", code="invalid_config")
        body = {"email_address": p["email"], "status": "subscribed",
                "merge_fields": {k: v for k, v in {"FNAME": p.get("first_name"), "LNAME": p.get("last_name")}.items() if v}}
        data = self.json(c, "POST", f"/lists/{list_id}/members", json=body)
        return {"record_id": data.get("id")}


# ------------------------------------------------------------------ generic & AI


class RestApi(Adapter):
    service = "REST API"

    def base(self, c: Conn) -> str:
        return str(c.get("base_url")).rstrip("/")

    def headers(self, c: Conn) -> dict:
        key = c.get("api_key")
        if not key:
            return {}
        name = c.get("auth_header") or "Authorization"
        prefix = c.get("auth_prefix")
        prefix = "Bearer" if prefix in (None, "") and name.lower() == "authorization" else (prefix or "")
        return {name: f"{prefix} {key}".strip()}

    def test(self, c: Conn) -> dict:
        path = str(c.get("test_path") or "/")
        self.call(c, "GET", path if path.startswith("/") else "/" + path)
        return {"account": self.base(c)}


class CustomWebhook(Adapter):
    service = "Webhook"

    def test(self, c: Conn) -> dict:
        http.assert_url_allowed(str(c.get("webhook_url")))
        return {"account": "URL validated (no request is sent until a workflow uses it)"}

    def actions(self):
        return {"send_message": self.post, "api_request": self.post}

    def post(self, c: Conn, p: dict) -> dict:
        body = p.get("body") if p.get("body") not in (None, "") else {"text": p.get("text")}
        resp = http.request("POST", str(c.get("webhook_url")), json=body, timeout=20)
        http.raise_for_status(resp, "Webhook")
        return {"status": resp.status_code}


class McpServer(Adapter):
    service = "MCP server"

    def test(self, c: Conn) -> dict:
        from app.ai.mcp import McpClient

        tools = McpClient(str(c.get("url")), c.get("api_key") or None).list_tools()
        return {"account": str(c.get("url")), "tools": [t["name"] for t in tools][:50]}

    def actions(self):
        return {}


class AnthropicAI(Adapter):
    service = "Anthropic"

    def test(self, c: Conn) -> dict:
        from app.ai.providers import get_provider

        return get_provider("anthropic").test(c.secret, c.config)

    def actions(self):
        return {}


class OpenAIAdapter(Adapter):
    service = "OpenAI"
    provider = "openai"

    def test(self, c: Conn) -> dict:
        from app.ai.providers import get_provider

        return get_provider(self.provider).test(c.secret, c.config)

    def actions(self):
        return {}


class OpenAICompatible(OpenAIAdapter):
    service, provider = "OpenAI-compatible server", "openai_compatible"


class GeminiAI(OpenAIAdapter):
    service, provider = "Google Gemini", "gemini"


# ------------------------------------------------------------------ catalog

_GOOGLE = dict(authorize_url="https://accounts.google.com/o/oauth2/v2/auth", token_url="https://oauth2.googleapis.com/token",
               extra_params={"access_type": "offline", "prompt": "consent"}, revoke_url="https://oauth2.googleapis.com/revoke")
_MS = dict(authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
           token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token")


def _google(*scopes: str) -> OAuthDef:
    return OAuthDef(group="google", scopes=["openid", "email", *scopes], **_GOOGLE)


@dataclass
class ProviderDef:
    key: str
    name: str
    category: str
    auth_type: str  # oauth2 | api_key | bot_token | webhook_url | basic | connection
    adapter: Adapter
    fields: list[Field] = field(default_factory=list)
    oauth: OAuthDef | None = None
    setup: str = ""
    docs_url: str = ""

    def describe(self) -> dict:
        return {
            "key": self.key, "name": self.name, "category": self.category, "auth_type": self.auth_type,
            "fields": [f.__dict__ for f in self.fields],
            "scopes": self.oauth.scopes if self.oauth else [],
            "oauth_group": self.oauth.group if self.oauth else None,
            "actions": sorted(self.adapter.actions().keys()),
            "setup": self.setup, "docs_url": self.docs_url,
        }


def _key(label: str = "API key", help: str = "") -> Field:
    return Field("api_key", label, secret=True, help=help)


_OAUTH_SETUP = ("Requires a platform OAuth app: set WAE_OAUTH_{G}_CLIENT_ID and WAE_OAUTH_{G}_CLIENT_SECRET and register the "
                "redirect URI <WAE_PUBLIC_BASE_URL>/api/v1/integrations/oauth/callback with the provider.")

PROVIDERS: dict[str, ProviderDef] = {p.key: p for p in [
    # Communication
    ProviderDef("gmail", "Gmail", "Communication", "oauth2", Gmail(),
                oauth=_google("https://www.googleapis.com/auth/gmail.send"), setup=_OAUTH_SETUP.format(G="GOOGLE")),
    ProviderDef("outlook", "Microsoft Outlook", "Communication", "oauth2", Microsoft(),
                oauth=OAuthDef(group="microsoft", scopes=["offline_access", "User.Read", "Mail.Send", "Calendars.ReadWrite"], **_MS),
                setup=_OAUTH_SETUP.format(G="MICROSOFT")),
    ProviderDef("whatsapp", "WhatsApp Business Cloud API", "Communication", "api_key", WhatsApp(), fields=[
        Field("access_token", "Access token", secret=True, help="System-user permanent token from Meta Business."),
        Field("phone_number_id", "Phone number ID"),
        Field("app_secret", "App secret", secret=True, required=False, help="Used to verify inbound webhook signatures."),
    ], setup="Create a Meta app with the WhatsApp product, add a phone number and generate a system-user token."),
    ProviderDef("telegram", "Telegram", "Communication", "bot_token", Telegram(),
                fields=[Field("bot_token", "Bot token", secret=True, help="From @BotFather.")]),
    ProviderDef("discord", "Discord", "Communication", "webhook_url", Discord(),
                fields=[Field("webhook_url", "Webhook URL", secret=True, help="Channel settings > Integrations > Webhooks.")]),
    ProviderDef("slack", "Slack", "Communication", "bot_token", Slack(),
                fields=[Field("bot_token", "Bot token (xoxb-...)", secret=True, help="Slack app with the chat:write scope.")]),
    ProviderDef("smtp", "SMTP Email", "Communication", "basic", Smtp(), fields=[
        Field("host", "SMTP host"), Field("port", "Port", placeholder="587"), Field("username", "Username", required=False),
        Field("password", "Password", secret=True, required=False), Field("from_address", "From address"),
    ]),
    ProviderDef("imap", "IMAP Mailbox", "Communication", "basic", Imap(), fields=[
        Field("host", "IMAP host", placeholder="imap.example.com"),
        Field("port", "Port", required=False, placeholder="993"),
        Field("security", "Encryption", required=False, placeholder="ssl", help="ssl (port 993, default) or starttls (port 143). Unencrypted connections are refused."),
        Field("username", "Username"),
        Field("password", "Password", secret=True, help="For Gmail and Outlook use an app password."),
    ], setup="Used by the Mailbox Trigger to start workflows from new email. Gmail: enable IMAP and create an app password. "
             "Microsoft 365: IMAP with basic authentication may be disabled by your tenant."),
    # Productivity
    ProviderDef("google_calendar", "Google Calendar", "Productivity", "oauth2", GoogleCalendar(),
                oauth=_google("https://www.googleapis.com/auth/calendar.events"), setup=_OAUTH_SETUP.format(G="GOOGLE")),
    ProviderDef("google_drive", "Google Drive", "Productivity", "oauth2", GoogleDrive(),
                oauth=_google("https://www.googleapis.com/auth/drive.file"), setup=_OAUTH_SETUP.format(G="GOOGLE")),
    ProviderDef("google_sheets", "Google Sheets", "Productivity", "oauth2", GoogleSheets(),
                fields=[Field("spreadsheet_id", "Default spreadsheet ID", required=False)],
                oauth=_google("https://www.googleapis.com/auth/spreadsheets"), setup=_OAUTH_SETUP.format(G="GOOGLE")),
    ProviderDef("google_docs", "Google Docs", "Productivity", "oauth2", GoogleDocs(),
                oauth=_google("https://www.googleapis.com/auth/documents"), setup=_OAUTH_SETUP.format(G="GOOGLE")),
    ProviderDef("microsoft_365", "Microsoft 365", "Productivity", "oauth2", Microsoft(),
                oauth=OAuthDef(group="microsoft", scopes=["offline_access", "User.Read", "Mail.Send", "Calendars.ReadWrite", "Files.ReadWrite"], **_MS),
                setup=_OAUTH_SETUP.format(G="MICROSOFT")),
    ProviderDef("notion", "Notion", "Productivity", "api_key", Notion(),
                fields=[_key("Internal integration secret", "Share the target databases with the integration.")]),
    ProviderDef("airtable", "Airtable", "Productivity", "api_key", Airtable(),
                fields=[_key("Personal access token"), Field("base_id", "Default base ID", required=False)]),
    # E-commerce
    ProviderDef("shopify", "Shopify", "E-commerce", "api_key", Shopify(),
                fields=[Field("shop_domain", "Shop domain", placeholder="your-store.myshopify.com"), _key("Admin API access token")]),
    ProviderDef("woocommerce", "WooCommerce", "E-commerce", "basic", WooCommerce(), fields=[
        Field("store_url", "Store URL", placeholder="https://shop.example.com"),
        Field("consumer_key", "Consumer key", secret=True), Field("consumer_secret", "Consumer secret", secret=True)]),
    ProviderDef("stripe", "Stripe", "E-commerce", "api_key", Stripe(),
                fields=[_key("Secret or restricted key", "Prefer a restricted key limited to what the automations need.")]),
    # CRM
    ProviderDef("hubspot", "HubSpot", "CRM", "api_key", HubSpot(), fields=[_key("Private app access token")]),
    ProviderDef("pipedrive", "Pipedrive", "CRM", "api_key", Pipedrive(),
                fields=[Field("company_domain", "Company domain", placeholder="acme (from acme.pipedrive.com)"), _key("API token")]),
    ProviderDef("salesforce", "Salesforce", "CRM", "oauth2", Salesforce(),
                oauth=OAuthDef(group="salesforce", authorize_url="https://login.salesforce.com/services/oauth2/authorize",
                               token_url="https://login.salesforce.com/services/oauth2/token", scopes=["api", "refresh_token", "openid", "email"]),
                setup=_OAUTH_SETUP.format(G="SALESFORCE")),
    # Marketing
    ProviderDef("meta", "Meta (Facebook / Instagram)", "Marketing", "api_key", Meta(),
                fields=[Field("access_token", "Page or system-user access token", secret=True)],
                setup="Available Graph API endpoints depend on the permissions granted to your Meta app."),
    ProviderDef("linkedin", "LinkedIn", "Marketing", "oauth2", LinkedIn(),
                oauth=OAuthDef(group="linkedin", authorize_url="https://www.linkedin.com/oauth/v2/authorization",
                               token_url="https://www.linkedin.com/oauth/v2/accessToken", scopes=["openid", "profile", "email", "w_member_social"], pkce=False),
                setup=_OAUTH_SETUP.format(G="LINKEDIN") + " Posting APIs require LinkedIn product approval."),
    ProviderDef("mailchimp", "Mailchimp", "Marketing", "api_key", Mailchimp(),
                fields=[_key("API key"), Field("list_id", "Default audience ID", required=False)]),
    # Infrastructure
    ProviderDef("supabase", "Supabase", "Infrastructure", "api_key", Supabase(),
                fields=[Field("project_url", "Project URL", placeholder="https://xyz.supabase.co"), _key("Service role or anon key")]),
    ProviderDef("postgresql", "PostgreSQL", "Infrastructure", "connection", PostgreSQL(), fields=[
        Field("host", "Host"), Field("port", "Port", placeholder="5432", required=False), Field("database", "Database"),
        Field("username", "User"), Field("password", "Password", secret=True),
        Field("sslmode", "SSL mode", required=False, placeholder="require")],
        setup="Use a dedicated database role limited to the tables the automations need."),
    ProviderDef("rest_api", "REST API", "Infrastructure", "api_key", RestApi(), fields=[
        Field("base_url", "Base URL", placeholder="https://api.example.com"),
        Field("api_key", "API key / token", secret=True, required=False),
        Field("auth_header", "Auth header name", required=False, placeholder="Authorization"),
        Field("auth_prefix", "Auth prefix", required=False, placeholder="Bearer"),
        Field("test_path", "Health-check path", required=False, placeholder="/")]),
    ProviderDef("custom_webhook", "Custom Webhook", "Infrastructure", "webhook_url", CustomWebhook(),
                fields=[Field("webhook_url", "Webhook URL", secret=True)]),
    ProviderDef("mcp_server", "MCP Server", "Infrastructure", "api_key", McpServer(), fields=[
        Field("url", "Server URL (Streamable HTTP)", placeholder="https://mcp.example.com/mcp"),
        Field("api_key", "Bearer token", secret=True, required=False)],
        setup="Agents can only call tools from this server that are explicitly allowlisted on the agent."),
    # AI providers
    ProviderDef("anthropic", "Anthropic Claude", "AI", "api_key", AnthropicAI(), fields=[_key()]),
    ProviderDef("openai", "OpenAI", "AI", "api_key", OpenAIAdapter(), fields=[_key()]),
    ProviderDef("gemini", "Google Gemini", "AI", "api_key", GeminiAI(), fields=[_key()]),
    ProviderDef("openai_compatible", "OpenAI-compatible / local model", "AI", "api_key", OpenAICompatible(), fields=[
        Field("base_url", "Base URL", placeholder="https://my-inference-host/v1"),
        Field("api_key", "API key", secret=True, required=False)],
        setup="Works with any server implementing /v1/chat/completions (vLLM, Ollama, LM Studio, gateways). Servers on a "
              "private network additionally require WAE_ALLOW_PRIVATE_NETWORK=true."),
]}

AI_PROVIDER_KEYS = ("anthropic", "openai", "gemini", "openai_compatible")
