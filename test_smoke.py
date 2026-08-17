"""Phase 0 smoke test for the agent core.

Runnable directly (``python test_smoke.py``) or via pytest.
Proves: ordinary tool calls execute, ``run_code`` halts until approval,
and the rejection path clears state without executing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core import (
    AgentState,
    ChatMessage,
    MaxTurnsError,
    ParserError,
    PendingApprovalError,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    new_state,
    parse_tool_calls,
    parse_tool_calls_strict,
    resolve_approval,
    run,
    step,
    user_message,
)

REGISTRY_JSON = [
    {
        "name": "echo",
        "description": "Return the given text.",
        "requires_approval": False,
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "run_code",
        "description": "Execute arbitrary code.",
        "requires_approval": True,
        "parameters": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "enum": ["python", "javascript", "bash"]},
                "code": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
            },
            "required": ["language", "code"],
        },
    },
]


@dataclass
class FakeProvider:
    responses: list[ChatMessage] = field(default_factory=list)

    def __call__(self, state: AgentState) -> ChatMessage:
        if not self.responses:
            raise AssertionError("FakeProvider ran out of scripted responses")
        return self.responses.pop(0)


@dataclass
class CountingHandler:
    calls: int = 0

    def echo(self, arguments: dict) -> str:
        self.calls += 1
        return f"echo: {arguments['text']}"

    def run_code(self, arguments: dict) -> str:
        self.calls += 1
        return f"ran: {arguments['code']}"


def make_registry() -> tuple[ToolRegistry, CountingHandler]:
    registry = ToolRegistry()
    counter = CountingHandler()
    registry.load_json("tools/registry.json", {"echo": counter.echo, "run_code": counter.run_code})
    return registry, counter


def test_parser_basic() -> None:
    calls = parse_tool_calls('<|tool_call_start|>echo(text="hello")<|tool_call_end|>')
    assert calls == [{"id": "call_0001", "name": "echo", "arguments": {"text": "hello"}}]


def test_parser_multiple_calls() -> None:
    text = (
        '<|tool_call_start|>echo(text="a")<|tool_call_end|>'
        '<|tool_call_start|>echo(text="b")<|tool_call_end|>'
    )
    calls = parse_tool_calls(text)
    assert [c["name"] for c in calls] == ["echo", "echo"]
    assert [c["arguments"]["text"] for c in calls] == ["a", "b"]


def test_parser_no_blocks() -> None:
    assert parse_tool_calls("plain answer, no tool call") == []


def test_parser_malformed_lenient_and_strict() -> None:
    bad = "<|tool_call_start|>this is not python(<<<)<|tool_call_end|>"
    assert parse_tool_calls(bad) == []
    try:
        parse_tool_calls_strict(bad)
    except ParserError:
        pass
    else:
        raise AssertionError("strict parser should raise ParserError")


def test_registry_rejects_bad_definition() -> None:
    registry = ToolRegistry()
    bad = {
        "name": "nope",
        "description": "missing parameters",
    }
    try:
        registry.register(bad, lambda args: "")
    except ValueError:
        pass
    else:
        raise AssertionError("bad tool definition should be rejected")


def test_registry_unknown_tool() -> None:
    registry, _ = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    msg = registry.dispatch(state, ToolCall(id="call_0001", name="ghost", arguments={}))
    assert msg is not None
    assert msg.role == "tool"
    assert "unknown tool" in msg.content


def test_ordinary_tool_executes() -> None:
    registry, counter = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    state.messages.append(user_message("echo hello"))

    def provider(s: AgentState) -> ChatMessage:
        return ChatMessage(
            role="assistant",
            content="",
            function_calls=[ToolCall(id="call_0001", name="echo", arguments={"text": "hello"})],
        )

    step(state, provider, registry)
    assert counter.calls == 1
    assert state.turn_count == 1
    last = state.messages[-1]
    assert last.role == "tool"
    assert last.content == "echo: hello"


def test_run_code_halts_until_approval() -> None:
    registry, counter = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    state.messages.append(user_message("run something"))

    def provider(s: AgentState) -> ChatMessage:
        return ChatMessage(
            role="assistant",
            content="",
            function_calls=[
                ToolCall(
                    id="call_0001",
                    name="run_code",
                    arguments={"language": "python", "code": "print(1)", "timeout_seconds": 5},
                )
            ],
        )

    step(state, provider, registry)
    assert state.pending_approval is not None
    assert state.pending_approval.tool_name == "run_code"
    assert counter.calls == 0

    try:
        step(state, provider, registry)
    except PendingApprovalError:
        pass
    else:
        raise AssertionError("step() must refuse while approval is pending")


def test_approval_rejection_clears_without_executing() -> None:
    registry, counter = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    state.pending_approval = None
    resolve_approval(state, registry, approved=False)
    assert state.pending_approval is None
    assert counter.calls == 0


def test_approval_accept_runs_sandbox() -> None:
    registry, counter = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")

    def provider(s: AgentState) -> ChatMessage:
        return ChatMessage(
            role="assistant",
            content="",
            function_calls=[
                ToolCall(
                    id="call_0001",
                    name="run_code",
                    arguments={"language": "python", "code": "print(1)", "timeout_seconds": 5},
                )
            ],
        )

    step(state, provider, registry)
    assert state.pending_approval is not None
    resolve_approval(state, registry, approved=True)
    assert state.pending_approval is None
    assert counter.calls == 1
    assert state.messages[-1].content == "ran: print(1)"


def test_run_driver_terminates() -> None:
    registry, counter = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    state.messages.append(user_message("echo and stop"))

    provider = FakeProvider(
        [
            ChatMessage(
                role="assistant",
                content="",
                function_calls=[ToolCall(id="call_0001", name="echo", arguments={"text": "a"})],
            ),
            ChatMessage(role="assistant", content="done."),
        ]
    )

    run(state, provider, registry)
    assert counter.calls == 1
    assert state.messages[-1].content == "done."
    assert state.turn_count == 2


def test_max_turns_enforced() -> None:
    registry, _ = make_registry()
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct", max_turns=1)
    state.messages.append(user_message("hi"))

    def provider(s: AgentState) -> ChatMessage:
        return ChatMessage(role="assistant", content="still looping.")

    step(state, provider, registry)
    try:
        step(state, provider, registry)
    except MaxTurnsError:
        pass
    else:
        raise AssertionError("step() must refuse past max_turns")


def test_definitions_load() -> None:
    registry, _ = make_registry()
    names = {d.name for d in registry.definitions()}
    assert names == {"echo", "run_code"}


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
    print(f"\nAll {len(tests)} smoke tests passed.")


if __name__ == "__main__":
    main()