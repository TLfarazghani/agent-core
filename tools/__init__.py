"""Tool registration helpers.

- ``register_web_tools`` — web_search (DuckDuckGo / Google News / Wikipedia) and
  fetch_url: real, keyless, stdlib-only web search (no MCP remote required).
- ``register_networked_tools`` — Phase 1: send_email / send_message over the
  shared MCP-remote client (web_search is now local via register_web_tools).
- ``register_docgen_tools`` — Phase 2: create_docx / create_pptx local compute
  (Windows backend: python-docx / python-pptx).
- ``register_runcode_tool`` — Phase 3: run_code Docker sandbox (approval-gated).
- ``register_cognitive_tools`` — Phase 5: inspect_self / remember / recall /
  make_plan / update_plan — identity, memory, planning (state-aware handlers).
- ``register_files_tools`` — Phase 9: read_file / write_file / edit_file /
  list_dir / glob / grep under ``AGENT_CORE_FILE_ROOT`` (writes approval-gated).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core.tool_registry import ToolRegistry

from .cognitive import COGNITIVE_TOOLS, register_cognitive_tools
from .remote import McpClient, tool_handler
from .web_search import make_handler as make_websearch_handler
from .web_search import make_fetch_handler as make_fetch_handler
from .create_docx import make_handler as make_docx_handler
from .create_pptx import make_handler as make_pptx_handler
from .run_code import make_handler as make_runcode_handler
from .files import (
    make_edit_file_handler,
    make_glob_handler,
    make_grep_handler,
    make_list_dir_handler,
    make_read_file_handler,
    make_write_file_handler,
)

WEB_TOOLS = {"web_search", "fetch_url"}
NETWORKED_TOOLS = {"send_email", "send_message"}
DOCGEN_TOOLS = {"create_docx", "create_pptx"}
RUNCODE_TOOLS = {"run_code"}
FILE_TOOLS = {
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "glob",
    "grep",
}


def register_web_tools(
    registry: ToolRegistry,
    urlopen: Callable | None = None,
) -> ToolRegistry:
    handlers = {
        "web_search": make_websearch_handler(urlopen=urlopen),
        "fetch_url": make_fetch_handler(urlopen=urlopen),
    }
    registry.load_json("tools/registry.json", handlers, names=WEB_TOOLS)
    return registry


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


def register_files_tools(
    registry: ToolRegistry,
    root: str | Path | None = None,
) -> ToolRegistry:
    base = Path(root) if root is not None else None
    handlers = {
        "read_file": make_read_file_handler(base),
        "write_file": make_write_file_handler(base),
        "edit_file": make_edit_file_handler(base),
        "list_dir": make_list_dir_handler(base),
        "glob": make_glob_handler(base),
        "grep": make_grep_handler(base),
    }
    registry.load_json("tools/registry.json", handlers, names=FILE_TOOLS)
    return registry