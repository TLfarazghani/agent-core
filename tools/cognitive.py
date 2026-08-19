"""Phase 5 cognitive tool handlers: identity, memory, planning.

State-aware handlers are invoked as ``handler(state, arguments)`` (marked with
``core.tool_registry.takes_state``). All are ``requires_approval: false`` --
they are read/write of the agent's own persistent state, not privileged
actions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from core import KINDS, meta, planner, recall_bounded, takes_state
from core.memory import save_memory

COGNITIVE_TOOLS = {"inspect_self", "remember", "recall", "make_plan", "update_plan"}


def _format_memory(entry: dict) -> str:
    return f"[{entry.get('kind')}] {entry.get('key')}: {entry.get('content')}"


def make_inspect_self_handler(registry) -> Callable:
    def handler(state, arguments: dict) -> str:
        return meta.inspect_self(state, registry)

    return takes_state(handler)


def make_remember_handler(memory_dir: Path | None = None) -> Callable:
    def handler(state, arguments: dict) -> str:
        kind = arguments.get("kind", "fact")
        if kind not in KINDS:
            return f"error: invalid kind '{kind}' (expected one of {KINDS})"
        try:
            entry = save_memory(
                key=arguments["key"],
                content=arguments["content"],
                kind=kind,
                source_session=state.session_id,
                memory_dir=memory_dir,
            )
        except ValueError as exc:
            return f"error: {exc}"
        return f"remembered: {_format_memory(entry)}"

    return takes_state(handler)


def make_recall_handler(memory_dir: Path | None = None) -> Callable:
    def handler(state, arguments: dict) -> str:
        topic = arguments.get("topic", "")
        limit = arguments.get("limit", 5)
        text = recall_bounded(topic, memory_dir=memory_dir, max_tokens=512)
        lines = [line for line in text.splitlines() if line]
        if not lines:
            return f"no memories found for topic {topic!r}"
        return "\n".join(lines[:limit])

    return takes_state(handler)


def make_make_plan_handler() -> Callable:
    def handler(state, arguments: dict) -> str:
        steps = arguments.get("steps")
        if isinstance(steps, str):
            steps = [steps]
        if not isinstance(steps, list) or not steps or not all(
            isinstance(s, str) and s.strip() for s in steps
        ):
            return "error: make_plan requires a non-empty list of step descriptions"
        plan = planner.make_plan(state, arguments["goal"], steps)
        return f"plan set: {json.dumps(plan.model_dump(), indent=2)}"

    return takes_state(handler)


def make_update_plan_handler() -> Callable:
    def handler(state, arguments: dict) -> str:
        try:
            step = planner.update_plan(
                state,
                arguments["step_id"],
                arguments["status"],
                result=arguments.get("result"),
            )
        except ValueError as exc:
            return f"error: {exc}"
        return f"step {step.id} -> {step.status}"

    return takes_state(handler)


def register_cognitive_tools(registry, memory_dir: Path | None = None) -> None:
    """Register inspect_self, remember, recall, make_plan, update_plan."""
    handlers = {
        "inspect_self": make_inspect_self_handler(registry),
        "remember": make_remember_handler(memory_dir),
        "recall": make_recall_handler(memory_dir),
        "make_plan": make_make_plan_handler(),
        "update_plan": make_update_plan_handler(),
    }
    registry.load_json("tools/registry.json", handlers, names=COGNITIVE_TOOLS)