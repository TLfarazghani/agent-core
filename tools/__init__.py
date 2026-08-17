"""Networked tools registered over the MCP-remote transport.

Exposes a single entrypoint that loads the networked subset of
``tools/registry.json`` and binds each tool to the shared remote client.
"""

from __future__ import annotations

from core.tool_registry import ToolRegistry

from .remote import McpClient, tool_handler

NETWORKED_TOOLS = {"web_search", "send_email", "send_message"}


def register_networked_tools(
    registry: ToolRegistry,
    base_url: str,
    api_key: str | None = None,
) -> ToolRegistry:
    client = McpClient(base_url=base_url, api_key=api_key)
    handlers = {name: tool_handler(client, name) for name in NETWORKED_TOOLS}
    registry.load_json("tools/registry.json", handlers, names=NETWORKED_TOOLS)
    return registry