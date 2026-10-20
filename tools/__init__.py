"""Tool registration helpers.

- ``register_web_tools`` — web_search (DuckDuckGo / Google News / Wikipedia) and
  fetch_url: real, keyless, stdlib-only web search (no MCP remote required).
- ``register_email_tools`` — send_email over native SMTP (stdlib smtplib/email;
  injectable smtp_factory + settings seams). Default-registry email so sessions
  without ``MCP_BASE_URL`` can still send (approval-gated).
- ``register_networked_tools`` — Phase 1: send_message over the shared
  MCP-remote client (web_search is now local via register_web_tools;
  send_email is now local via register_email_tools and is skipped here when
  already registered so MCP never duplicates it).
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
from .email import make_handler as make_email_handler
from .run_code import make_handler as make_runcode_handler
from .files import (
    make_edit_file_handler,
    make_glob_handler,
    make_grep_handler,
    make_list_dir_handler,
    make_read_file_handler,
    make_write_file_handler,
)
from .skills import SKILL_TOOLS, register_skills_tools
from .subagent import AGENT_TOOLS, register_subagent_tool
from .tasks import TASK_TOOLS, register_task_tools

WEB_TOOLS = {"web_search", "fetch_url"}
EMAIL_TOOLS = {"send_email"}
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
    resolver: Callable | None = None,
) -> ToolRegistry:
    """web_search + fetch_url behind the SSRF guard.

    Inject ``urlopen`` (fake HTTP) and ``resolver`` (fake DNS) in tests so no
    network or live DNS is touched; both default to the real guarded transport.
    """
    handlers = {
        "web_search": make_websearch_handler(urlopen=urlopen, resolver=resolver),
        "fetch_url": make_fetch_handler(urlopen=urlopen, resolver=resolver),
    }
    registry.load_json("tools/registry.json", handlers, names=WEB_TOOLS)
    return registry


def register_email_tools(
    registry: ToolRegistry,
    smtp_factory: Callable[..., Any] | None = None,
    settings: Callable[[], dict[str, Any]] | None = None,
) -> ToolRegistry:
    """Native SMTP send_email: the default-registry email path.

    Injectable ``smtp_factory`` (connection maker) and ``settings`` (config
    reader) so tests never touch the network or the live environment --
    same seam pattern as run_code's injectable docker client.
    """
    handlers = {"send_email": make_email_handler(smtp_factory=smtp_factory, settings=settings)}
    registry.load_json("tools/registry.json", handlers, names=EMAIL_TOOLS)
    return registry


def register_networked_tools(
    registry: ToolRegistry,
    base_url: str,
    api_key: str | None = None,
) -> ToolRegistry:
    """Phase 1 MCP-remote tools (send_message; send_email when not already registered).

    Tools already registered (e.g. native SMTP send_email from
    ``register_email_tools``) are skipped instead of raising ``duplicate tool
    registration`` -- the local-first native path wins; set no
    ``AGENT_CORE_SMTP_*`` env and rebuild the registry without email tools to
    force the MCP transport.
    """
    remaining = {name for name in NETWORKED_TOOLS if registry.definition(name) is None}
    if not remaining:
        return registry
    client = McpClient(base_url=base_url, api_key=api_key)
    handlers = {name: tool_handler(client, name) for name in remaining}
    registry.load_json("tools/registry.json", handlers, names=remaining)
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
    **handler_kwargs: Any,
) -> ToolRegistry:
    """Pass `handler_kwargs` through to the run_code handler (sandbox knobs:
    mem_limit / nano_cpus / pids_limit / max_output_chars / read_only / cap_drop)."""
    handlers = {"run_code": make_runcode_handler(client=docker_client, **handler_kwargs)}
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