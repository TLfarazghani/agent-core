"""Phase 8: shared slash-command registry (core/commands.py).

Runnable directly or via pytest; no live model, no Docker. Proves the same
table backs both UIs (the web server calls ``run_command`` directly) and that
commands never bypass the approval gate.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core import (
    COMMANDS,
    AgentState,
    ChatMessage,
    CommandContext,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    help_text,
    is_command,
    new_state,
    parse_command,
    run_command,
)
from core.sessions import save_session


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo text.",
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
            name="risky",
            description="Needs approval.",
            requires_approval=True,
            parameters={
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        ),
        lambda arguments: "risky ran",
    )
    return registry


def make_ctx(state: AgentState | None = None, registry: ToolRegistry | None = None, **kwargs) -> CommandContext:
    state = state or new_state(target="windows", model="test-model")
    return CommandContext(
        state=state,
        registry=registry or make_registry(),
        max_context_tokens=kwargs.pop("max_context_tokens", 4096),
        **kwargs,
    )


def test_registry_shape() -> None:
    for name, command in COMMANDS.items():
        assert name == command.name
        assert name.startswith("/")
        assert command.description
        assert command.usage.startswith(name)
    assert "/help" in COMMANDS and "/mode" in COMMANDS and "/quit" in COMMANDS
    assert help_text().startswith("  /help")


def test_parse_and_is_command() -> None:
    assert is_command("/turns 3")
    assert not is_command("hello")
    assert parse_command("/turns 3") == ("/turns", "3")
    assert parse_command("/help") == ("/help", "")
    assert parse_command("  /mode   plan  ") == ("/mode", "plan")


def test_help_lists_every_command() -> None:
    text = run_command(make_ctx(), "/help").text
    for name in COMMANDS:
        assert name in text


def test_unknown_command_is_an_error_not_a_crash() -> None:
    result = run_command(make_ctx(), "/nope")
    assert result.error
    assert "Unknown command" in result.text


def test_mode_get_and_set() -> None:
    ctx = make_ctx()
    assert ctx.state.permission_mode == "default"
    assert "default*" in run_command(ctx, "/mode").text
    set_result = run_command(ctx, "/mode plan")
    assert not set_result.error
    assert ctx.state.permission_mode == "plan"
    assert set_result.save
    bad = run_command(ctx, "/mode yolo")
    assert bad.error
    assert ctx.state.permission_mode == "plan"


def test_turns_sets_budget_and_rejects_bad_values() -> None:
    ctx = make_ctx()
    assert run_command(ctx, "/turns 3").error is False
    assert ctx.state.max_turns == 3
    assert run_command(ctx, "/turns abc").error
    assert run_command(ctx, "/turns 0").error
    assert ctx.state.max_turns == 3


def test_status_and_context_report_live_values() -> None:
    ctx = make_ctx(max_context_tokens=1000)
    ctx.state.messages.append(ChatMessage(role="user", content="hello there"))
    status = run_command(ctx, "/status").text
    assert "Agent Core" in status
    context = run_command(ctx, "/context").text
    assert "1000" in context
    assert "mode: default" in context


def test_cost_reports_session_tokens() -> None:
    """Phase 12: /cost folds state.usage (server-reported tokens)."""
    from core.telemetry import record_usage

    ctx = make_ctx()
    empty = run_command(ctx, "/cost")
    assert empty.error is False
    assert "No usage recorded yet" in empty.text

    record_usage(
        ctx.state,
        {"prompt_tokens": 2014, "completion_tokens": 412, "total_tokens": 2426},
        latency_s=None,
    )
    populated = run_command(ctx, "/cost")
    assert "2,426" in populated.text
    assert "$0.00" in populated.text and "local" in populated.text
    assert "/cost" in run_command(ctx, "/help").text


def test_context_shows_real_usage_line() -> None:
    ctx = make_ctx(max_context_tokens=4096)
    base = run_command(ctx, "/context").text
    assert "Real usage" not in base
    from core.telemetry import record_usage

    record_usage(ctx.state, {"prompt_tokens": 512, "completion_tokens": 64, "total_tokens": 576}, 0.8)
    with_usage = run_command(ctx, "/context").text
    assert "Real usage (server-reported)" in with_usage
    assert "576" in with_usage and "tok/s" in with_usage


def test_doctor_with_injected_probes() -> None:
    """Phase 12: /doctor runs the injectable checks — no network, no Docker.

    The real dir-writability probes still run, pointed at tempdirs.
    """
    with tempfile.TemporaryDirectory() as tmp:
        from core.telemetry import check_dir_writable

        ctx = make_ctx(
            doctor_probes={
                "llama_server": lambda: {"ok": True, "detail": "GET /health -> 200"},
                "model_match": lambda: {"ok": True, "detail": "same model"},
                "docker": lambda: {"ok": True, "detail": "daemon reachable"},
                "session_dir": lambda: check_dir_writable(Path(tmp)),
                "memory_dir": lambda: check_dir_writable(Path(tmp)),
            },
            session_dir=Path(tmp),
            memory_dir=Path(tmp),
        )
        ok = run_command(ctx, "/doctor")
    assert ok.error is False
    assert "ALL CHECKS PASSED" in ok.text
    assert "llama_server" in ok.text and "session_dir" in ok.text


def test_doctor_reports_failures() -> None:
    ctx = make_ctx(doctor_probes={
        "llama_server": lambda: {"ok": False, "detail": "llama-server unreachable", "hint": "start server"},
        "model_match": lambda: {"ok": True, "detail": "same model"},
        "docker": lambda: {"ok": False, "detail": "docker CLI not found"},
    })
    bad = run_command(ctx, "/doctor")
    assert "FAIL" in bad.text and "Overall: 2 problem(s) found" in bad.text
    assert "hint: start server" in bad.text


def test_plan_renders_steps() -> None:
    from core import planner_make_plan

    ctx = make_ctx()
    assert "No plan" in run_command(ctx, "/plan").text
    planner_make_plan(ctx.state, "ship phase 8", ["write core", "write tests"])
    text = run_command(ctx, "/plan").text
    assert "ship phase 8" in text
    assert "write core" in text and "pending" in text


def test_tools_marks_approval_and_read_only() -> None:
    text = run_command(make_ctx(), "/tools").text
    assert "echo" in text and "read-only" in text
    assert "risky" in text and "approval" in text


def test_new_keeps_mode_and_turns_and_resets_history() -> None:
    ctx = make_ctx()
    ctx.state.permission_mode = "plan"
    ctx.state.max_turns = 4
    ctx.state.messages.append(ChatMessage(role="user", content="old"))
    result = run_command(ctx, "/new")
    assert result.clear and result.save and result.state is not None
    assert result.state.session_id != ctx.state.session_id
    assert result.state.permission_mode == "plan"
    assert result.state.max_turns == 4
    assert all(m.content != "old" for m in result.state.messages)


def test_resume_loads_saved_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp)
        saved = new_state(target="windows", model="test-model")
        saved.permission_mode = "strict"
        saved.messages.append(ChatMessage(role="user", content="remember me"))
        save_session(saved, session_dir)
        ctx = make_ctx(session_dir=session_dir)
        result = run_command(ctx, f"/resume {saved.session_id}")
        assert not result.error
        assert result.state is not None
        assert result.state.session_id == saved.session_id
        assert result.state.permission_mode == "strict"
        assert any(m.content == "remember me" for m in result.state.messages)
        missing = run_command(ctx, "/resume does-not-exist")
        assert missing.error


def test_memory_command_reads_store() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        from core import save_memory

        memory_dir = Path(tmp)
        save_memory("favourite", "blue", memory_dir=memory_dir)
        ctx = make_ctx(memory_dir=memory_dir)
        text = run_command(ctx, "/memory favourite").text
        assert "blue" in text
        assert "No memories" in run_command(ctx, "/memory nothing-here").text


def test_approve_and_reject_use_the_gate() -> None:
    registry = make_registry()
    ctx = make_ctx(registry=registry)
    parked = registry.dispatch(ctx.state, ToolCall(id="c1", name="risky", arguments={"code": "x"}))
    assert parked is None
    assert ctx.state.pending_approval is not None

    approved = run_command(ctx, "/approve")
    assert not approved.error
    assert ctx.state.pending_approval is None
    assert ctx.state.messages[-1].content == "risky ran"

    assert "No pending approval" in run_command(ctx, "/reject").text

    registry.dispatch(ctx.state, ToolCall(id="c2", name="risky", arguments={"code": "y"}))
    assert ctx.state.pending_approval is not None
    rejected = run_command(ctx, "/reject")
    assert not rejected.error
    assert ctx.state.pending_approval is None
    assert "did not run" in rejected.text


def test_quit_requests_save_and_exit() -> None:
    result = run_command(make_ctx(), "/quit")
    assert result.quit and result.save


def test_handler_errors_are_contained() -> None:
    class Broken:
        def definitions(self):
            raise RuntimeError("boom")

    ctx = make_ctx(registry=Broken())  # type: ignore[arg-type]
    result = run_command(ctx, "/tools")
    assert result.error
    assert "Command failed" in result.text


if __name__ == "__main__":
    tests = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS  {test.__name__}")
    print(f"\nAll {len(tests)} command tests passed.")
