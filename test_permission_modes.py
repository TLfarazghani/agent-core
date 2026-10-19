"""Phase 8: permission modes, enforced in core/tool_registry.dispatch().

No live model, no Docker. The point of this suite is the invariant that the
hardcoded run_code gate always wins: ``auto`` may skip the flag, ``plan`` may
skip non-read-only tools, ``strict`` may park everything -- but run_code still
waits for ``resolve_approval(approved=True)`` in every mode.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from core import (
    APPROVAL_MODES,
    HARDCODED_APPROVAL_TOOLS,
    PERMISSION_MODES,
    AgentState,
    ChatMessage,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    new_state,
)
from core.loop import resolve_approval

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Read-only echo.",
            read_only=True,
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        lambda arguments: f"echo: {arguments['text']}",
    )
    registry.register(
        ToolDefinition(
            name="mutate",
            description="Mutating but not gated.",
            read_only=False,
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        lambda arguments: f"mutated: {arguments['text']}",
    )
    registry.register(
        ToolDefinition(
            name="risky",
            description="Gated tool.",
            requires_approval=True,
            read_only=False,
            parameters={
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        ),
        lambda arguments: "risky ran",
    )
    registry.register(
        ToolDefinition(
            name="run_code",
            description="Sandbox.",
            requires_approval=True,
            read_only=False,
            parameters={
                "type": "object",
                "properties": {"language": {"type": "string"}, "code": {"type": "string"}},
                "required": ["code"],
            },
        ),
        lambda arguments: "sandbox ran",
    )
    return registry


def state_with_mode(mode: str) -> AgentState:
    state = new_state(target="windows", model="test-model")
    state.permission_mode = mode  # type: ignore[assignment]
    return state


def call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(id=f"call-{name}", name=name, arguments=arguments)


def test_modes_are_the_documented_four() -> None:
    assert set(PERMISSION_MODES) == {"default", "plan", "auto", "strict"}
    assert set(APPROVAL_MODES) == set(PERMISSION_MODES)
    assert HARDCODED_APPROVAL_TOOLS == frozenset({"run_code"})
    assert new_state(target="windows", model="m").permission_mode == "default"


def test_default_mode_parks_only_gated_tools() -> None:
    registry = make_registry()
    state = state_with_mode("default")
    assert registry.dispatch(state, call("echo", {"text": "x"})) is not None
    assert registry.dispatch(state, call("mutate", {"text": "x"})) is not None
    assert registry.dispatch(state, call("risky", {"code": "x"})) is None
    assert state.pending_approval is not None
    assert state.pending_approval.tool_name == "risky"


def test_plan_mode_runs_only_read_only_tools() -> None:
    registry = make_registry()
    state = state_with_mode("plan")
    result = registry.dispatch(state, call("echo", {"text": "x"}))
    assert result is not None and result.content == "echo: x"
    assert registry.dispatch(state, call("mutate", {"text": "x"})) is None
    assert state.pending_approval is not None

    state.pending_approval = None
    assert registry.dispatch(state, call("risky", {"code": "x"})) is None
    assert state.pending_approval is not None


def test_auto_mode_runs_gated_tools_but_never_run_code() -> None:
    registry = make_registry()
    state = state_with_mode("auto")
    assert registry.dispatch(state, call("echo", {"text": "x"})) is not None
    assert registry.dispatch(state, call("mutate", {"text": "x"})) is not None
    assert registry.dispatch(state, call("risky", {"code": "x"})) is not None

    assert registry.dispatch(state, call("run_code", {"code": "print(1)"})) is None
    assert state.pending_approval is not None
    assert state.pending_approval.tool_name == "run_code"

    # Only the explicit human decision runs it.
    assert registry.execute(call("run_code", {"code": "print(1)"}), state).content == "sandbox ran"
    resolve_approval(state, registry, approved=True)
    assert state.pending_approval is None
    assert state.messages[-1].content == "sandbox ran"


def test_strict_mode_parks_everything() -> None:
    registry = make_registry()
    state = state_with_mode("strict")
    for name, arguments in (
        ("echo", {"text": "x"}),
        ("mutate", {"text": "x"}),
        ("risky", {"code": "x"}),
    ):
        state.pending_approval = None
        assert registry.dispatch(state, call(name, arguments)) is None, name
        assert state.pending_approval is not None


def test_run_code_never_bypassable_in_any_mode() -> None:
    registry = make_registry()
    for mode in PERMISSION_MODES:
        state = state_with_mode(mode)
        assert registry.dispatch(state, call("run_code", {"code": "print(1)"})) is None, mode
        assert state.pending_approval is not None, mode
        assert state.pending_approval.tool_name == "run_code", mode


def test_rejection_never_executes_in_auto_mode() -> None:
    registry = make_registry()
    state = state_with_mode("auto")
    registry.dispatch(state, call("run_code", {"code": "print(1)"}))
    resolve_approval(state, registry, approved=False)
    assert state.pending_approval is None
    assert all(message.content != "sandbox ran" for message in state.messages)


def test_invalid_arguments_park_nothing() -> None:
    registry = make_registry()
    state = state_with_mode("strict")
    result = registry.dispatch(state, call("echo", {}))
    assert result is not None
    assert result.content.startswith("error:")
    assert state.pending_approval is None


def test_dispatch_without_state_keeps_legacy_behavior() -> None:
    """``execute`` and plain registry callers must not need a permission mode."""
    registry = make_registry()
    assert registry.execute(call("risky", {"code": "x"})).content == "risky ran"


def test_state_round_trips_the_new_fields() -> None:
    state = state_with_mode("strict")
    dumped = state.model_dump()
    assert dumped["permission_mode"] == "strict"
    with (_SCHEMA_DIR / "agent_state.schema.json").open(encoding="utf-8") as fh:
        schema = json.load(fh)
    jsonschema.validate(instance=dumped, schema=schema)

    with (_SCHEMA_DIR / "tool_definition.schema.json").open(encoding="utf-8") as fh:
        tool_schema = json.load(fh)
    jsonschema.validate(
        instance={"name": "t", "description": "d", "read_only": True, "parameters": {"type": "object"}},
        schema=tool_schema,
    )


def test_invalid_mode_rejected_by_pydantic() -> None:
    with pytest.raises(Exception):
        AgentState(target="windows", model="m", permission_mode="yolo")  # type: ignore[arg-type]


def test_registry_json_marks_read_only_tools() -> None:
    entries = json.loads((Path(__file__).resolve().parent / "tools" / "registry.json").read_text(encoding="utf-8"))
    by_name = {entry["name"]: entry for entry in entries}
    for name in ("echo", "web_search", "fetch_url", "inspect_self", "recall", "make_plan", "update_plan"):
        assert by_name[name].get("read_only") is True, name
    for name in ("run_code", "create_docx", "create_pptx", "send_email", "send_message"):
        assert by_name[name].get("read_only", False) is False, name


if __name__ == "__main__":
    tests = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS  {test.__name__}")
    print(f"\nAll {len(tests)} permission-mode tests passed.")
