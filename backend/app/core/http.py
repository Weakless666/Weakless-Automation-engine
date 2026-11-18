"""Outbound HTTP with SSRF protection.

Every outbound request made on behalf of a workflow, agent tool, integration
or notification goes through ``request``. User-influenced URLs are resolved
and rejected when they point at loopback, private, link-local or otherwise
internal addresses. Redirects are followed manually so each hop is validated.

Known limitation: the DNS answer is validated and httpx then resolves again
(a DNS-rebinding window). Deploy with egress filtering for defence in depth.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import get_settings
from app.core.errors import NodeError

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
TRANSPORT: httpx.BaseTransport | None = None  # tests inject httpx.MockTransport here


def resolve_host(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise NodeError(f"Could not resolve host '{host}'", code="dns_error", retryable=True) from exc
    return list({i[4][0] for i in infos})


def _is_internal(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    internal = (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified
    )
    if isinstance(addr, ipaddress.IPv6Address):
        internal = internal or addr.is_site_local
    return internal or not addr.is_global


def assert_url_allowed(url: str, allow_private: bool | None = None) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise NodeError(f"URL scheme '{parts.scheme}' is not allowed", code="ssrf_blocked")
    host = parts.hostname
    if not host:
        raise NodeError("URL has no host", code="ssrf_blocked")
    if parts.username or parts.password:
        raise NodeError("Credentials in URLs are not allowed", code="ssrf_blocked")
    if allow_private is None:
        allow_private = get_settings().allow_private_network
    if allow_private:
        return
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        if host.lower() in ("localhost",) or host.lower().endswith((".localhost", ".local", ".internal")):
            raise NodeError(f"Requests to '{host}' are blocked", code="ssrf_blocked")
        ips = resolve_host(host)
    for ip in ips:
        if _is_internal(ip):
            raise NodeError(f"Requests to internal address '{host}' are blocked", code="ssrf_blocked")


def request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | list[tuple[str, str]] | None = None,  # a list allows repeated keys (PostgREST filters)
    json: object | None = None,
    data: object | None = None,
    content: bytes | None = None,
    files: object | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float = 30.0,
    allow_private: bool | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,  # raise only for file downloads the caller stores (media)
) -> httpx.Response:
    """Perform a guarded HTTP request. Raises ``NodeError`` for network failures."""
    method = method.upper()
    try:
        with httpx.Client(transport=TRANSPORT, timeout=timeout, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                assert_url_allowed(url, allow_private)
                req = client.build_request(method, url, headers=headers, params=params, json=json, data=data, content=content, files=files)
                if auth:
                    import base64

                    req.headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
                resp = client.send(req, stream=True)
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    nxt = urljoin(url, resp.headers["location"])
                    resp.close()
                    if urlsplit(nxt).hostname != urlsplit(url).hostname and headers:
                        headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "cookie", "x-api-key")}
                    if resp.status_code == 303:
                        method, json, data, content, files = "GET", None, None, None, None
                    url, params = nxt, None
                    continue
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        resp.close()
                        raise NodeError(f"Response exceeded the {max_bytes // (1024 * 1024)} MB limit", code="response_too_large")
                resp.close()
                resp._content = body
                return resp
            raise NodeError("Too many redirects", code="too_many_redirects")
    except httpx.TimeoutException as exc:
        raise NodeError(f"Request to {urlsplit(url).hostname} timed out", code="timeout", retryable=True) from exc
    except httpx.TransportError as exc:
        raise NodeError(f"Network error calling {urlsplit(url).hostname}: {type(exc).__name__}", code="network_error", retryable=True) from exc


def raise_for_status(resp: httpx.Response, service: str) -> None:
    """Translate an HTTP error into a NodeError with a bounded, secret-free message."""
    if resp.status_code < 400:
        return
    retryable = resp.status_code in (408, 425, 429) or resp.status_code >= 500
    try:
        detail = resp.json()
    except ValueError:
        detail = resp.text[:500]
    from app.core.redact import redact

    raise NodeError(
        f"{service} returned HTTP {resp.status_code}",
        code=f"http_{resp.status_code}",
        retryable=retryable,
        details=redact(detail),
    )
