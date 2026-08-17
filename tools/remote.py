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

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        body = json.dumps(
            {
                "jsonrpc": JSON_RPC_VERSION,
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        ).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            request = urllib.request.Request(
                self.base_url, data=body, headers=headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RemoteError(f"remote unreachable: {exc.reason}") from exc
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