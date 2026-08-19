"""Web UI + agent API server (stdlib only).

Serves the vanilla JS single-page app from ``web/`` and exposes the agent over
Server-Sent Events. Reuses ``core.loop`` + ``windows.orchestrator``; the browser
never re-implements the loop, it only renders ``AgentState`` and resolves
approvals.

Run::

    .\\venv\\Scripts\\python web\\server.py [port]      # default 127.0.0.1:8002

Endpoints
    GET  /                          -> index.html
    GET  /app.js, /style.css        -> static assets
    GET  /api/health                -> model server status
    GET  /api/tools                 -> registered tool definitions
    GET  /api/search?q=..&kind=..   -> web/news/wikipedia search (no key)
    GET  /api/fetch?url=..          -> fetch a page as plain text
    POST /api/sessions              -> create a session (body: optional {"max_turns": int})
    GET  /api/sessions              -> list saved sessions
    DELETE /api/sessions            -> delete ALL sessions (returns {"deleted": int})
    GET  /api/sessions/<id>         -> full AgentState
    DELETE /api/sessions/<id>       -> delete a session
    POST /api/sessions/<id>/clear     -> reset history, keep the same session
    POST /api/sessions/<id>/messages  -> run a turn, streams SSE events
    POST /api/sessions/<id>/approve   -> approve pending tool, streams SSE
    POST /api/sessions/<id>/reject    -> reject pending tool, streams SSE

SSE event types: ``token``, ``tool_call``, ``tool_result``, ``approval``,
``error``, ``done`` (carries the full serialized AgentState).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import (
    AgentState,
    ChatMessage,
    ToolCall,
    agent_bio,
    finalize_turn,
    new_state,
    should_stop_after_retries,
    user_message,
)
from core.loop import MaxTurnsError, resolve_approval, step
from core.sessions import (
    delete_all_sessions,
    delete_session,
    list_sessions,
    load_session,
    new_agent_state,
    save_session,
)
from core.tool_registry import ToolRegistry

from windows.orchestrator import (
    DEFAULT_BASE_URL,
    LlamaCppProvider,
    default_registry,
)

_WEB_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WEB_DIR.parent
_MODELS_DIR = _REPO_ROOT / "models"
_HEALTH_URL = DEFAULT_BASE_URL.removesuffix("/v1") + "/health"

ProviderFactory = Callable[[Callable[[str], None]], Any]


def default_health_check() -> bool:
    try:
        with urllib.request.urlopen(_HEALTH_URL, timeout=5) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - any failure means the model server is down
        return False


def default_provider_factory(registry: ToolRegistry) -> ProviderFactory:
    def factory(emit_token: Callable[[str], None]) -> Any:
        return LlamaCppProvider(
            registry=registry,
            stream=True,
            stream_callback=emit_token,
        )

    return factory


class SSEWriter:
    """Write one-shot Server-Sent Event responses over HTTP/1.0 (close-delimited)."""

    def __init__(self, handler: BaseHTTPRequestHandler) -> None:
        self.handler = handler
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        try:
            handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def event(self, event: str, data: dict[str, Any]) -> None:
        self.handler.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
        self.handler.wfile.flush()

    def close(self) -> None:
        try:
            self.handler.wfile.flush()
        except OSError:
            pass


class AgentApp:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        provider_factory: ProviderFactory | None = None,
        health_check: Callable[[], bool] | None = None,
    ) -> None:
        self.registry = registry or default_registry()
        mcp_base = os.environ.get("MCP_BASE_URL")
        if mcp_base:
            from tools import register_networked_tools

            register_networked_tools(
                self.registry, base_url=mcp_base, api_key=os.environ.get("MCP_API_KEY")
            )
        self.provider_factory = provider_factory or default_provider_factory(self.registry)
        self.health_check = health_check or default_health_check
        self._sessions: dict[str, AgentState] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._lock_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def get_session(self, session_id: str) -> AgentState:
        state = self._sessions.get(session_id)
        if state is None:
            state = load_session(session_id)
            self._sessions[session_id] = state
        return state

    def create_session(self, max_turns: int | None = None) -> AgentState:
        state = new_agent_state(max_turns=max_turns)
        state.messages.append(
            ChatMessage(role="system", content=agent_bio(state, self.registry))
        )
        self._sessions[state.session_id] = state
        return state

    def delete_session(self, session_id: str) -> bool:
        in_memory = self._sessions.pop(session_id, None)
        return delete_session(session_id) or in_memory is not None

    def delete_all_sessions(self) -> int:
        """Delete every saved session (disk + in-memory). Returns the count."""
        removed = delete_all_sessions()
        with self._lock_guard:
            removed += len(self._sessions)
            self._sessions.clear()
            self._locks.clear()
        return removed

    def clear_session(self, session_id: str, max_turns: int | None = None) -> AgentState:
        """Reset a session to a fresh state, keeping the same session id.

        The session's turn-budget setting is preserved unless an explicit
        ``max_turns`` is supplied.
        """
        keep = self.get_session(session_id).max_turns
        state = new_agent_state(max_turns=max_turns if max_turns is not None else keep)
        state.session_id = session_id
        state.messages.append(
            ChatMessage(role="system", content=agent_bio(state, self.registry))
        )
        self._sessions[session_id] = state
        save_session(state)
        return state

    def _save_best_effort(self, state: AgentState) -> None:
        try:
            save_session(state)
        except Exception:  # noqa: BLE001 - never mask the original turn error
            pass

    def run_turn(self, session_id: str, out: SSEWriter) -> None:
        """Drive the loop, streaming events, until terminal / approval / cap."""
        with self._lock_for(session_id):
            self._stream_turn(session_id, out)

    def _emit_messages(self, out: SSEWriter, messages: list[ChatMessage]) -> None:
        for message in messages:
            if message.role == "assistant" and message.function_calls:
                for call in message.function_calls:
                    out.event(
                        "tool_call",
                        {"id": call.id, "name": call.name, "arguments": call.arguments},
                    )
            elif message.role == "tool":
                out.event(
                    "tool_result",
                    {
                        "call_id": message.tool_call_id,
                        "content": message.content,
                        "error": message.content.startswith("error"),
                    },
                )

    def _stream_turn(self, session_id: str, out: SSEWriter) -> None:
        """Run the loop to completion. Caller must hold the session lock."""
        state = self.get_session(session_id)

        def emit_token(text: str) -> None:
            out.event("token", {"text": text})

        provider = self.provider_factory(emit_token)
        while True:
            if state.pending_approval is not None:
                out.event(
                    "approval",
                    {
                        "call_id": state.pending_approval.call_id,
                        "tool_name": state.pending_approval.tool_name,
                        "arguments": state.pending_approval.arguments,
                    },
                )
                return
            if state.turn_count >= state.max_turns:
                out.event("error", {"message": "Turn budget reached. Start a new session."})
                self._save_best_effort(state)
                return
            last = state.messages[-1] if state.messages else None
            if last is not None and last.role == "assistant" and not last.function_calls:
                finalize_turn(state)
                out.event("done", {"state": state.model_dump()})
                save_session(state)
                return
            if should_stop_after_retries(state):
                finalize_turn(state)
                out.event("done", {"state": state.model_dump()})
                save_session(state)
                return
            before = len(state.messages)
            try:
                step(state, provider, self.registry)
            except MaxTurnsError:
                out.event("error", {"message": "Turn budget reached. Start a new session."})
                self._save_best_effort(state)
                return
            except Exception as exc:  # noqa: BLE001 - surface to the UI
                out.event("error", {"message": f"error: {type(exc).__name__}: {exc}"})
                self._save_best_effort(state)
                return
            self._emit_messages(out, state.messages[before:])

    def handle_approval(self, session_id: str, approved: bool, out: SSEWriter) -> None:
        with self._lock_for(session_id):
            state = self.get_session(session_id)
            if state.pending_approval is not None:
                before = len(state.messages)
                resolve_approval(state, self.registry, approved=approved)
                self._emit_messages(out, state.messages[before:])
            self._stream_turn(session_id, out)

    def health(self) -> bool:
        return self.health_check()


class Handler(BaseHTTPRequestHandler):
    app: AgentApp
    server: ThreadingHTTPServer  # type: ignore[assignment]

    protocol_version = "HTTP/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    _LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

    def _request_allowed(self) -> bool:
        """Reject DNS rebinding and cross-origin (CSRF) requests.

        The server is local-only, but binding to 127.0.0.1 is not enough: a
        malicious page in the same browser can still reach localhost, and DNS
        rebinding can point an attacker domain at 127.0.0.1. Accept requests
        only when the Host header names a loopback host, and when an Origin
        header is present it must be loopback too. We send no CORS headers and
        answer OPTIONS with 403, so cross-origin preflights are blocked at the
        browser even for same-browser pages.
        """
        host = self.headers.get("Host", "")
        hostname = host.split(":")[0].lower().strip()
        if hostname not in self._LOCAL_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin:
            origin_host = urllib.parse.urlsplit(origin).hostname
            if origin_host is None or origin_host.lower() not in self._LOCAL_HOSTS:
                return False
        return True

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    @staticmethod
    def _coerce_max_turns(body: dict[str, Any]) -> int | None:
        raw = body.get("max_turns")
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value < 1:
            return None
        return value

    def _search_dispatch(self, app: AgentApp, args: dict[str, Any]) -> None:
        try:
            msg = app.registry.dispatch(
                new_state(target="webgpu", model="web_search"),
                ToolCall(id="web_search", name="web_search", arguments=args),
            )
        except Exception as exc:  # noqa: BLE001
            return self._send_json(500, {"error": f"web search failed: {exc}"})
        return self._send_json(200, {"result": msg.content})

    def _fetch_dispatch(self, app: AgentApp, args: dict[str, Any]) -> None:
        try:
            msg = app.registry.dispatch(
                new_state(target="webgpu", model="fetch_url"),
                ToolCall(id="fetch_url", name="fetch_url", arguments=args),
            )
        except Exception as exc:  # noqa: BLE001
            return self._send_json(500, {"error": f"fetch failed: {exc}"})
        return self._send_json(200, {"result": msg.content})

    def _serve_static(self, name: str) -> None:
        return self._serve_file(_WEB_DIR, name)

    def _serve_model_file(self, name: str) -> None:
        return self._serve_file(_MODELS_DIR, name)

    def _serve_file(self, base: Path, name: str) -> None:
        path = (base / name).resolve()
        if base not in path.parents:
            self._send_json(403, {"error": "forbidden"})
            return
        if not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
            ".onnx": "application/octet-stream",
            ".gguf": "application/octet-stream",
            ".bin": "application/octet-stream",
            ".wasm": "application/wasm",
        }
        body = path.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", content_types.get(path.suffix, "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if not self._request_allowed():
            return self._send_json(403, {"error": "forbidden"})
        app: AgentApp = self.server.app
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path.startswith("/api/"):
            return self._handle_api_get(app, path)
        if path.startswith("/models/"):
            return self._serve_model_file(path[len("/models/"):])
        return self._serve_static(path.lstrip("/"))

    def _handle_api_get(self, app: AgentApp, path: str) -> None:
        if path == "/api/health":
            return self._send_json(200, {"ok": app.health(), "base_url": DEFAULT_BASE_URL})
        if path == "/api/search":
            params = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            args = {
                "query": (params.get("q") or params.get("query") or [""])[0],
                "kind": (params.get("kind") or ["web"])[0],
            }
            max_results = params.get("max_results", ["5"])
            try:
                args["max_results"] = int(max_results[0])
            except ValueError:
                pass
            return self._search_dispatch(app, args)
        if path == "/api/fetch":
            params = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            args = {"url": (params.get("url") or [""])[0]}
            max_chars = params.get("max_chars", ["4000"])
            try:
                args["max_chars"] = int(max_chars[0])
            except ValueError:
                pass
            return self._fetch_dispatch(app, args)
        if path == "/api/tools":
            tools = [
                {
                    "name": d.name,
                    "description": d.description,
                    "requires_approval": d.requires_approval,
                }
                for d in app.registry.definitions()
            ]
            return self._send_json(200, {"tools": tools})
        if path == "/api/sessions":
            return self._send_json(200, {"sessions": list_sessions()})
        if path.startswith("/api/sessions/") and not path.endswith("/messages"):
            session_id = path.split("/")[-1]
            if path.endswith("/approve") or path.endswith("/reject"):
                return self._send_json(405, {"error": "POST only"})
            try:
                state = app.get_session(session_id)
                return self._send_json(200, {"state": state.model_dump()})
            except ValueError:
                return self._send_json(400, {"error": "invalid session id"})
            except FileNotFoundError:
                return self._send_json(404, {"error": "session not found"})
        return self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if not self._request_allowed():
            return self._send_json(403, {"error": "forbidden"})
        app: AgentApp = self.server.app
        path = self.path.split("?", 1)[0]
        if path == "/api/sessions":
            body = self._read_json()
            max_turns = self._coerce_max_turns(body)
            if "max_turns" in body and max_turns is None:
                return self._send_json(400, {"error": "invalid max_turns"})
            state = app.create_session(max_turns=max_turns)
            return self._send_json(200, {"session_id": state.session_id})
        if path.startswith("/api/sessions/"):
            parts = path.split("/")
            session_id = parts[3]
            action = parts[4] if len(parts) > 4 else None
            if action == "clear":
                try:
                    app.get_session(session_id)
                except ValueError:
                    return self._send_json(400, {"error": "invalid session id"})
                except FileNotFoundError:
                    return self._send_json(404, {"error": "session not found"})
                body = self._read_json()
                max_turns = self._coerce_max_turns(body)
                if "max_turns" in body and max_turns is None:
                    return self._send_json(400, {"error": "invalid max_turns"})
                state = app.clear_session(session_id, max_turns=max_turns)
                return self._send_json(200, {"state": state.model_dump()})
            try:
                state = app.get_session(session_id)
            except ValueError:
                return self._send_json(400, {"error": "invalid session id"})
            except FileNotFoundError:
                return self._send_json(404, {"error": "session not found"})
            if action == "messages":
                body = self._read_json()
                state.messages.append(user_message(body.get("message", "")))
                self._stream(path, app, session_id)
                return
            if action == "approve":
                self._stream(path, app, session_id, approved=True)
                return
            if action == "reject":
                self._stream(path, app, session_id, approved=False)
                return
        return self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        if not self._request_allowed():
            return self._send_json(403, {"error": "forbidden"})
        app: AgentApp = self.server.app
        path = self.path.split("?", 1)[0]
        if path == "/api/sessions":
            removed = app.delete_all_sessions()
            return self._send_json(200, {"deleted": removed})
        if path.startswith("/api/sessions/"):
            session_id = path.split("/")[-1]
            try:
                deleted = app.delete_session(session_id)
            except ValueError:
                return self._send_json(400, {"error": "invalid session id"})
            return self._send_json(200 if deleted else 404, {"deleted": deleted})
        return self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server API
        """Reject cross-origin preflights outright (no CORS headers sent)."""
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _stream(
        self,
        path: str,
        app: AgentApp,
        session_id: str,
        approved: bool | None = None,
    ) -> None:
        try:
            out = SSEWriter(self)
            if approved is None:
                app.run_turn(session_id, out)
            else:
                app.handle_approval(session_id, approved, out)
            out.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - respond over SSE if possible
            try:
                out.event("error", {"message": f"error: {type(exc).__name__}: {exc}"})
                out.close()
            except Exception:  # noqa: BLE001
                pass


def make_server(app: AgentApp, port: int = 8002) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.app = app
    return server


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8002
    app = AgentApp()
    server = make_server(app, port)
    url = f"http://127.0.0.1:{port}"
    print(f"agent-core web UI at {url}  (llama-server at {DEFAULT_BASE_URL})")
    print(f"model status: {'online' if app.health() else 'OFFLINE - start .\\windows\\server_config.ps1'}")
    print("press Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
