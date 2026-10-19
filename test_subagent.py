"""Phase 10 smoke test: bounded, one-level sub-agents.

Self-contained: fake providers, no llama-server. Proves the summary contract,
the one-level/one-at-a-time caps, and -- most importantly -- that a gated tool
inside a sub-agent bubbles up to the parent's approval gate and still only runs
through ``resolve_approval(approved=True)``. Runnable directly or via pytest.
"""

from __future__ import annotations

from core import (
    MAX_CONCURRENT_SUBAGENTS,
    ChatMessage,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    new_state,
    resolve_approval,
    spawn_subagent,
)
from core.subagent import SUBAGENT_NEEDS_APPROVAL, SUBAGENT_OK, SUBAGENT_REJECTED
from tools import register_subagent_tool


def _state():
    return new_state(target="windows", model="test-model")


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="echo",
            requires_approval=False,
            read_only=True,
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        lambda arguments: f"echo: {arguments.get('text')}",
    )
    registry.register(
        ToolDefinition(
            name="run_code",
            description="run code (hardcoded approval)",
            requires_approval=True,
            parameters={
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        ),
        lambda arguments: "ran: " + str(arguments.get("code")),
    )
    return registry


def _script(*messages: ChatMessage):
    queue = list(messages)

    def provider(state):
        return queue.pop(0)

    return provider


def test_spawn_returns_summary_and_restores_counter() -> None:
    parent = _state()
    provider = _script(ChatMessage(role="assistant", content="final summary"))
    result = spawn_subagent(parent, "do a thing", provider, _echo_registry())
    assert result.status == SUBAGENT_OK
    assert result.summary == "final summary"
    assert result.turns == 1
    assert parent.active_subagents == 0


def test_nested_agent_call_is_refused() -> None:
    parent = _state()
    registry = _echo_registry()
    provider = _script(
        ChatMessage(
            role="assistant",
            content="",
            function_calls=[ToolCall(id="n1", name="agent", arguments={"goal": "deeper"})],
        ),
        ChatMessage(role="assistant", content="recovered"),
    )
    register_subagent_tool(registry, provider_factory=lambda: provider)
    result = spawn_subagent(parent, "top", provider, registry)
    assert result.status == SUBAGENT_OK
    assert result.summary == "recovered"


def test_spawn_refuses_when_a_subagent_is_already_running() -> None:
    parent = _state()
    parent.active_subagents = MAX_CONCURRENT_SUBAGENTS
    result = spawn_subagent(
        parent, "goal", _script(ChatMessage(role="assistant", content="x")), _echo_registry()
    )
    assert result.status == SUBAGENT_REJECTED
    assert parent.active_subagents == MAX_CONCURRENT_SUBAGENTS


def test_spawn_empty_goal_rejected() -> None:
    result = spawn_subagent(
        _state(), "   ", _script(ChatMessage(role="assistant", content="x")), _echo_registry()
    )
    assert result.status == SUBAGENT_REJECTED


def test_spawn_bubbles_gated_tool_as_needs_approval() -> None:
    parent = _state()
    provider = _script(
        ChatMessage(
            role="assistant",
            content="",
            function_calls=[ToolCall(id="c1", name="run_code", arguments={"code": "print(1)"})],
        )
    )
    result = spawn_subagent(parent, "run it", provider, _echo_registry())
    assert result.status == SUBAGENT_NEEDS_APPROVAL
    assert result.pending is not None
    assert result.pending.tool_name == "run_code"
    assert parent.active_subagents == 0
    assert parent.pending_approval is None  # the tool, not the core, bubbles it


def test_agent_tool_returns_summary() -> None:
    registry = _echo_registry()
    register_subagent_tool(
        registry,
        provider_factory=lambda: _script(ChatMessage(role="assistant", content="researched")),
    )
    state = _state()
    msg = registry.dispatch(state, ToolCall(id="a1", name="agent", arguments={"goal": "x"}))
    assert msg is not None
    assert msg.content == "researched"
    assert state.pending_approval is None


def test_agent_tool_bubbles_approval_to_parent_gate() -> None:
    registry = _echo_registry()
    register_subagent_tool(
        registry,
        provider_factory=lambda: _script(
            ChatMessage(
                role="assistant",
                content="",
                function_calls=[
                    ToolCall(id="c1", name="run_code", arguments={"code": "print(1)"})
                ],
            ),
            ChatMessage(role="assistant", content="unused"),
        ),
    )
    state = _state()
    msg = registry.dispatch(state, ToolCall(id="a1", name="agent", arguments={"goal": "x"}))
    assert msg is not None
    assert "requires human approval" in msg.content
    assert state.pending_approval is not None
    assert state.pending_approval.tool_name == "run_code"

    resolve_approval(state, registry, approved=True)
    assert state.messages[-1].content == "ran: print(1)"


def test_agent_tool_rejection_never_runs() -> None:
    registry = _echo_registry()
    register_subagent_tool(
        registry,
        provider_factory=lambda: _script(
            ChatMessage(
                role="assistant",
                content="",
                function_calls=[
                    ToolCall(id="c1", name="run_code", arguments={"code": "print(1)"})
                ],
            )
        ),
    )
    state = _state()
    registry.dispatch(state, ToolCall(id="a1", name="agent", arguments={"goal": "x"}))
    assert state.pending_approval is not None
    resolve_approval(state, registry, approved=False)
    assert state.messages[-1].content == "rejected by user"
    assert all("ran:" not in m.content for m in state.messages if m.role == "tool")


def test_agent_tool_without_provider_reports_error() -> None:
    registry = _echo_registry()
    register_subagent_tool(registry)
    state = _state()
    msg = registry.dispatch(state, ToolCall(id="a1", name="agent", arguments={"goal": "x"}))
    assert msg is not None
    assert msg.content.startswith("error:")


if __name__ == "__main__":
    import sys

    failures = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - test runner
            failures += 1
            print(f"FAIL  {test.__name__}: {exc!r}")
    print(f"\nAll {len(tests) - failures}/{len(tests)} subagent tests passed.")
    sys.exit(1 if failures else 0)
