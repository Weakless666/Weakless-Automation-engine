"""IMAP mailbox access for the Mailbox Trigger.

* Connections are always encrypted: implicit TLS (``security=ssl``, default, port 993) or
  STARTTLS (``security=starttls``, port 143). Certificates are verified. A server that offers
  neither is refused, so a password is never sent in clear text.
* The host passes the same SSRF rules as outbound HTTP (no private or loopback addresses).
* Messages are read with ``BODY.PEEK[]`` (the read flag only changes when the trigger is
  configured to mark messages as read) and tracked by UID together with the folder's UIDVALIDITY,
  as RFC 3501 requires: if UIDVALIDITY changes, previously seen UIDs mean nothing and the
  watcher starts again from the newest message instead of replaying the folder.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import re
import ssl
from dataclasses import dataclass
from email.utils import getaddresses, parsedate_to_datetime

from app.core import http
from app.core.errors import NodeError

MAX_TEXT = 50_000
MAX_HTML = 100_000
MAX_MESSAGE_BYTES = 25 * 1024 * 1024


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()  # verifies the certificate and host name


@dataclass
class FolderState:
    uidvalidity: int
    uids: list[int]  # new UIDs in ascending order (all of them, the caller limits)
    highest: int  # highest UID present in the folder (0 when empty)


def connect(c) -> imaplib.IMAP4:
    host = str(c.get("host") or "").strip()
    if not host:
        raise NodeError("IMAP host is required", code="invalid_config")
    security = str(c.get("security") or "ssl").strip().lower()
    if security not in ("ssl", "starttls"):
        raise NodeError("Encryption must be 'ssl' or 'starttls'", code="invalid_config")
    port = int(c.get("port") or (993 if security == "ssl" else 143))
    http.assert_url_allowed(f"https://{host}")
    try:
        if security == "ssl":
            client = imaplib.IMAP4_SSL(host, port, ssl_context=_ssl_context(), timeout=20)
        else:
            client = imaplib.IMAP4(host, port, timeout=20)
            if "STARTTLS" not in client.capabilities:
                client.shutdown()
                raise NodeError("The IMAP server does not offer STARTTLS; refusing to send the password unencrypted", code="insecure_server")
            client.starttls(ssl_context=_ssl_context())
    except NodeError:
        raise
    except ssl.SSLError as exc:
        raise NodeError(f"IMAP TLS handshake failed: {exc.reason or type(exc).__name__}", code="tls_error") from exc
    except (OSError, imaplib.IMAP4.error) as exc:
        raise NodeError(f"IMAP connection failed: {type(exc).__name__}", code="imap_error", retryable=True) from exc
    try:
        client.login(str(c.get("username")), str(c.get("password")))
    except imaplib.IMAP4.error as exc:
        _close(client)
        raise NodeError("IMAP authentication failed", code="auth_failed") from exc
    return client


def _close(client: imaplib.IMAP4) -> None:
    for step in (client.logout, client.shutdown):
        try:
            step()
            return
        except Exception:  # noqa: BLE001 - best effort
            continue


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ok(typ: str, data, what: str):
    if typ != "OK":
        detail = data[0].decode(errors="replace")[:200] if data and isinstance(data[0], bytes) else ""
        raise NodeError(f"IMAP {what} failed{': ' + detail if detail else ''}", code="imap_error", retryable=True)
    return data


def open_folder(client: imaplib.IMAP4, folder: str, *, writable: bool) -> int:
    typ, data = client.select(_quote(folder or "INBOX"), readonly=not writable)
    if typ != "OK":
        raise NodeError(f"Folder '{folder}' could not be opened", code="folder_not_found")
    _, values = client.response("UIDVALIDITY")
    try:
        return int(values[0])
    except (TypeError, ValueError, IndexError):
        raise NodeError("The IMAP server did not report UIDVALIDITY", code="imap_error") from None


def folder_state(client: imaplib.IMAP4, folder: str, after_uid: int | None, *, writable: bool) -> FolderState:
    uidvalidity = open_folder(client, folder, writable=writable)
    data = _ok(*client.uid("SEARCH", None, "ALL"), "search")
    uids = sorted(int(u) for u in (data[0] or b"").split())
    highest = uids[-1] if uids else 0
    new = [u for u in uids if after_uid is not None and u > after_uid]
    return FolderState(uidvalidity=uidvalidity, uids=new, highest=highest)


def fetch(client: imaplib.IMAP4, uid: int) -> bytes:
    data = _ok(*client.uid("FETCH", str(uid), "(BODY.PEEK[])"), "fetch")
    for part in data:
        if isinstance(part, tuple) and len(part) == 2 and isinstance(part[1], bytes):
            return part[1][:MAX_MESSAGE_BYTES]
    raise NodeError(f"Message {uid} could not be read", code="imap_error", retryable=True)


def mark_seen(client: imaplib.IMAP4, uid: int) -> None:
    _ok(*client.uid("STORE", str(uid), "+FLAGS", r"(\Seen)"), "flag update")


def _addresses(values: list[str]) -> list[dict]:
    return [{"name": n, "email": a} for n, a in getaddresses(values) if a]


def parse(raw: bytes) -> dict:
    """The trigger payload for one message: headers, text and HTML bodies, attachment metadata."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    def body(kind: str, limit: int) -> str:
        part = msg.get_body(preferencelist=(kind,))
        if part is None:
            return ""
        try:
            return str(part.get_content())[:limit]
        except (LookupError, ValueError):  # unknown charset or broken encoding
            payload = part.get_payload(decode=True) or b""
            return payload.decode("utf-8", errors="replace")[:limit]

    sender = _addresses([str(msg.get("From", ""))])
    try:
        date = parsedate_to_datetime(str(msg["Date"])).isoformat() if msg["Date"] else None
    except (TypeError, ValueError):
        date = None
    attachments = []
    for part in msg.iter_attachments():
        content = part.get_payload(decode=True) or b""
        attachments.append({"filename": part.get_filename() or "", "content_type": part.get_content_type(), "size": len(content)})
    text = body("plain", MAX_TEXT)
    html = body("html", MAX_HTML)
    if not text and html:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:MAX_TEXT]
    return {
        "message_id": str(msg.get("Message-ID", "")).strip(),
        "subject": str(msg.get("Subject", "")),
        "from": sender[0] if sender else {"name": "", "email": ""},
        "to": _addresses(msg.get_all("To", [])),
        "cc": _addresses(msg.get_all("Cc", [])),
        "reply_to": _addresses(msg.get_all("Reply-To", [])),
        "date": date,
        "text": text,
        "html": html,
        "attachments": attachments,
    }


def test(c) -> dict:
    client = connect(c)
    try:
        open_folder(client, "INBOX", writable=False)
        return {"account": c.get("username")}
    finally:
        _close(client)
