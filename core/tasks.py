"""Phase 11: lightweight session task backlog.

Distinct from ``core.planner`` (a single ordered procedure for the current
request): tasks are durable items that persist with the session and survive
resumes, mirroring Claude Code's ``tasks`` collection.

Stdlib-only and transport-agnostic (the JS port is ``web/tasks.js``). Tasks are
bookkeeping only -- every tool call a task leads to still hits the hardcoded
approval gate in ``core.tool_registry.dispatch()``.
"""

from __future__ import annotations

from typing import Optional

from .state import AgentState, Task

VALID_TASK_STATUSES = ("pending", "in_progress", "done", "failed", "skipped")


def _next_id(state: AgentState) -> str:
    highest = 0
    for task in state.tasks:
        digits = task.id.removeprefix("task_")
        if digits.isdigit():
            highest = max(highest, int(digits))
    return f"task_{highest + 1}"


def create_task(state: AgentState, description: str) -> Task:
    description = (description or "").strip()
    if not description:
        raise ValueError("task description must be a non-empty string")
    task = Task(id=_next_id(state), description=description)
    state.tasks.append(task)
    return task


def update_task(
    state: AgentState,
    task_id: str,
    status: str,
    result: Optional[str] = None,
) -> Task:
    if status not in VALID_TASK_STATUSES:
        raise ValueError(f"invalid task status: {status!r}")
    for task in state.tasks:
        if task.id == task_id:
            task.status = status  # type: ignore[assignment]
            if result is not None:
                task.result = result
            return task
    raise ValueError(f"no task with id {task_id!r}")


def list_tasks(state: AgentState) -> list[Task]:
    return list(state.tasks)


def render_tasks(state: AgentState) -> str:
    if not state.tasks:
        return "No tasks."
    markers = {
        "done": "✓",
        "in_progress": "▶",
        "failed": "✗",
        "pending": "·",
        "skipped": "−",
    }
    lines = ["Tasks:"]
    for task in state.tasks:
        suffix = f" — {task.result}" if task.result else ""
        lines.append(
            f"  {markers.get(task.status, '·')} {task.id} {task.description} "
            f"[{task.status}]{suffix}"
        )
    return "\n".join(lines)
