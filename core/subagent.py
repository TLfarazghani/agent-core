"""Phase 10: bounded, one-level sub-agents.

``spawn_subagent`` runs a **nested, bounded** ``core.loop.run`` with an isolated
message list and its own turn budget, then returns a single summary string.
Two invariants hold unchanged inside a sub-agent:

- the **hardcoded approval gate** in ``core.tool_registry.dispatch()`` still runs
  (a sub-agent never auto-approves and never bypasses ``resolve_approval``);
- retry-once (``core/loop.py``) still applies.

Sub-agents are **one level deep**: the child is marked with
``active_subagents = MAX_CONCURRENT_SUBAGENTS`` so a nested ``agent`` call is
refused, and concurrency is capped. When a sub-agent hits an approval-gated
tool it stops and hands the pending call back (``status="needs_approval"``);
the caller (the ``agent`` tool) bubbles it to the parent loop, which asks the
human. Stdlib-only and transport-agnostic (JS port: ``web/subagent.js``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .loop import run
from .state import AgentState, ChatMessage, PendingApproval

MAX_SUBAGENT_TURNS = 4
MAX_CONCURRENT_SUBAGENTS = 1

SUBAGENT_SYSTEM_PROMPT = (
    "You are a bounded sub-agent of Agent Core. Work only on the stated goal, "
    "use the available tools when needed, and finish by replying with a short, "
    "self-contained summary of what you did and found. You cannot ask the user "
    "questions; if a tool requires approval, stop and report it. Do not spawn "
    "further sub-agents."
)

# Statuses a sub-agent run can end in.
SUBAGENT_OK = "ok"
SUBAGENT_NEEDS_APPROVAL = "needs_approval"
SUBAGENT_REJECTED = "rejected"


@dataclass
class SubagentResult:
    """Outcome of one sub-agent run."""

    status: str
    summary: str
    turns: int = 0
    pending: Optional[PendingApproval] = None


def _child_state(parent: AgentState, goal: str, max_turns: int) -> AgentState:
    child = AgentState(
        session_id=parent.session_id,
        target=parent.target,
        model=parent.model,
        max_turns=max(1, max_turns),
        permission_mode=parent.permission_mode,
    )
    # One level deep: a nested agent call sees the cap already reached.
    child.active_subagents = MAX_CONCURRENT_SUBAGENTS
    child.messages.append(ChatMessage(role="system", content=SUBAGENT_SYSTEM_PROMPT))
    child.messages.append(ChatMessage(role="user", content=goal))
    return child


def _last_summary(child: AgentState) -> str:
    for message in reversed(child.messages):
        if message.role == "assistant" and message.content and not message.function_calls:
            return message.content.strip()
    for message in reversed(child.messages):
        if message.role == "assistant" and message.content:
            return message.content.strip()
    return "(the sub-agent produced no summary)"


def spawn_subagent(
    parent_state: AgentState,
    goal: str,
    provider: Callable[[AgentState], ChatMessage],
    registry,
    max_turns: int = MAX_SUBAGENT_TURNS,
    memory_dir=None,
) -> SubagentResult:
    """Run a bounded nested loop for ``goal`` and return its summary.

    ``provider`` produces one assistant message for a child ``AgentState`` (the
    same ``GenerateProvider`` contract as the parent loop). The sub-agent never
    auto-approves: on a gated tool it returns ``needs_approval`` with the pending
    call for the caller to bubble to the parent/human.
    """
    goal = (goal or "").strip()
    if not goal:
        return SubagentResult(SUBAGENT_REJECTED, "error: sub-agent goal is empty")
    if parent_state.active_subagents >= MAX_CONCURRENT_SUBAGENTS:
        return SubagentResult(
            SUBAGENT_REJECTED,
            "error: a sub-agent is already running (one level deep, one at a time)",
        )

    child = _child_state(parent_state, goal, max_turns)
    parent_state.active_subagents += 1
    try:
        run(child, provider, registry, memory_dir)
    finally:
        parent_state.active_subagents = max(0, parent_state.active_subagents - 1)

    if child.pending_approval is not None:
        return SubagentResult(
            SUBAGENT_NEEDS_APPROVAL,
            (
                "Sub-agent stopped: tool "
                f"{child.pending_approval.tool_name!r} requires human approval."
            ),
            turns=child.turn_count,
            pending=child.pending_approval,
        )
    return SubagentResult(SUBAGENT_OK, _last_summary(child), turns=child.turn_count)
