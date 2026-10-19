"""Phase 11 smoke test: the task backlog (core/tasks.py + task tools).

Self-contained: tempfile session dir, no live deps. Proves ids, statuses,
rendering, the state-aware tools, and that tasks serialize with the session.
Runnable directly or via pytest.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core import ChatMessage, ToolCall, ToolRegistry, new_state, step
from core.sessions import load_session, save_session
from core.tasks import create_task, list_tasks, render_tasks, update_task
from tools import register_task_tools


def _state():
    return new_state(target="windows", model="test-model")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_task_tools(registry)
    return registry


def test_create_and_update_round_trip() -> None:
    state = _state()
    first = create_task(state, "first task")
    assert first.id == "task_1"
    assert first.status == "pending"
    assert create_task(state, "second task").id == "task_2"
    updated = update_task(state, "task_1", "done", "finished it")
    assert updated.status == "done"
    assert updated.result == "finished it"
    assert list_tasks(state) == state.tasks


def test_invalid_status_and_missing_id_raise() -> None:
    state = _state()
    create_task(state, "t")
    try:
        update_task(state, "task_1", "yolo")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        update_task(state, "task_9", "done")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_empty_description_rejected() -> None:
    try:
        create_task(_state(), "   ")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_render_lists_tasks() -> None:
    state = _state()
    assert render_tasks(state) == "No tasks."
    create_task(state, "write docs")
    update_task(state, "task_1", "in_progress")
    text = render_tasks(state)
    assert "task_1" in text and "write docs" in text and "[in_progress]" in text


def test_task_tools_dispatch_statefully() -> None:
    registry = _registry()
    state = _state()
    created = registry.dispatch(
        state, ToolCall(id="c1", name="task_create", arguments={"description": "ship it"})
    )
    assert created is not None
    assert "task_1" in created.content

    updated = registry.dispatch(
        state,
        ToolCall(id="c2", name="task_update", arguments={"id": "task_1", "status": "done"}),
    )
    assert updated is not None
    assert updated.content == "task task_1 -> done"

    listed = registry.dispatch(state, ToolCall(id="c3", name="task_list", arguments={}))
    assert listed is not None
    assert "task_1" in listed.content


def test_task_tools_never_require_approval() -> None:
    registry = _registry()
    state = _state()
    for name, args in (
        ("task_create", {"description": "x"}),
        ("task_update", {"id": "task_1", "status": "done"}),
        ("task_list", {}),
    ):
        if name == "task_list":
            create_task(state, "x")
        registry.dispatch(state, ToolCall(id="c", name=name, arguments=args))
        assert state.pending_approval is None, name


def test_tasks_persist_with_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp)
        state = _state()
        create_task(state, "persisted task")
        update_task(state, "task_1", "done", "ok")
        save_session(state, session_dir)

        reloaded = load_session(state.session_id, session_dir)
        assert len(reloaded.tasks) == 1
        assert reloaded.tasks[0].description == "persisted task"
        assert reloaded.tasks[0].status == "done"
        assert reloaded.tasks[0].result == "ok"


def test_tasks_survive_a_turn() -> None:
    state = _state()
    create_task(state, "stay put")
    state.messages.append(ChatMessage(role="user", content="hi"))
    step(state, lambda _s: ChatMessage(role="assistant", content="ok"), ToolRegistry())
    assert len(state.tasks) == 1


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
    print(f"\nAll {len(tests) - failures}/{len(tests)} task tests passed.")
    sys.exit(1 if failures else 0)
