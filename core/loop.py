"""Platform-agnostic agent loop.

Owns the state machine. Platforms never mutate ``AgentState`` directly; they
only provide a ``GenerateProvider`` and a ``ToolRegistry``.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from .state import AgentState, ChatMessage, ToolCall
from .tool_registry import ToolRegistry

GenerateProvider = Callable[[AgentState], ChatMessage]


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

    response = provider(state)
    state.messages.append(response)
    state.turn_count += 1

    for call in response.function_calls or []:
        tool_msg = registry.dispatch(state, call)
        if tool_msg is None:
            break
        state.messages.append(tool_msg)
    return state


def run(
    state: AgentState,
    provider: GenerateProvider,
    registry: ToolRegistry,
) -> AgentState:
    """Drive ``step()`` until the agent reaches a terminal state.

    Terminal states: a pending approval, the turn cap, or an assistant
    message that makes no tool calls.
    """
    while True:
        if state.pending_approval is not None:
            return state
        if state.turn_count >= state.max_turns:
            return state
        last = state.messages[-1] if state.messages else None
        if last is not None and last.role == "assistant" and not last.function_calls:
            return state
        step(state, provider, registry)


def resolve_approval(
    state: AgentState,
    registry: ToolRegistry,
    approved: bool,
) -> AgentState:
    """Resolve a pending approval.

    Only on ``approved=True`` does the tool actually execute. ``approved=False``
    clears the pending state without executing anything.
    """
    pending = state.pending_approval
    if pending is None:
        return state
    state.pending_approval = None
    if approved:
        call = ToolCall(id=pending.call_id, name=pending.tool_name, arguments=pending.arguments)
        state.messages.append(registry.execute(call))
    return state


def user_message(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


def new_state(*, target: str, model: str, max_turns: int = 8) -> AgentState:
    return AgentState(target=target, model=model, max_turns=max_turns)