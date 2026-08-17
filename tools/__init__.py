"""Tool registration helpers.

- ``register_networked_tools`` — Phase 1: web_search / send_email / send_message
  over the shared MCP-remote client.
- ``register_docgen_tools`` — Phase 2: create_docx / create_pptx local compute
  (Windows backend: python-docx / python-pptx).
- ``register_runcode_tool`` — Phase 3: run_code Docker sandbox (approval-gated).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.tool_registry import ToolRegistry

from .remote import McpClient, tool_handler
from .create_docx import make_handler as make_docx_handler
from .create_pptx import make_handler as make_pptx_handler
from .run_code import make_handler as make_runcode_handler

NETWORKED_TOOLS = {"web_search", "send_email", "send_message"}
DOCGEN_TOOLS = {"create_docx", "create_pptx"}
RUNCODE_TOOLS = {"run_code"}


def register_networked_tools(
    registry: ToolRegistry,
    base_url: str,
    api_key: str | None = None,
) -> ToolRegistry:
    client = McpClient(base_url=base_url, api_key=api_key)
    handlers = {name: tool_handler(client, name) for name in NETWORKED_TOOLS}
    registry.load_json("tools/registry.json", handlers, names=NETWORKED_TOOLS)
    return registry


def register_docgen_tools(
    registry: ToolRegistry,
    output_dir: str | Path | None = None,
) -> ToolRegistry:
    handlers = {
        "create_docx": make_docx_handler(Path(output_dir) if output_dir else None),
        "create_pptx": make_pptx_handler(Path(output_dir) if output_dir else None),
    }
    registry.load_json("tools/registry.json", handlers, names=DOCGEN_TOOLS)
    return registry


def register_runcode_tool(
    registry: ToolRegistry,
    docker_client: Any | None = None,
) -> ToolRegistry:
    handlers = {"run_code": make_runcode_handler(client=docker_client)}
    registry.load_json("tools/registry.json", handlers, names=RUNCODE_TOOLS)
    return registry