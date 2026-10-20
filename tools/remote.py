"""MCP-remote transport for networked tools (web_search, send_email, send_message).

Thin, stdlib-only JSON-RPC client over HTTP that speaks the MCP ``tools/call``
method shape. This is the "identical implementation shape across all three
platforms" from the research doc: the heavy logic lives in the remote MCP
server, and each platform ships the same thin client. Keep this module
stdlib-only so it ports line-for-line to parser.js / AgentCore.kt.

Endpoint contract (implemented by the mock in test_networked_tools.py and by a
real MCP Streamable HTTP server):

    POST {base_url}
    {"jsonrpc":"2.0","id":1,"method":"tools/call",
     "params":{"name":"web_search","arguments":{"query":"..."}}}

    -> {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"..."}]}}

Auth scheme: when an api_key is configured, every request carries
``Authorization: Bearer <api_key>`` (the exact configured key — never a
masked/redacted placeholder going over the wire); with no key configured NO
Authorization header is sent at all (an empty or absent key is unconfigured,
not "a literal '' password"). Client-side error messages NEVER embed the
configured key: non-2xx responses surface status + server-provided detail
only, and any accidental echo of the key inside a server body is redacted to
"***". The key is also never logged by this module.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

JSON_RPC_VERSION = "2.0"


class RemoteError(RuntimeError):
    pass


class McpClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30.0):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    def _auth_headers(self) -> dict[str, str]:
        """Authorization for the configured key; empty dict when unconfigured."""
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _redact(text: str, api_key: str | None) -> str:
        """Scrub an accidental key echo out of server-provided detail."""
        if api_key and api_key in text:
            return text.replace(api_key, "***")
        return text

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        body = json.dumps(
            {
                "jsonrpc": JSON_RPC_VERSION,
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        ).encode("utf-8")

        headers = {"Content-Type": "application/json", **self._auth_headers()}

        try:
            request = urllib.request.Request(
                self.base_url, data=body, headers=headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Non-2xx (401/403 auth failures, 4xx/5xx in general). Surface the
            # status + server detail, never our own headers/request content:
            # a server body that echoes the Authorization header is scrubbed
            # so the configured key never lands in model context or logs.
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001 - body may be unreadable
                detail = ""
            detail = self._redact(detail, self.api_key)
            raise RemoteError(
                f"remote http error {exc.code}: {detail}".rstrip()
            ) from exc
        except urllib.error.URLError as exc:
            raise RemoteError(f"remote unreachable: {exc.reason or exc}") from exc
        except json.JSONDecodeError as exc:
            raise RemoteError(f"remote returned non-JSON response: {exc}") from exc

        if "error" in payload:
            raise RemoteError(f"remote error: {payload['error']}")
        result = payload.get("result") or {}
        if result.get("isError"):
            raise RemoteError(
                f"remote tool '{name}' failed: {result.get('content', '')}"
            )
        text_parts = [
            item.get("text", "")
            for item in result.get("content") or []
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(text_parts)


def tool_handler(client: McpClient, tool_name: str):
    """Return a ``ToolRegistry`` handler that forwards to the remote."""

    def handler(arguments: dict[str, Any]) -> str:
        return client.call_tool(tool_name, arguments)

    return handler