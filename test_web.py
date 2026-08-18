"""Web server tests: self-contained, fake provider + fake docker, no llama-server.

Starts the real ``web.server`` HTTP server on an ephemeral port and drives it
with ``urllib``. The fake provider returns scripted ChatMessages, so the suite
exercises the actual HTTP + SSE + loop + approval-gate plumbing end to end.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from typing import Any

from core import ChatMessage, ToolCall, ToolDefinition, ToolRegistry

sys.path.insert(0, r"C:\hermes\agent-core\web")
from server import AgentApp, make_server  # noqa: E402


class FakeProvider:
    def __init__(self, script: list[ChatMessage]) -> None:
        self.script = script

    def __call__(self, state: Any) -> ChatMessage:
        return self.script.pop(0) if self.script else ChatMessage(role="assistant", content="(empty)")


class FakeDocker:
    def run(self, *args: Any, **kwargs: Any) -> Any:
        return None


def _build_registry() -> tuple[ToolRegistry, FakeDocker]:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="fake_tool",
            description="Return fake_tool ok.",
            requires_approval=False,
            parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        ),
        lambda arguments: f"fake result: {arguments['x']}",
    )
    registry.register(
        ToolDefinition(
            name="risky",
            description="A tool that needs approval.",
            requires_approval=True,
            parameters={"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
        ),
        lambda arguments: "risky ran",
    )
    return registry, FakeDocker()


class _TestServer:
    def __init__(self) -> None:
        self.registry, _ = _build_registry()
        self.server = make_server(AgentApp(registry=self.registry, health_check=lambda: True), 0)
        self.server.block_on_close = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._warm_up()

    def _warm_up(self) -> None:
        for _ in range(50):
            try:
                self.get("/api/health")
                return
            except (urllib.error.URLError, ConnectionError, OSError):
                import time

                time.sleep(0.02)
        raise RuntimeError("test server failed to start")

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path: str) -> Any:
        return _retry_transport(self._get, path)

    def _get(self, path: str) -> Any:
        with urllib.request.urlopen(self.url(path), timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def sse(self, path: str, body: dict) -> list[tuple[str, dict]]:
        return _retry_transport(self._sse, path, body)

    def _sse(self, path: str, body: dict) -> list[tuple[str, dict]]:
        request = urllib.request.Request(
            self.url(path),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        events: list[tuple[str, dict]] = []
        with urllib.request.urlopen(request, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            event = next(
                (l[7:] for l in block.splitlines() if l.startswith("event: ")), "message"
            )
            data = next(
                (l[6:] for l in block.splitlines() if l.startswith("data: ")), "{}"
            )
            events.append((event, json.loads(data)))
        return events


def _retry_transport(fn, *args):
    """Windows: rapid server start/stop can RST a fresh connection (WinError 10053).

    This is a test-harness socket teardown race, not a server bug. Retry once
    on transport-level aborts; HTTP-level errors still propagate.
    """
    try:
        return fn(*args)
    except (ConnectionAbortedError, ConnectionResetError, urllib.error.URLError):
        import time

        time.sleep(0.1)
        return fn(*args)


def _create_session(server: _TestServer) -> str:
    import urllib.request as ur

    def create():
        request = ur.Request(server.url("/api/sessions"), data=b"{}", method="POST")
        with ur.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))["session_id"]

    return _retry_transport(create)


def _events(server: _TestServer, path: str, body: dict | None = None) -> list[tuple[str, dict]]:
    return server.sse(path, body or {})


def test_health() -> None:
    server = _TestServer()
    try:
        assert server.get("/api/health")["ok"] is True
    finally:
        server.stop()


def test_static_serves_index() -> None:
    server = _TestServer()
    try:
        with urllib.request.urlopen(server.url("/"), timeout=10) as resp:
            html = resp.read().decode("utf-8")
        assert "<!DOCTYPE html>" in html
        assert "agent-core" in html
    finally:
        server.stop()


def test_static_serves_module_worker_assets() -> None:
    """The in-browser WebGPU path loads worker.js as a module worker, which
    then imports engine.js and parser.js -- all three must be served, or the
    worker fails with a silent load error."""
    server = _TestServer()
    try:
        for name in ("worker.js", "engine.js", "parser.js", "app.js"):
            with urllib.request.urlopen(server.url("/" + name), timeout=10) as resp:
                assert resp.status == 200, name
                assert "text/javascript" in resp.headers["Content-Type"], name
                body = resp.read().decode("utf-8")
                assert body.strip(), name
    finally:
        server.stop()


def test_models_route_forbids_traversal_and_404s_missing() -> None:
    """/models/ maps to the repo models/ dir for the in-browser model path;
    must not traverse out and must 404 when the ONNX model is absent."""
    server = _TestServer()
    try:
        try:
            urllib.request.urlopen(server.url("/models/../../../etc/passwd"), timeout=10)
            raise AssertionError("traversal should not succeed")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, exc.code
        try:
            urllib.request.urlopen(server.url("/models/LFM2.5-1.2B-Instruct-ONNX/onnx_model_q4.onnx"), timeout=10)
            raise AssertionError("missing model should 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, exc.code
    finally:
        server.stop()


def test_tools_listed() -> None:
    server = _TestServer()
    try:
        tools = server.get("/api/tools")["tools"]
        names = {t["name"] for t in tools}
        assert {"fake_tool", "risky"} <= names
    finally:
        server.stop()


def test_plain_chat_streams_done() -> None:
    server = _TestServer()
    try:
        app: AgentApp = server.server.app

        def factory(emit_token):
            return FakeProvider([ChatMessage(role="assistant", content="hello there")])

        app.provider_factory = factory
        session_id = _create_session(server)
        events = _events(server, f"/api/sessions/{session_id}/messages", {"message": "hi"})
        types = [e for e, _ in events]
        assert "done" in types
        done = next(d for e, d in events if e == "done")
        assert done["state"]["messages"][-1]["content"] == "hello there"
    finally:
        server.stop()


def test_tool_call_and_result_events() -> None:
    server = _TestServer()
    try:
        app: AgentApp = server.server.app

        def factory(emit_token):
            return FakeProvider(
                [
                    ChatMessage(
                        role="assistant",
                        content="",
                        function_calls=[ToolCall(id="c1", name="fake_tool", arguments={"x": "z"})],
                    ),
                    ChatMessage(role="assistant", content="all done"),
                ]
            )

        app.provider_factory = factory
        session_id = _create_session(server)
        events = _events(server, f"/api/sessions/{session_id}/messages", {"message": "go"})
        types = [e for e, _ in events]
        assert "tool_call" in types
        assert "tool_result" in types
        assert "done" in types
        tool_call = next(d for e, d in events if e == "tool_call")
        assert tool_call["name"] == "fake_tool"
        tool_result = next(d for e, d in events if e == "tool_result")
        assert tool_result["content"] == "fake result: z"
    finally:
        server.stop()


def _stateful_approval_provider_factory(emit_token):
    """Respond based on conversation history, like the real provider does."""

    def provider(state):
        if not any(
            m.role == "assistant" and m.function_calls for m in state.messages
        ):
            return ChatMessage(
                role="assistant",
                content="",
                function_calls=[ToolCall(id="c2", name="risky", arguments={"code": "boom"})],
            )
        return ChatMessage(role="assistant", content="understood")

    return provider


def test_approval_gate_reject_never_runs() -> None:
    server = _TestServer()
    try:
        app: AgentApp = server.server.app
        app.provider_factory = _stateful_approval_provider_factory
        session_id = _create_session(server)
        events = _events(server, f"/api/sessions/{session_id}/messages", {"message": "do it"})
        assert "approval" in [e for e, _ in events]
        approval = next(d for e, d in events if e == "approval")
        assert approval["tool_name"] == "risky"

        events = _events(server, f"/api/sessions/{session_id}/reject")
        types = [e for e, _ in events]
        assert "done" in types
        state = next(d for e, d in events if e == "done")["state"]
        assert all(m["role"] != "tool" for m in state["messages"])
        assert state["messages"][-1]["content"] == "understood"
    finally:
        server.stop()


def test_approval_gate_approve_runs_tool() -> None:
    server = _TestServer()
    try:
        app: AgentApp = server.server.app
        app.provider_factory = _stateful_approval_provider_factory
        session_id = _create_session(server)
        _events(server, f"/api/sessions/{session_id}/messages", {"message": "do it"})
        events = _events(server, f"/api/sessions/{session_id}/approve")
        types = [e for e, _ in events]
        assert "tool_result" in types
        tool_result = next(d for e, d in events if e == "tool_result")
        assert tool_result["content"] == "risky ran"
        done = next(d for e, d in events if e == "done")
        contents = [m["content"] for m in done["state"]["messages"]]
        assert "risky ran" in contents
        assert done["state"]["messages"][-1]["content"] == "understood"
    finally:
        server.stop()


def test_unknown_session_404() -> None:
    server = _TestServer()
    try:
        try:
            server.get("/api/sessions/nope-123")
            assert False, "expected 404"
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.stop()


def test_clear_resets_history_keeps_session() -> None:
    server = _TestServer()
    try:
        app: AgentApp = server.server.app

        def factory(emit_token):
            return FakeProvider([ChatMessage(role="assistant", content="first reply")])

        app.provider_factory = factory
        session_id = _create_session(server)
        _events(server, f"/api/sessions/{session_id}/messages", {"message": "hi"})

        def clear():
            request = urllib.request.Request(
                server.url(f"/api/sessions/{session_id}/clear"), data=b"{}", method="POST"
            )
            with urllib.request.urlopen(request, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))["state"]

        cleared = _retry_transport(clear)
        assert cleared["session_id"] == session_id
        assert cleared["messages"] == [] or all(m["role"] != "user" for m in cleared["messages"])

        assert server.get(f"/api/sessions/{session_id}")["state"]["session_id"] == session_id
    finally:
        server.stop()


def test_delete_session() -> None:
    server = _TestServer()
    try:
        session_id = _create_session(server)
        assert server.get(f"/api/sessions/{session_id}")["state"]["session_id"] == session_id

        def delete():
            request = urllib.request.Request(
                server.url(f"/api/sessions/{session_id}"), method="DELETE"
            )
            with urllib.request.urlopen(request, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))

        deleted = _retry_transport(delete)
        assert deleted["deleted"] is True
        try:
            server.get(f"/api/sessions/{session_id}")
            assert False, "expected 404 after delete"
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.stop()


def _start_web_server(registry: ToolRegistry) -> tuple[Any, threading.Thread]:
    server = make_server(AgentApp(registry=registry, health_check=lambda: True), 0)
    server.block_on_close = True
    server.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_api_search_proxy_returns_results() -> None:
    registry = ToolRegistry()
    from tools import register_web_tools

    class FakePage:
        def __init__(self, text: str) -> None:
            self._text = text.encode("utf-8")

        def read(self, *args: Any) -> bytes:
            return self._text

        def __enter__(self) -> "FakePage":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    class FakeUrlopen:
        def __call__(self, request: Any, timeout: int) -> FakePage:
            assert request.full_url.startswith("https://html.duckduckgo.com/html/")
            return FakePage(
                '<a class="result__a" href="https://example.com">Example</a>'
                '<a class="result__snippet" href="https://example.com">A snippet.</a>'
            )

    register_web_tools(registry, urlopen=FakeUrlopen())
    server, thread = _start_web_server(registry)
    try:
        payload = _retry_transport(
            _get_json, f"http://127.0.0.1:{server.port}/api/search?q=example"
        )
        assert "Example" in payload["result"]
        assert "A snippet." in payload["result"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_api_fetch_proxy_strips_html() -> None:
    registry = ToolRegistry()
    from tools import register_web_tools

    class FakePage:
        def __init__(self, text: str) -> None:
            self._text = text.encode("utf-8")

        def read(self, *args: Any) -> bytes:
            return self._text

        def __enter__(self) -> "FakePage":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    class FakeUrlopen:
        def __call__(self, request: Any, timeout: int) -> FakePage:
            return FakePage(
                "<html><body><h1>Hello</h1><p>world of web.</p></body></html>"
            )

    register_web_tools(registry, urlopen=FakeUrlopen())
    server, thread = _start_web_server(registry)
    try:
        payload = _retry_transport(
            _get_json, f"http://127.0.0.1:{server.port}/api/fetch?url=https://example.com"
        )
        assert "Hello world of web." in payload["result"]
        assert "<h1>" not in payload["result"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    failures = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - test runner
            failures += 1
            print(f"FAIL  {test.__name__}: {exc!r}")
    print(f"\nAll {len(tests) - failures}/{len(tests)} web smoke tests passed.")
    sys.exit(1 if failures else 0)
