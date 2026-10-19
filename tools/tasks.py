"""Phase 11 task tools: a lightweight, persistent task backlog.

Distinct from the single ``AgentState.plan``: ``task_create`` / ``task_update`` /
``task_list`` manage a durable backlog that serializes with the session and
survives resumes. Bookkeeping only -- a task's work still hits the approval gate.
"""

from __future__ import annotations

import json
from typing import Callable

from core import takes_state
from core import tasks as task_store

TASK_TOOLS = {"task_create", "task_update", "task_list"}


def make_task_create_handler() -> Callable:
    def handler(state, arguments: dict) -> str:
        try:
            task = task_store.create_task(state, arguments.get("description", ""))
        except ValueError as exc:
            return f"error: {exc}"
        return f"task created: {json.dumps(task.model_dump())}"

    return takes_state(handler)


def make_task_update_handler() -> Callable:
    def handler(state, arguments: dict) -> str:
        try:
            task = task_store.update_task(
                state,
                arguments["id"],
                arguments["status"],
                result=arguments.get("result"),
            )
        except (ValueError, KeyError) as exc:
            return f"error: {exc}"
        return f"task {task.id} -> {task.status}"

    return takes_state(handler)


def make_task_list_handler() -> Callable:
    def handler(state, arguments: dict) -> str:
        return task_store.render_tasks(state)

    return takes_state(handler)


def register_task_tools(registry) -> None:
    """Register ``task_create``, ``task_update``, ``task_list``."""
    handlers = {
        "task_create": make_task_create_handler(),
        "task_update": make_task_update_handler(),
        "task_list": make_task_list_handler(),
    }
    registry.load_json("tools/registry.json", handlers, names=TASK_TOOLS)
