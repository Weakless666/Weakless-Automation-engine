"""Minimal MCP (Model Context Protocol) client over Streamable HTTP.

Implements what agents need: ``initialize``, ``tools/list`` and ``tools/call``.
The client is never handed to a model directly - agents can only reach an MCP
tool that an engineer explicitly allowlisted on the agent (see ``ai/tools.py``).
"""
from __future__ import annotations

import json
from typing import Any

from app.core import http
from app.core.errors import NodeError

PROTOCOL_VERSION = "2025-06-18"


class McpClient:
    def __init__(self, url: str, token: str | None = None, timeout: float = 30.0):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self._id = 0
        self._initialized = False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self._initialized:
            h["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return h

    @staticmethod
    def _parse(resp) -> dict | None:
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            last = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                        last = msg
            return last
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise NodeError("MCP server returned a non-JSON response", code="mcp_error")

    def _rpc(self, method: str, params: dict | None = None, notification: bool = False) -> Any:
        body: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            self._id += 1
            body["id"] = self._id
        resp = http.request("POST", self.url, json=body, headers=self._headers(), timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise NodeError("MCP server rejected the credentials", code="auth_failed")
        http.raise_for_status(resp, "MCP server")
        if resp.headers.get("mcp-session-id"):
            self.session_id = resp.headers["mcp-session-id"]
        if notification:
            return None
        msg = self._parse(resp)
        if not msg:
            raise NodeError("MCP server returned an empty response", code="mcp_error")
        if msg.get("error"):
            raise NodeError(f"MCP error: {str(msg['error'].get('message', 'unknown'))[:300]}", code="mcp_error")
        return msg.get("result") or {}

    def initialize(self) -> dict:
        if self._initialized:
            return {}
        result = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": {"name": "weakless-automation-engine", "version": "0.1.0"}})
        self._initialized = True
        self._rpc("notifications/initialized", notification=True)
        return result

    def list_tools(self) -> list[dict]:
        self.initialize()
        tools, cursor = [], None
        for _ in range(20):
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools += [{"name": t.get("name"), "description": t.get("description", ""),
                       "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}}} for t in result.get("tools", [])]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return {"content": text[:50000], "is_error": bool(result.get("isError")), "structured": result.get("structuredContent")}
