"""Phase 10 ``agent`` tool: bounded, one-level sub-agents.

The handler needs a ``GenerateProvider`` for the nested loop, so it is
registered by the **transport** (``cli.py``, ``web/server.py``) where a provider
exists -- not by ``default_registry()`` (which has no provider). The core logic
lives in ``core/subagent.py`` and is transport-agnostic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from core import takes_state
from core.subagent import MAX_SUBAGENT_TURNS, SUBAGENT_NEEDS_APPROVAL, spawn_subagent

AGENT_TOOLS = {"agent"}


def make_agent_handler(
    registry,
    provider_factory: Optional[Callable[[], Callable]] = None,
    memory_dir: Optional[Path] = None,
) -> Callable:
    def handler(state, arguments: dict) -> str:
        if provider_factory is None:
            return "error: sub-agents are unavailable in this transport (no provider)"
        goal = arguments.get("goal", "")
        try:
            provider = provider_factory()
        except Exception as exc:  # noqa: BLE001 - surface as a tool result
            return f"error: could not start sub-agent: {exc}"
        max_turns = arguments.get("max_turns", MAX_SUBAGENT_TURNS)
        result = spawn_subagent(
            state,
            goal,
            provider,
            registry,
            max_turns=max_turns,
            memory_dir=memory_dir,
        )
        if result.status == SUBAGENT_NEEDS_APPROVAL and result.pending is not None:
            # Bubble the gated call to the parent loop, where the human is
            # asked; the sub-agent itself never auto-approves.
            state.pending_approval = result.pending
        return result.summary

    return takes_state(handler)


def register_subagent_tool(
    registry,
    provider_factory: Optional[Callable[[], Callable]] = None,
    memory_dir: Optional[Path] = None,
) -> None:
    """Register the ``agent`` tool. ``provider_factory`` returns a fresh
    ``GenerateProvider`` per sub-agent (no shared stream callback)."""
    handlers = {"agent": make_agent_handler(registry, provider_factory, memory_dir)}
    registry.load_json("tools/registry.json", handlers, names=AGENT_TOOLS)
