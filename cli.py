"""Interactive CLI for agent-core.

Drives ``core.loop`` with the ``windows.orchestrator.LlamaCppProvider``. The
CLI only renders state and resolves approvals; it never re-implements the loop.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from core import (
    AgentState,
    ChatMessage,
    agent_bio,
    finalize_turn,
    resolve_approval,
    should_stop_after_retries,
    user_message,
)
from core.loop import MaxTurnsError
from core.sessions import SESSION_DIR, load_session, new_agent_state, refresh_agent_bio, save_session
from core.tool_registry import ToolRegistry

from windows.orchestrator import DEFAULT_BASE_URL, LlamaCppProvider, default_registry

HEALTH_URL = DEFAULT_BASE_URL.removesuffix("/v1") + "/health"

_IS_TTY = sys.stdout.isatty() and sys.stdin.isatty()


def _style(text: str, code: str) -> str:
    if not _IS_TTY:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _bold(text: str) -> str:
    return _style(text, "1")


def _dim(text: str) -> str:
    return _style(text, "2")


def _amber(text: str) -> str:
    return _style(text, "33")


def _red(text: str) -> str:
    return _style(text, "31")


def _tool_line(name: str) -> str:
    glyph = f"⟦ tool: {name} ⟧" if _IS_TTY else f"[tool: {name}]"
    return _bold(glyph)


def _done_line(result: str) -> str:
    first = result.splitlines()[0] if result else "(no output)"
    glyph = f"⟦ done: {first} ⟧" if _IS_TTY else f"[done: {first}]"
    return _dim(glyph)


def _error_line(result: str) -> str:
    glyph = f"⟦ error ⟧ {result}" if _IS_TTY else f"[error] {result}"
    return _red(glyph)


def check_server() -> None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f"health returned {response.status}")
    except Exception as exc:  # noqa: BLE001 - any failure means the server is down
        print(
            _red(
                "llama-server unreachable at "
                f"{DEFAULT_BASE_URL} (health: {exc}). "
                "Start it with: .\\windows\\server_config.ps1"
            ),
            file=sys.stderr,
        )
        sys.exit(1)


def render_plain_message(message: ChatMessage) -> None:
    if message.role in ("user", "system"):
        return
    if message.role == "assistant":
        if message.content:
            prefix = "assistant:" if not _IS_TTY else "assistant ›"
            print(f"\n {_bold(prefix)} {message.content}")
        for call in message.function_calls or []:
            print(f"  {_tool_line(call.name)}")
            for key, value in call.arguments.items():
                print(f"    {key} = {json.dumps(value)}")
    elif message.role == "tool":
        result = message.content or ""
        print_line = _done_line if not result.startswith("error") else _error_line
        print(f"  {print_line(result)}")


def prompt_approval(state: AgentState, registry: ToolRegistry) -> None:
    pending = state.pending_approval
    if pending is None:
        return
    glyph = "⚠ PENDING APPROVAL" if _IS_TTY else "PENDING APPROVAL"
    dash = "—" if _IS_TTY else "-"
    print(f"\n {_amber(glyph)} {dash} {_bold(pending.tool_name)}")
    for key, value in pending.arguments.items():
        print(f"   {key} = {json.dumps(value)}")
    answer = input(" Approve? [y/N] ").strip().lower()
    approved = answer in ("y", "yes")
    resolve_approval(state, registry, approved=approved)
    result = state.messages[-1]
    if approved:
        line = _done_line(result.content) if not result.content.startswith("error") else _error_line(result.content)
        print(f"  {line}")
    else:
        print(_dim("  ⟦ rejected: tool did not run ⟧" if _IS_TTY else "  [rejected: tool did not run]"))


def run_turn(
    state: AgentState,
    provider: LlamaCppProvider,
    registry: ToolRegistry,
    rendered: int = 0,
) -> int:
    while True:
        if state.pending_approval is not None:
            return rendered
        if state.turn_count >= state.max_turns:
            print(_amber("Turn budget reached. Start a new session with /new."))
            return rendered
        if should_stop_after_retries(state):
            finalize_turn(state)
            print(_dim("  ⟦ gave up: repeated tool failures this turn ⟧"))
            return rendered
        last = state.messages[-1] if state.messages else None
        if last is not None and last.role == "assistant" and not last.function_calls:
            finalize_turn(state)
            return rendered
        try:
            from core.loop import step

            step(state, provider, registry)
        except MaxTurnsError:
            print(_amber("Turn budget reached. Start a new session with /new."))
            return rendered
        for message in state.messages[rendered:]:
            render_plain_message(message)
        rendered = len(state.messages)


def print_header(state: AgentState) -> None:
    rule = "─" * 60 if _IS_TTY else "-" * 60
    sep = "·" if _IS_TTY else "|"
    print(_bold(rule))
    print(
        f"  agent-core {sep} {state.model} {sep} local {DEFAULT_BASE_URL} {sep} "
        f"session {state.session_id[:6]} {sep} turn {state.turn_count}/{state.max_turns}"
    )
    print(_bold(rule))


def print_tools(registry: ToolRegistry) -> None:
    print(_bold("Registered tools:"))
    for definition in registry.definitions():
        print(f"  {definition.name:<16} {definition.description}")
    print(_dim("  (web_search / send_email / send_message need MCP_BASE_URL set)"))


def render_plan(state: AgentState) -> None:
    plan = state.plan
    if plan is None:
        return
    glyph = "⟦ plan ⟧" if _IS_TTY else "[plan]"
    print(f"  {_bold(glyph)} {plan.goal}")
    markers = {"done": "✓", "in_progress": "▶", "failed": "✗", "pending": "·", "skipped": "−"}
    for step in plan.steps:
        marker = markers.get(step.status, "·")
        suffix = f" — {step.result}" if step.result else ""
        print(f"    {marker} {step.id} {step.description} {_dim(step.status)}{suffix}")


def new_agent(registry: ToolRegistry | None = None, max_turns: int | None = None) -> AgentState:
    state = new_agent_state(max_turns=max_turns)
    if registry is not None:
        state.messages.append(ChatMessage(role="system", content=agent_bio(state, registry)))
    return state


def main() -> int:
    check_server()

    registry = default_registry()
    mcp_base = os.environ.get("MCP_BASE_URL")
    if mcp_base:
        from tools import register_networked_tools

        register_networked_tools(registry, base_url=mcp_base, api_key=os.environ.get("MCP_API_KEY"))

    provider = LlamaCppProvider(registry=registry, stream=True)
    state = new_agent(registry)
    print_header(state)
    print(_dim("  local agent - /help for commands - Ctrl-C stops generation"))
    if not _IS_TTY:
        print(_dim("  (non-interactive output: ANSI disabled)"))

    interrupted = False
    rendered = 0
    while True:
        try:
            line = input("\n> ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print()
            continue

        if not line:
            continue
        if line.startswith("/"):
            command, _, arg = line.partition(" ")
            arg = arg.strip()
            if command == "/quit":
                break
            elif command == "/new":
                state = new_agent(registry)
                rendered = 0
                print_header(state)
            elif command == "/turns":
                try:
                    value = max(1, int(arg))
                except ValueError:
                    print(_red("Usage: /turns <n>  (positive integer)"))
                    continue
                state.max_turns = value
                print_header(state)
                print(_dim(f"  turn budget for this session set to {value}"))
            elif command == "/resume":
                try:
                    state = load_session(arg)
                    refresh_agent_bio(state, registry)
                    rendered = 0
                    print_header(state)
                except (FileNotFoundError, ValueError, json.JSONDecodeError):
                    print(_red(f"No session '{arg}' found."))
            elif command == "/tools":
                print_tools(registry)
            elif command == "/approve":
                if state.pending_approval is None:
                    print(_dim("No pending approval."))
                else:
                    resolve_approval(state, registry, approved=True)
                    render_plain_message(state.messages[-1])
            elif command == "/reject":
                if state.pending_approval is None:
                    print(_dim("No pending approval."))
                else:
                    resolve_approval(state, registry, approved=False)
                    print(_dim("  ⟦ rejected: tool did not run ⟧"))
            elif command == "/help":
                print(
                    "  /new <id>   new session   /resume <id>  load session\n"
                    "  /turns <n>  set this session's turn budget (default: env AGENT_CORE_MAX_TURNS / 8)\n"
                    "  /tools      list tools    /approve|/reject  resolve approval\n"
                    "  /quit       save + exit"
                )
            else:
                print(_dim(f"Unknown command: {command}"))
            continue

        if interrupted:
            interrupted = False
        state.messages.append(user_message(line))
        prefix = "user:" if not _IS_TTY else "user ›"
        print(f"\n {_bold(prefix)} {line}")
        rendered += 1
        rendered = run_turn(state, provider, registry, rendered)
        render_plan(state)
        if state.pending_approval is not None:
            prompt_approval(state, provider.registry)
            rendered = len(state.messages)
            rendered = run_turn(state, provider, registry, rendered)
            render_plan(state)

    path = save_session(state)
    print(_dim(f"\nsession saved to {path}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
