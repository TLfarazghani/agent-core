"""Phase 1 smoke test: networked tools over a mocked MCP-remote.

Runs a stdlib HTTP server that speaks the MCP ``tools/call`` shape, registers
the networked tools against it, and verifies dispatch returns schema-valid
tool results. Runnable directly or via pytest.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from core import AgentState, ChatMessage, ToolCall, ToolRegistry, new_state, resolve_approval
from tools import register_networked_tools

REMOTE_TOOL_RESULTS = {
    "web_search": {
        "content": [
            {"type": "text", "text": "1. Liquid AI LFM2.5 — official docs\n2. LFM2.5 GGUF on Hugging Face"}
        ]
    },
    "send_email": {"content": [{"type": "text", "text": "email queued to a@example.com"}]},
    "send_message": {"content": [{"type": "text", "text": "message sent via telegram"}]},
}


class MockRemoteState:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.auth_seen: str | None = None
        self.fail_next = False
        # Misbehaving-remote mode: 401 EVEN when the presented key is valid,
        # echoing the received header — used to exercise client-side redaction.
        self.reject_valid_key = False


class MockMcpServer:
    def __init__(self, api_key: str | None = None) -> None:
        self.state = MockRemoteState()
        self._api_key = api_key
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length))
                server.state.calls.append(payload)
                auth = self.headers.get("Authorization")
                server.state.auth_seen = auth

                # Enforce the configured key like a real remote would: reject
                # with 401 + a detail that echoes the RECEIVED header, so the
                # client-side redaction path is exercised on the wire. The
                # expected key is never included in any response.
                # reject_valid_key simulates a misbehaving remote that 401s
                # even on a VALID key while echoing it back — the client must
                # scrub that echo before the detail reaches model context.
                if server._api_key and (
                    auth != f"Bearer {server._api_key}" or server.state.reject_valid_key
                ):
                    self._send(
                        401,
                        {
                            "jsonrpc": "2.0",
                            "id": payload.get("id"),
                            "error": {
                                "code": -32001,
                                "message": f"invalid api key (received: {auth!r})",
                            },
                        },
                    )
                    return

                if server.state.fail_next:
                    server.state.fail_next = False
                    self._send(200, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32000, "message": "simulated failure"}})
                    return

                name = payload["params"]["name"]
                result = REMOTE_TOOL_RESULTS.get(name, {"content": [{"type": "text", "text": f"unknown tool {name}"}]})
                self._send(200, {"jsonrpc": "2.0", "id": payload.get("id"), "result": result})

            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:  # silence request logging
                pass

        return Handler

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _new_state() -> AgentState:
    return new_state(target="windows", model="LFM2.5-1.2B-Instruct")


def _dispatch(registry: ToolRegistry, name: str, args: dict) -> ChatMessage:
    return registry.dispatch(_new_state(), ToolCall(id="call_0001", name=name, arguments=args))


def test_send_message_result_gated_then_received() -> None:
    """send_message is ALWAYS-confirm gated (2026-10-20): parks first, then the
    mock receives the tools/call only after an explicit approval."""
    server = MockMcpServer()
    server.start()
    try:
        registry = ToolRegistry()
        register_networked_tools(registry, base_url=f"http://127.0.0.1:{server.port}")
        state = _new_state()
        msg = registry.dispatch(state, ToolCall(id="call_0001", name="send_message", arguments={"channel": "telegram", "to": "me", "text": "hi"}))
        assert msg is None  # parked at the approval gate
        assert state.pending_approval.tool_name == "send_message"
        assert server.state.calls == []  # nothing dispatched before approval
        resolve_approval(state, registry, approved=True)
        assert server.state.calls[0]["method"] == "tools/call"
        assert server.state.calls[0]["params"]["name"] == "send_message"
        assert server.state.calls[0]["params"]["arguments"] == {"channel": "telegram", "to": "me", "text": "hi"}
        assert "message sent" in state.messages[-1].content
    finally:
        server.stop()


def test_web_search_no_longer_networked() -> None:
    """web_search is now a local tool (register_web_tools); it must NOT be
    registered by the MCP remote anymore."""
    server = MockMcpServer()
    server.start()
    try:
        registry = ToolRegistry()
        register_networked_tools(registry, base_url=f"http://127.0.0.1:{server.port}")
        names = {d.name for d in registry.definitions()}
        assert names == {"send_email", "send_message"}
    finally:
        server.stop()


def test_send_email_result() -> None:
    """MCP-transport send_email is approval-gated like the native one."""
    server = MockMcpServer()
    server.start()
    try:
        registry = ToolRegistry()
        register_networked_tools(registry, base_url=f"http://127.0.0.1:{server.port}")
        state = _new_state()
        msg = registry.dispatch(
            state,
            ToolCall(id="call_0001", name="send_email",
                     arguments={"to": "a@example.com", "subject": "hi", "body": "body", "attachments": ["x.pdf"]}),
        )
        assert msg is None  # parked at the approval gate
        assert state.pending_approval.tool_name == "send_email"
        assert len(server.state.calls) == 0  # nothing dispatched before approval
        resolve_approval(state, registry, approved=True)
        assert len(server.state.calls) == 1
        assert server.state.calls[0]["params"]["arguments"] == {
            "to": "a@example.com", "subject": "hi", "body": "body", "attachments": ["x.pdf"]
        }
        sent = state.messages[-1].content
        assert "email queued" in sent
    finally:
        server.stop()


def test_send_message_validates_channel() -> None:
    server = MockMcpServer()
    server.start()
    try:
        registry = ToolRegistry()
        register_networked_tools(registry, base_url=f"http://127.0.0.1:{server.port}")
        msg = _dispatch(registry, "send_message", {"channel": "carrier_pigeon", "to": "x", "text": "hi"})
        assert msg is not None and msg.role == "tool"
        assert "invalid arguments" in msg.content
        assert server.state.calls == []
    finally:
        server.stop()


def test_api_key_forwarded() -> None:
    server = MockMcpServer(api_key="secret-key")
    server.start()
    try:
        registry = ToolRegistry()
        register_networked_tools(registry, base_url=f"http://127.0.0.1:{server.port}", api_key="secret-key")
        state = _new_state()
        registry.dispatch(state, ToolCall(id="call_0001", name="send_message", arguments={"channel": "telegram", "to": "me", "text": "hi"}))
        resolve_approval(state, registry, approved=True)  # gate: external send needs human OK
        assert server.state.auth_seen == "Bearer secret-key"
    finally:
        server.stop()


def test_no_api_key_sends_no_authorization_header() -> None:
    """Unconfigured key = NO Authorization header at all — not an empty
    header, not a masked placeholder."""
    server = MockMcpServer()
    server.start()
    try:
        registry = ToolRegistry()
        register_networked_tools(registry, base_url=f"http://127.0.0.1:{server.port}")
        state = _new_state()
        registry.dispatch(state, ToolCall(id="call_0001", name="send_message", arguments={"channel": "telegram", "to": "me", "text": "hi"}))
        resolve_approval(state, registry, approved=True)  # gate: external send needs human OK
        assert server.state.auth_seen is None  # no Authorization header received
        assert server.state.calls[0]["params"]["name"] == "send_message"  # call still went through
        assert "message sent" in state.messages[-1].content
    finally:
        server.stop()


def test_api_key_redacted_from_error_detail() -> None:
    """A misbehaving remote that 401s on a VALID key while echoing the
    received Authorization header back must never leak the key into model
    context: the tool result carries '***' in its place."""
    server = MockMcpServer(api_key="secret-key")
    server.state.reject_valid_key = True
    server.start()
    try:
        registry = ToolRegistry()
        register_networked_tools(registry, base_url=f"http://127.0.0.1:{server.port}", api_key="secret-key")
        state = _new_state()
        registry.dispatch(state, ToolCall(id="call_0001", name="send_message", arguments={"channel": "telegram", "to": "me", "text": "hi"}))
        resolve_approval(state, registry, approved=True)  # gate: external send needs human OK
        assert server.state.auth_seen == "Bearer secret-key"  # the REAL key went out...
        result = state.messages[-1].content
        assert result.startswith("error:")  # surfaced as a tool error...
        assert "401" in result  # ...with the status visible...
        assert "***" in result  # ...the echo scrubbed to a placeholder...
        assert "secret-key" not in result  # ...and the key itself never present
    finally:
        server.stop()


def test_remote_failure_surfaces_as_tool_error() -> None:
    """A JSON-RPC-level failure from the remote surfaces as a tool error.
    Exercised end-to-end through the approval gate with send_message, since
    web_search is a local tool now and would never reach the server."""
    server = MockMcpServer()
    server.start()
    try:
        registry = ToolRegistry()
        register_networked_tools(registry, base_url=f"http://127.0.0.1:{server.port}")
        server.state.fail_next = True
        state = _new_state()
        registry.dispatch(state, ToolCall(id="call_0001", name="send_message", arguments={"channel": "telegram", "to": "me", "text": "hi"}))
        resolve_approval(state, registry, approved=True)  # gate: external send needs human OK
        msg = state.messages[-1]
        assert msg.role == "tool"
        assert "error" in msg.content
        assert "simulated failure" in msg.content  # the remote's own detail, surfaced
        assert len(server.state.calls) == 1  # the failure came from the wire, not local validation
    finally:
        server.stop()


def test_registry_definitions_loaded() -> None:
    server = MockMcpServer()
    server.start()
    try:
        registry = ToolRegistry()
        register_networked_tools(registry, base_url=f"http://127.0.0.1:{server.port}")
        names = {d.name for d in registry.definitions()}
        assert names == {"send_email", "send_message"}
    finally:
        server.stop()


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
    print(f"\nAll {len(tests)} networked-tool smoke tests passed.")


if __name__ == "__main__":
    main()