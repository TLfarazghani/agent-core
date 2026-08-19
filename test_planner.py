"""Phase 5 smoke test: planning (core/planner.py + AgentState.plan).

Proves create/update round-trips, schema serialization (the AgentState dump
validates against schemas/agent_state.schema.json), and the tools through the
registry. Runnable directly or via pytest.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import jsonschema

from core import (
    AgentState,
    ChatMessage,
    Plan,
    PlanStep,
    ToolCall,
    ToolRegistry,
    new_state,
    planner_make_plan,
    planner_update_plan,
)
from tools.cognitive import register_cognitive_tools

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_cognitive_tools(registry)
    return registry


def test_make_plan_sets_state() -> None:
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    plan = planner_make_plan(state, "Research X, draft a docx, summarize", ["research X", "draft docx", "summarize"])
    assert state.plan is plan
    assert plan.goal == "Research X, draft a docx, summarize"
    assert [s.description for s in plan.steps] == ["research X", "draft docx", "summarize"]
    assert [s.status for s in plan.steps] == ["pending", "pending", "pending"]
    assert [s.id for s in plan.steps] == ["step_1", "step_2", "step_3"]


def test_update_plan_status_and_result() -> None:
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    planner_make_plan(state, "goal", ["one", "two"])
    step = planner_update_plan(state, "step_1", "in_progress")
    assert state.plan.steps[0].status == "in_progress"
    step2 = planner_update_plan(state, "step_1", "done", result="everything worked")
    assert state.plan.steps[0].status == "done"
    assert state.plan.steps[0].result == "everything worked"


def test_update_plan_rejects_bad_input() -> None:
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    planner_make_plan(state, "goal", ["one"])
    for step_id, status in (("step_9", "done"), ("step_1", "bogus")):
        try:
            planner_update_plan(state, step_id, status)
        except ValueError:
            pass
        else:
            raise AssertionError(f"update_plan({step_id!r}, {status!r}) must raise")


def test_update_plan_without_plan_raises() -> None:
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    try:
        planner_update_plan(state, "step_1", "done")
    except ValueError:
        pass
    else:
        raise AssertionError("update_plan without a plan must raise")


def test_plan_serializes_and_validates_against_schema() -> None:
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    planner_make_plan(state, "goal", ["a", "b"])
    planner_update_plan(state, "step_2", "failed", result="nope")
    dump = json.loads(state.model_dump_json())
    schema = json.loads((_SCHEMA_DIR / "agent_state.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(instance=dump, schema=schema)
    assert dump["plan"]["goal"] == "goal"
    assert dump["plan"]["steps"][1]["status"] == "failed"
    assert dump["retry_count"] == 0


def test_plan_survives_model_dump_roundtrip() -> None:
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    planner_make_plan(state, "goal", ["a", "b"])
    data = state.model_dump_json()
    state2 = AgentState.model_validate_json(data)
    assert state2.plan == state.plan


def test_make_plan_tool_through_registry() -> None:
    registry = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    msg = registry.dispatch(
        state,
        ToolCall(
            id="c1",
            name="make_plan",
            arguments={"goal": "build", "steps": ["design", "code", "test"]},
        ),
    )
    assert msg is not None and msg.role == "tool"
    assert "plan set" in msg.content
    assert state.plan is not None
    assert len(state.plan.steps) == 3

    msg2 = registry.dispatch(
        state,
        ToolCall(id="c2", name="update_plan", arguments={"step_id": "step_1", "status": "done"}),
    )
    assert msg2 is not None and "step_1 -> done" in msg2.content


def test_make_plan_tool_validates_input() -> None:
    registry = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    for args in (
        {"goal": "", "steps": ["x"]},
        {"goal": "g", "steps": []},
        {"goal": "g", "steps": ["ok", 5]},
    ):
        msg = registry.dispatch(state, ToolCall(id="c", name="make_plan", arguments=args))
        assert msg is not None
        assert msg.content.startswith("error")


def test_update_plan_tool_reports_errors() -> None:
    registry = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    msg = registry.dispatch(
        state, ToolCall(id="c", name="update_plan", arguments={"step_id": "step_1", "status": "done"})
    )
    assert msg is not None and "no plan in progress" in msg.content


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
    print(f"\nAll {len(tests)} planner tests passed.")


if __name__ == "__main__":
    main()