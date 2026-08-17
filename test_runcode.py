"""Phase 3 smoke test: run_code Docker sandbox.

Uses a fake docker client so the suite needs no daemon. Verifies the handler's
sandbox flags, exit-code handling, timeout path, and the approval gate wired to
the real handler (execute only on approve, never on reject).
Runnable directly or via pytest.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from core import (
    AgentState,
    ChatMessage,
    PendingApprovalError,
    ToolCall,
    ToolRegistry,
    new_state,
    resolve_approval,
    step,
    user_message,
)
from tools import register_runcode_tool


class FakeContainer:
    def __init__(self, status_code: int = 0, logs: str = "hello from sandbox", block_wait: bool = False):
        self.status_code = status_code
        self.output = logs
        self.block_wait = block_wait
        self.removed = False
        self.killed = False

    def wait(self, timeout: int | None = None) -> dict:
        if self.block_wait:
            time.sleep(60)  # hang until the watchdog kills us
        return {"StatusCode": self.status_code}

    def kill(self) -> None:
        self.killed = True

    def logs(self, stdout: bool = True, stderr: bool = True) -> bytes:
        return self.output.encode("utf-8")

    def remove(self, force: bool = True) -> None:
        self.removed = True


class FakeContainers:
    def __init__(self) -> None:
        self.run_kwargs: list[tuple[str, dict[str, Any]]] = []
        self.containers: list[FakeContainer] = []
        self.next_status = 0
        self.next_logs = ""
        self.next_block_wait = False

    def run(self, image: str, **kwargs: Any) -> FakeContainer:
        self.run_kwargs.append((image, kwargs))
        container = FakeContainer(self.next_status, self.next_logs, self.next_block_wait)
        self.containers.append(container)
        return container


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()


def _new_state() -> AgentState:
    return new_state(target="windows", model="LFM2.5-1.2B-Instruct")


def _registry(client: FakeDockerClient) -> ToolRegistry:
    registry = ToolRegistry()
    register_runcode_tool(registry, docker_client=client)
    return registry


def _run_code_call(**overrides: Any) -> ToolCall:
    args = {"language": "python", "code": "print(1)", "timeout_seconds": 5}
    args.update(overrides)
    return ToolCall(id="call_0001", name="run_code", arguments=args)


def test_sandbox_flags_are_applied() -> None:
    client = FakeDockerClient()
    client.containers.next_logs = "ran"
    registry = _registry(client)

    msg = registry.execute(_run_code_call())
    assert msg is not None and msg.role == "tool"

    image, kwargs = client.containers.run_kwargs[0]
    assert image == "python:3.12-slim"
    assert kwargs["network_disabled"] is True
    assert kwargs["mem_limit"] == "256m"
    assert kwargs["nano_cpus"] == int(0.5 * 1e9)
    assert kwargs["detach"] is True
    assert client.containers.containers[0].removed is True


def test_run_code_returns_output() -> None:
    client = FakeDockerClient()
    client.containers.next_logs = "hello from sandbox"
    registry = _registry(client)

    msg = registry.execute(_run_code_call())
    assert msg is not None and msg.role == "tool"
    assert msg.content == "hello from sandbox"


def test_run_code_nonzero_exit() -> None:
    client = FakeDockerClient()
    client.containers.next_status = 1
    client.containers.next_logs = "boom"
    registry = _registry(client)

    msg = registry.execute(_run_code_call())
    assert msg is not None
    assert msg.content.startswith("exit code 1")
    assert "boom" in msg.content


def test_run_code_timeout_surfaces() -> None:
    client = FakeDockerClient()
    client.containers.next_block_wait = True
    registry = _registry(client)

    msg = registry.execute(_run_code_call(timeout_seconds=1))
    assert msg is not None and msg.role == "tool"
    assert "timed out" in msg.content
    assert client.containers.containers[0].killed is True
    assert client.containers.containers[0].removed is True


def test_run_code_unsupported_language() -> None:
    client = FakeDockerClient()
    registry = _registry(client)

    msg = registry.execute(_run_code_call(language="cobol"))
    assert msg is not None
    assert "invalid arguments" in msg.content
    assert client.containers.run_kwargs == []


def test_approval_gate_halts_and_reject_never_runs() -> None:
    client = FakeDockerClient()
    registry = _registry(client)
    state = _new_state()
    state.messages.append(user_message("run something"))

    def provider(s: AgentState) -> ChatMessage:
        return ChatMessage(role="assistant", content="", function_calls=[_run_code_call()])

    step(state, provider, registry)
    assert state.pending_approval is not None
    assert client.containers.run_kwargs == []

    try:
        step(state, provider, registry)
    except PendingApprovalError:
        pass
    else:
        raise AssertionError("step() must refuse while approval is pending")

    resolve_approval(state, registry, approved=False)
    assert state.pending_approval is None
    assert client.containers.run_kwargs == []


def test_approval_accept_runs_sandbox() -> None:
    client = FakeDockerClient()
    client.containers.next_logs = "ran"
    registry = _registry(client)
    state = _new_state()
    state.messages.append(user_message("run something"))

    def provider(s: AgentState) -> ChatMessage:
        return ChatMessage(role="assistant", content="", function_calls=[_run_code_call()])

    step(state, provider, registry)
    assert state.pending_approval is not None
    resolve_approval(state, registry, approved=True)
    assert state.pending_approval is None
    assert len(client.containers.run_kwargs) == 1
    assert state.messages[-1].content == "ran"


def test_registry_definition_loaded() -> None:
    client = FakeDockerClient()
    registry = _registry(client)
    names = {d.name for d in registry.definitions()}
    assert names == {"run_code"}
    assert registry.definition("run_code").requires_approval is True


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
    print(f"\nAll {len(tests)} run_code smoke tests passed.")


if __name__ == "__main__":
    main()