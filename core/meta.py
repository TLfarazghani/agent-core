"""Agent Core identity: the shared name + self-introspection.

Stdlib-only and transport-agnostic (the JS mirror lives in ``web/engine.js``
and ``web/worker.js``). ``AGENT_NAME`` is the single source of truth for the
agent's name; ``agent_bio`` builds the identity block injected into the system
prompt so the agent answers "who are you / what can you do" from fact, and
``inspect_self`` returns a live snapshot of the running state.
"""

from __future__ import annotations

import json
import time

from .context import estimate_tokens

AGENT_NAME = "Agent Core"
DEFAULT_CONTEXT_TOKENS = 32768


def current_time() -> str:
    """Machine-clock date/time for the identity block, e.g. 2026-08-19 14:03."""
    return time.strftime("%Y-%m-%d %H:%M")


def agent_bio(
    state,
    registry,
    max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
) -> str:
    """Identity block: name, target, model, context budget, turn budget, tools,
    and the current date/time from the machine clock.

    Injected as a system message so identity questions are answered from fact.
    The date line makes "today"/"what date is it" queries answerable from the
    clock instead of the model's (stale) training prior; time-sensitive facts
    beyond that should be confirmed with ``web_search``.
    """
    tools = ", ".join(sorted(d.name for d in registry.definitions())) or "(none)"
    return (
        f"You are {AGENT_NAME}.\n"
        f"Current date/time: {current_time()}.\n"
        f"Target: {state.target}. Model: {state.model}.\n"
        f"Context budget: {estimate_tokens(state.messages)} ~tokens / {max_context_tokens}.\n"
        f"Turn budget: {state.turn_count}/{state.max_turns} used.\n"
        f"Available tools: {tools}.\n"
        "You answer questions about yourself from this block; never invent "
        "capabilities you do not have."
    )


def inspect_self(
    state,
    registry,
    max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
) -> str:
    """Live snapshot for the ``inspect_self`` tool (JSON string)."""
    snapshot = {
        "name": AGENT_NAME,
        "session_id": state.session_id,
        "target": state.target,
        "model": state.model,
        "turn_count": state.turn_count,
        "max_turns": state.max_turns,
        "estimated_context_tokens": estimate_tokens(state.messages),
        "max_context_tokens": max_context_tokens,
        "pending_approval": (
            state.pending_approval.model_dump() if state.pending_approval else None
        ),
        "plan": state.plan.model_dump() if state.plan else None,
        "tools": sorted(d.name for d in registry.definitions()),
    }
    return json.dumps(snapshot, indent=2)
