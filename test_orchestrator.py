"""Orchestrator unit tests: message mapping + tool rendering (no server needed).

Uses a stub OpenAI client so the suite never touches the network. Imported
directly from the module under test; mirrors the other test files' style.
"""

from __future__ import annotations

import json

from core import AgentState, ChatMessage, ToolCall, ToolRegistry, user_message
from core.tool_registry import ToolRegistry as TR

import tools  # noqa: F401  (import side-effect: registers nothing)
from windows.orchestrator import (
    _from_openai_message,
    _to_openai_message,
    _to_openai_tools,
    default_registry,
)


class FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, id: str, name: str, arguments: str) -> None:
        self.id = id
        self.type = "function"
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content: str | None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


def test_to_openai_plain_user() -> None:
    msg = _to_openai_message(ChatMessage(role="user", content="hi"))
    assert msg == {"role": "user", "content": "hi"}


def test_to_openai_assistant_with_tool_calls() -> None:
    msg = _to_openai_message(
        ChatMessage(
            role="assistant",
            content="",
            function_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})],
        )
    )
    assert msg["role"] == "assistant"
    assert msg["tool_calls"][0]["function"] == {
        "name": "echo",
        "arguments": json.dumps({"text": "x"}),
    }


def test_to_openai_tool_result() -> None:
    msg = _to_openai_message(
        ChatMessage(role="tool", tool_call_id="c1", content="result!")
    )
    assert msg == {"role": "tool", "tool_call_id": "c1", "content": "result!"}


def test_from_openai_plain() -> None:
    msg = _from_openai_message(FakeMessage("hello"))
    assert msg.role == "assistant"
    assert msg.content == "hello"
    assert msg.function_calls is None


def test_from_openai_with_tool_calls() -> None:
    msg = _from_openai_message(
        FakeMessage("", [FakeToolCall("c1", "echo", '{"text": "hi"}')])
    )
    assert msg.function_calls == [ToolCall(id="c1", name="echo", arguments={"text": "hi"})]


def test_from_openai_malformed_arguments_degrades() -> None:
    msg = _from_openai_message(
        FakeMessage("", [FakeToolCall("c1", "echo", "not-json")])
    )
    assert msg.function_calls[0].arguments == {"raw": "not-json"}


def test_to_openai_tools_wraps_definitions() -> None:
    registry: TR = default_registry()
    names = {tool["function"]["name"] for tool in _to_openai_tools(registry)}
    assert {"run_code", "create_docx", "create_pptx"} <= names
    run_code = next(
        t for t in _to_openai_tools(registry) if t["function"]["name"] == "run_code"
    )
    assert "parameters" in run_code["function"]
    assert "requires_approval" not in run_code


def test_registry_includes_web_search_and_excludes_email() -> None:
    registry: TR = default_registry()
    assert registry.definition("web_search") is not None
    assert registry.definition("fetch_url") is not None
    # send_email/send_message remain opt-in networked (MCP_BASE_URL)
    assert registry.definition("send_email") is None
    assert registry.definition("send_message") is None


def test_provider_renders_messages_in_order() -> None:
    from windows.orchestrator import LlamaCppProvider

    class FakeCompletions:
        def __init__(self, client) -> None:
            self.client = client

        def create(self, **kwargs):
            self.client.calls.append(kwargs)
            return FakeResponse()

    class FakeChoice:
        def __init__(self, message) -> None:
            self.message = message

    class FakeResponse:
        usage = None
        choices = [FakeChoice(FakeMessage("stub"))]

    class FakeChat:
        def __init__(self, client) -> None:
            self.completions = FakeCompletions(client)

    class FakeClient:
        def __init__(self) -> None:
            self.calls = []
            self.chat = FakeChat(self)

    registry: TR = default_registry()
    provider = LlamaCppProvider(registry=registry)
    provider.client = FakeClient()
    state = AgentState(
        target="windows",
        model="LFM2.5-1.2B-Instruct",
        messages=[
            user_message("ping"),
            ChatMessage(role="tool", tool_call_id="c1", content="pong"),
        ],
    )
    provider(state)
    request = provider.client.calls[0]
    assert request["messages"][-1]["role"] == "tool"
    assert request["messages"][-1]["tool_call_id"] == "c1"
    assert request["model"] == "LFM2.5-1.2B-Instruct"
    assert request["extra_body"]["top_k"] == 50


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
    print(f"\nAll {len(tests) - failures}/{len(tests)} orchestrator smoke tests passed.")
    sys.exit(1 if failures else 0)
