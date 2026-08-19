"""Planning & task tracking on ``AgentState.plan``.

Stdlib-only and transport-agnostic (the JS port is ``web/planner.js``).
Plan steps are bookkeeping only: every step that calls a tool still hits the
hardcoded approval gate in ``core/tool_registry.dispatch()`` independently --
there is no plan-based approval bypass.
"""

from __future__ import annotations

from typing import Optional

from .state import Plan, PlanStep

VALID_STATUSES = ("pending", "in_progress", "done", "failed", "skipped")


def make_plan(state, goal: str, steps: list[str]) -> Plan:
    """Set ``state.plan`` from a goal and an ordered list of step descriptions."""
    plan = Plan(
        goal=goal.strip(),
        steps=[
            PlanStep(id=f"step_{i + 1}", description=desc.strip())
            for i, desc in enumerate(steps)
        ],
    )
    state.plan = plan
    return plan


def update_plan(
    state,
    step_id: str,
    status: str,
    result: Optional[str] = None,
) -> PlanStep:
    """Update one step's status (and optionally its result). Raises on bad input."""
    if state.plan is None:
        raise ValueError("no plan in progress; call make_plan first")
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid step status: {status!r} (expected one of {VALID_STATUSES})")
    for step in state.plan.steps:
        if step.id == step_id:
            step.status = status  # type: ignore[assignment]
            if result is not None:
                step.result = result
            return step
    raise ValueError(f"no plan step with id {step_id!r}")