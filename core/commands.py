"""Shared slash-command registry (Phase 8).

One stdlib-only, transport-agnostic table of ``{name, description, handler}``
consumed by **both** ``cli.py`` and ``web/server.py``, so a command behaves
identically in the terminal and the browser. Platforms only render
``CommandResult`` and apply the state/action fields — they never re-implement
command logic (the same rule as the loop and the tool registry).

Commands never bypass the approval gate: ``/approve`` and ``/reject`` call
``core.loop.resolve_approval``, which is the only path that can execute a
parked call. ``/mode`` changes ``AgentState.permission_mode``, which
``core.tool_registry.dispatch()`` enforces (``run_code`` stays hardcoded).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .context import estimate_tokens
from .memory import MEMORY_DIR, recall_bounded
from .meta import AGENT_NAME, agent_bio, inspect_self
from .sessions import (
    SESSION_DIR,
    load_session,
    new_agent_state,
    refresh_agent_bio,
)
from .state import PERMISSION_MODES, AgentState, ChatMessage
from .tool_registry import ToolRegistry

DEFAULT_MAX_CONTEXT_TOKENS = 32768


@dataclass
class CommandContext:
    """Everything a command may read or mutate, injected by the platform."""

    state: AgentState
    registry: ToolRegistry
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    session_dir: Path = field(default_factory=lambda: SESSION_DIR)
    memory_dir: Path = field(default_factory=lambda: MEMORY_DIR)


@dataclass
class CommandResult:
    """Rendered text plus the platform actions a command asks for."""

    text: str = ""
    state: Optional[AgentState] = None
    quit: bool = False
    clear: bool = False
    error: bool = False
    save: bool = False


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    usage: str
    handler: Callable[[CommandContext, str], CommandResult]


def _help(ctx: CommandContext, arg: str) -> CommandResult:
    lines = ["Commands:"]
    for command in COMMANDS.values():
        lines.append(f"  {command.usage:<22} {command.description}")
    return CommandResult(text="\n".join(lines))


def _quit(ctx: CommandContext, arg: str) -> CommandResult:
    return CommandResult(text="", quit=True, save=True)


def _new(ctx: CommandContext, arg: str) -> CommandResult:
    state = new_agent_state(
        memory_dir=ctx.memory_dir,
        max_turns=ctx.state.max_turns,
        model=ctx.state.model,
    )
    state.permission_mode = ctx.state.permission_mode
    state.messages.append(
        ChatMessage(
            role="system", content=agent_bio(state, ctx.registry, ctx.max_context_tokens)
        )
    )
    return CommandResult(text="New session started.", state=state, clear=True, save=True)


def _turns(ctx: CommandContext, arg: str) -> CommandResult:
    try:
        value = int(arg)
    except (TypeError, ValueError):
        return CommandResult(text="Usage: /turns <n>  (positive integer)", error=True)
    if value < 1:
        return CommandResult(text="Usage: /turns <n>  (positive integer)", error=True)
    ctx.state.max_turns = value
    return CommandResult(
        text=f"Turn budget for this session set to {value}.", save=True
    )


def _mode(ctx: CommandContext, arg: str) -> CommandResult:
    wanted = (arg or "").strip().lower()
    if not wanted:
        current = ctx.state.permission_mode
        listing = ", ".join(
            f"{m}*" if m == current else m for m in PERMISSION_MODES
        )
        return CommandResult(
            text=(
                f"Permission mode: {current}  ({listing})\n"
                "  default = ask for approval-gated tools\n"
                "  plan    = read-only tools only, everything else is parked\n"
                "  auto    = auto-approve gated tools except run_code\n"
                "  strict  = ask before every tool call\n"
                "run_code always requires explicit approval in every mode."
            )
        )
    if wanted not in PERMISSION_MODES:
        return CommandResult(
            text=f"Usage: /mode [{'|'.join(PERMISSION_MODES)}]", error=True
        )
    ctx.state.permission_mode = wanted  # type: ignore[assignment]
    return CommandResult(text=f"Permission mode set to {wanted}.", save=True)


def _tools(ctx: CommandContext, arg: str) -> CommandResult:
    lines = ["Registered tools:"]
    for definition in sorted(ctx.registry.definitions(), key=lambda d: d.name):
        flags = []
        if definition.requires_approval:
            flags.append("approval")
        if definition.read_only:
            flags.append("read-only")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {definition.name:<16} {definition.description}{suffix}")
    return CommandResult(text="\n".join(lines))


def _status(ctx: CommandContext, arg: str) -> CommandResult:
    return CommandResult(
        text=inspect_self(ctx.state, ctx.registry, ctx.max_context_tokens)
    )


def _context(ctx: CommandContext, arg: str) -> CommandResult:
    used = estimate_tokens(ctx.state.messages)
    budget = ctx.max_context_tokens
    pct = (used / budget * 100) if budget else 0.0
    return CommandResult(
        text=(
            f"Context: ~{used} tokens / {budget} ({pct:.1f}%)\n"
            f"Messages: {len(ctx.state.messages)}  "
            f"turns: {ctx.state.turn_count}/{ctx.state.max_turns}  "
            f"mode: {ctx.state.permission_mode}"
        )
    )


def _plan(ctx: CommandContext, arg: str) -> CommandResult:
    plan = ctx.state.plan
    if plan is None:
        return CommandResult(text="No plan in progress.")
    markers = {
        "done": "✓",
        "in_progress": "▶",
        "failed": "✗",
        "pending": "·",
        "skipped": "−",
    }
    lines = [f"Plan: {plan.goal}"]
    for step in plan.steps:
        suffix = f" — {step.result}" if step.result else ""
        lines.append(
            f"  {markers.get(step.status, '·')} {step.id} {step.description} "
            f"[{step.status}]{suffix}"
        )
    return CommandResult(text="\n".join(lines))


def _memory(ctx: CommandContext, arg: str) -> CommandResult:
    text = recall_bounded(arg.strip(), memory_dir=ctx.memory_dir, max_tokens=1024)
    if not text:
        topic = f" for '{arg.strip()}'" if arg.strip() else ""
        return CommandResult(text=f"No memories found{topic}.")
    return CommandResult(text=f"Memories:\n{text}")


def _resume(ctx: CommandContext, arg: str) -> CommandResult:
    session_id = arg.strip()
    if not session_id:
        return CommandResult(text="Usage: /resume <session_id>", error=True)
    try:
        state = load_session(session_id, ctx.session_dir)
    except FileNotFoundError:
        return CommandResult(text=f"No session '{session_id}' found.", error=True)
    except (ValueError, OSError):
        return CommandResult(text=f"Invalid session id '{session_id}'.", error=True)
    refresh_agent_bio(state, ctx.registry)
    return CommandResult(
        text=f"Resumed session {session_id}.", state=state, clear=True
    )


def _approve(ctx: CommandContext, arg: str) -> CommandResult:
    from .loop import resolve_approval

    if ctx.state.pending_approval is None:
        return CommandResult(text="No pending approval.")
    resolve_approval(ctx.state, ctx.registry, approved=True)
    last = ctx.state.messages[-1] if ctx.state.messages else None
    return CommandResult(text=f"Approved. {last.content if last else ''}".strip(), save=True)


def _reject(ctx: CommandContext, arg: str) -> CommandResult:
    from .loop import resolve_approval

    if ctx.state.pending_approval is None:
        return CommandResult(text="No pending approval.")
    resolve_approval(ctx.state, ctx.registry, approved=False)
    return CommandResult(text="Rejected: tool did not run.", save=True)


_COMMANDS = (
    Command("/help", "list commands", "/help", _help),
    Command("/new", "start a fresh session", "/new", _new),
    Command("/resume", "load a saved session", "/resume <id>", _resume),
    Command("/turns", "set this session's turn budget", "/turns <n>", _turns),
    Command("/mode", "get or set the permission mode", "/mode [default|plan|auto|strict]", _mode),
    Command("/tools", "list registered tools", "/tools", _tools),
    Command("/status", "live self snapshot", "/status", _status),
    Command("/context", "token budget usage", "/context", _context),
    Command("/plan", "render the current plan", "/plan", _plan),
    Command("/memory", "recall long-term memory", "/memory [topic]", _memory),
    Command("/approve", "approve the pending call", "/approve", _approve),
    Command("/reject", "reject the pending call", "/reject", _reject),
    Command("/quit", "save and exit (CLI)", "/quit", _quit),
)

COMMANDS: dict[str, Command] = {command.name: command for command in _COMMANDS}


def is_command(line: str) -> bool:
    return line.startswith("/")


def parse_command(line: str) -> tuple[str, str]:
    """Split ``"/turns 12"`` into ``("/turns", "12")``."""
    name, _, arg = line.strip().partition(" ")
    return name, arg.strip()


def command_names() -> list[str]:
    return list(COMMANDS)


def help_text() -> str:
    return "\n".join(
        f"  {command.usage:<22} {command.description}"
        for command in COMMANDS.values()
    )


def run_command(ctx: CommandContext, line: str) -> CommandResult:
    """Run one slash command. Unknown commands return an error result."""
    name, arg = parse_command(line)
    command = COMMANDS.get(name)
    if command is None:
        return CommandResult(text=f"Unknown command: {name}", error=True)
    try:
        return command.handler(ctx, arg)
    except Exception as exc:  # noqa: BLE001 - a command must never kill the UI
        return CommandResult(
            text=f"Command failed: {type(exc).__name__}: {exc}", error=True
        )


__all__ = [
    "AGENT_NAME",
    "COMMANDS",
    "Command",
    "CommandContext",
    "CommandResult",
    "command_names",
    "help_text",
    "is_command",
    "parse_command",
    "run_command",
]
