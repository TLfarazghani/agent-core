"""Phase 5 smoke test: identity (core/meta.py + inspect_self).

Proves AGENT_NAME is the shared constant, the bio block carries facts (name,
target, model, budgets, live tool list), and inspect_self reports accurate
session/context/tool state through the registry. Runnable directly or via pytest.
"""

from __future__ import annotations

import json

from core import (
    AGENT_NAME,
    AgentState,
    ToolCall,
    ToolRegistry,
    agent_bio,
    estimate_tokens,
    inspect_self,
    new_state,
)
from tools.cognitive import register_cognitive_tools
from windows.orchestrator import default_registry


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_cognitive_tools(registry)
    registry.load_json(
        "tools/registry.json",
        {"echo": lambda args: "echo"},
        names={"echo"},
    )
    return registry


def test_agent_name_shared_constant() -> None:
    assert AGENT_NAME == "Agent Core"


def test_bio_block_contains_identity_facts() -> None:
    registry = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    bio = agent_bio(state, registry)
    assert "Agent Core" in bio
    assert "windows" in bio
    assert "LFM2.5-1.2B-Instruct" in bio
    assert "echo" in bio
    assert "inspect_self" in bio
    assert "turn budget" in bio.lower()


def test_bio_reflects_live_tool_list() -> None:
    registry = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    bio = agent_bio(state, registry)
    names = sorted({d.name for d in registry.definitions()})
    assert all(name in bio for name in names)


def test_inspect_self_reports_accurate_state() -> None:
    registry = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    state.turn_count = 2
    state.retry_count = 1
    snapshot = json.loads(inspect_self(state, registry))
    assert snapshot["name"] == "Agent Core"
    assert snapshot["session_id"] == state.session_id
    assert snapshot["target"] == "windows"
    assert snapshot["model"] == "LFM2.5-1.2B-Instruct"
    assert snapshot["turn_count"] == 2
    assert snapshot["max_turns"] == state.max_turns
    assert snapshot["estimated_context_tokens"] == estimate_tokens(state.messages)
    assert snapshot["pending_approval"] is None
    assert set(snapshot["tools"]) == {d.name for d in registry.definitions()}


def test_inspect_self_tool_through_registry() -> None:
    registry = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    state.messages.append(
        __import__("core", fromlist=["ChatMessage"]).ChatMessage(role="system", content="sys")
    )
    msg = registry.dispatch(
        state,
        ToolCall(id="c1", name="inspect_self", arguments={}),
    )
    assert msg is not None and msg.role == "tool"
    snapshot = json.loads(msg.content)
    assert snapshot["session_id"] == state.session_id
    assert "inspect_self" in snapshot["tools"]
    assert snapshot["estimated_context_tokens"] == estimate_tokens(state.messages)


def test_default_registry_includes_cognitive_tools() -> None:
    registry = default_registry()
    names = {d.name for d in registry.definitions()}
    assert {"inspect_self", "remember", "recall", "make_plan", "update_plan"} <= names


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print(f"\nAll {len(tests)} meta tests passed.")


if __name__ == "__main__":
    main()