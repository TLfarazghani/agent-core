"""Platform-agnostic agent loop.

Owns the state machine. Platforms never mutate ``AgentState`` directly; they
only provide a ``GenerateProvider`` and a ``ToolRegistry``.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from .state import AgentState, ChatMessage, ToolCall
from .tool_registry import ToolRegistry

GenerateProvider = Callable[[AgentState], ChatMessage]

# Retry-once: after a tool failure in one user turn, the loop allows one more
# generate+execute cycle before giving up cleanly. Never auto-retries a
# rejected call (the resolve_approval(approved=False) path stays the only way
# through for those).
RETRY_ONCE_LIMIT = 1


def should_stop_after_retries(state: AgentState) -> bool:
    return state.retry_count > RETRY_ONCE_LIMIT


def _note_tool_result(state: AgentState, tool_msg: ChatMessage | None) -> None:
    """Count a tool failure toward the retry-once budget."""
    if tool_msg is not None and tool_msg.role == "tool" and tool_msg.content.startswith("error"):
        state.retry_count += 1


def finalize_turn(state: AgentState, memory_dir=None) -> None:
    """Reflection hook at a terminal state. Bounded; never raises."""
    try:
        from .reflection import maybe_emit_lesson

        maybe_emit_lesson(state, memory_dir=memory_dir)
    except Exception:  # noqa: BLE001 - reflection must never block the loop
        pass


class PendingApprovalError(RuntimeError):
    pass


class MaxTurnsError(RuntimeError):
    pass


class GenerateProviderProtocol(Protocol):
    def __call__(self, state: AgentState) -> ChatMessage:
        ...


def step(
    state: AgentState,
    provider: GenerateProvider,
    registry: ToolRegistry,
) -> AgentState:
    """One generate + parse + dispatch cycle. Mutates and returns ``state``."""
    if state.pending_approval is not None:
        raise PendingApprovalError(
            "step() refused: pending_approval is set; call resolve_approval() first"
        )
    if state.turn_count >= state.max_turns:
        raise MaxTurnsError(
            f"turn cap reached ({state.max_turns}); start a new session"
        )

    last_before = state.messages[-1] if state.messages else None
    if last_before is not None and last_before.role == "user":
        state.retry_count = 0

    response = provider(state)
    state.messages.append(response)
    state.turn_count += 1

    for i, call in enumerate(response.function_calls or []):
        tool_msg = registry.dispatch(state, call)
        _note_tool_result(state, tool_msg)
        if tool_msg is None:
            # Approval parked for this call. Keep the calls after it in this
            # turn so resolve_approval() can resume them (fixes silent loss
            # of multi-call turns: [run_code, echo] used to drop echo).
            state.pending_calls = list((response.function_calls or [])[i + 1 :])
            break
        state.messages.append(tool_msg)
    return state


def run(
    state: AgentState,
    provider: GenerateProvider,
    registry: ToolRegistry,
    memory_dir=None,
) -> AgentState:
    """Drive ``step()`` until the agent reaches a terminal state.

    Terminal states: a pending approval, the turn cap, a retry-once give-up,
    or an assistant message that makes no tool calls. On a terminal state a
    bounded reflection lesson may be written to memory.
    """
    while True:
        if state.pending_approval is not None:
            return state
        if state.turn_count >= state.max_turns:
            return state
        if should_stop_after_retries(state):
            finalize_turn(state, memory_dir)
            return state
        last = state.messages[-1] if state.messages else None
        if last is not None and last.role == "assistant" and not last.function_calls:
            finalize_turn(state, memory_dir)
            return state
        step(state, provider, registry)


def resolve_approval(
    state: AgentState,
    registry: ToolRegistry,
    approved: bool,
) -> AgentState:
    """Resolve a pending approval.

    Only on ``approved=True`` does the tool actually execute. ``approved=False``
    clears the pending state without executing anything. After the pending call
    is resolved, any tool calls from the same turn that were parked behind it
    (``state.pending_calls``) are resumed in order.
    """
    pending = state.pending_approval
    if pending is None:
        return state
    state.pending_approval = None
    if approved:
        call = ToolCall(id=pending.call_id, name=pending.tool_name, arguments=pending.arguments)
        tool_msg = registry.execute(call, state)
        _note_tool_result(state, tool_msg)
        state.messages.append(tool_msg)
    else:
        state.messages.append(
            ChatMessage(
                role="tool",
                tool_call_id=pending.call_id,
                content="rejected by user",
            )
        )
    remaining = list(state.pending_calls)
    state.pending_calls = []
    for i, call in enumerate(remaining):
        tool_msg = registry.dispatch(state, call)
        _note_tool_result(state, tool_msg)
        if tool_msg is None:
            state.pending_calls = list(remaining[i + 1 :])
            break
        state.messages.append(tool_msg)
    return state


def user_message(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


def new_state(*, target: str, model: str, max_turns: int = 8) -> AgentState:
    return AgentState(target=target, model=model, max_turns=max_turns)