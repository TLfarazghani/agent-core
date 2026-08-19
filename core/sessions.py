"""Session lifecycle: create (with system prompt), save, load, list, delete.

Sessions are plain serialized ``AgentState`` JSON under
``~/.agent-core/sessions/``. Transport-agnostic: the CLI and the web server
share this so a session started in one can be resumed in the other.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .loop import new_state
from .memory import MEMORY_DIR, recall_bounded
from .meta import AGENT_NAME
from .state import AgentState, ChatMessage

SESSION_DIR = Path.home() / ".agent-core" / "sessions"

DEFAULT_MAX_TURNS = 8

DEFAULT_MODEL = "LFM2.5-1.2B-Instruct"


def default_model() -> str:
    """Model alias for new sessions (Windows path).

    Phase 7: selectable, not hardcoded. ``AGENT_CORE_MODEL`` (e.g.
    ``LFM2.5-2.6B``) picks the model; defaults to the shipped 1.2B.
    """
    return os.environ.get("AGENT_CORE_MODEL", DEFAULT_MODEL)


def default_max_turns() -> int:
    """Per-session turn budget for new sessions.

    Overridable with the ``AGENT_CORE_MAX_TURNS`` env var; a session can also
    override it at creation time (``new_agent_state(max_turns=...)``).
    """
    raw = os.environ.get("AGENT_CORE_MAX_TURNS")
    if raw is None:
        return DEFAULT_MAX_TURNS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_TURNS

SYSTEM_PROMPT = (
    f"You are {AGENT_NAME}, a local, privacy-first personal assistant running "
    "entirely on the user's machine. Your capabilities: web search "
    "(web_search), fetching pages (fetch_url), creating documents and "
    "presentations (create_docx, create_pptx), running code in a sandbox "
    "(run_code), and long-term memory across sessions (remember, recall). You "
    "can introspect yourself (inspect_self) and track multi-step tasks "
    "(make_plan, update_plan). Your limits: you run locally with no external "
    "APIs beyond web search, and run_code always asks the user for approval. "
    "When the user asks you to run code or create a file, you MUST call the "
    "matching tool. Do not ask for clarification or details first: if some "
    "arguments are missing, choose sensible defaults and proceed. "
    "If the user just asks a question, answer directly without tools. "
    "For the current date/time, today, or any up-to-date fact, trust the "
    "'Current date/time' line in your identity block (it comes from the "
    "machine clock) and use web_search to confirm or find current information; "
    "never invent a date or guess recent facts from memory."
)


def new_agent_state(
    memory_dir: Path = MEMORY_DIR,
    recall_tokens: int = 512,
    max_turns: int | None = None,
    model: str | None = None,
) -> AgentState:
    state = new_state(
        target="windows",
        model=model if model is not None else default_model(),
        max_turns=max_turns if max_turns is not None else default_max_turns(),
    )
    state.messages.append(ChatMessage(role="system", content=SYSTEM_PROMPT))
    recall = recall_bounded("", memory_dir=memory_dir, max_tokens=recall_tokens)
    if recall:
        state.messages.append(
            ChatMessage(role="system", content=f"Prior knowledge:\n{recall}")
        )
    return state


def refresh_agent_bio(state: AgentState, registry) -> None:
    """Replace (or append) the identity-block system message with a fresh one.

    Resumed sessions keep the original bio from disk, whose date/time can be
    stale; calling this before resuming re-injects the current clock value.
    """
    from .meta import agent_bio

    fresh = agent_bio(state, registry)
    prefix = f"You are {AGENT_NAME}."
    for i, message in enumerate(state.messages):
        if message.role == "system" and message.content.startswith(prefix):
            state.messages[i] = ChatMessage(role="system", content=fresh)
            return
    state.messages.append(ChatMessage(role="system", content=fresh))


def session_path(session_id: str, session_dir: Path = SESSION_DIR) -> Path:
    """Resolve the file for a session, rejecting path traversal.

    session_id comes straight from URLs / user input. On Windows, both ``\\``
    and ``/`` are path separators, so a value like ``..\\..\\Users\\bob\\x``
    would escape the sessions directory if unvalidated. Reject anything that
    is not a bare filename token.
    """
    if not session_id or session_id in (".", ".."):
        raise ValueError(f"invalid session_id: {session_id!r}")
    if "/" in session_id or "\\" in session_id or ":" in session_id:
        raise ValueError(f"invalid session_id: {session_id!r}")
    lowered = session_id.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise ValueError(f"invalid session_id: {session_id!r}")
    path = (session_dir / f"{session_id}.json").resolve()
    resolved_dir = session_dir.resolve()
    if resolved_dir not in path.parents:
        raise ValueError(f"invalid session_id: {session_id!r}")
    return path


def save_session(state: AgentState, session_dir: Path = SESSION_DIR) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_path(state.session_id, session_dir)
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_session(session_id: str, session_dir: Path = SESSION_DIR) -> AgentState:
    return AgentState.model_validate_json(
        session_path(session_id, session_dir).read_text(encoding="utf-8")
    )


def list_sessions(session_dir: Path = SESSION_DIR) -> list[dict]:
    """List saved sessions newest-first with a preview of the first user turn."""
    if not session_dir.exists():
        return []
    entries = []
    for path in session_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        preview = next(
            (m["content"] for m in data.get("messages", []) if m.get("role") == "user"),
            "",
        )
        entries.append(
            {
                "session_id": data.get("session_id"),
                "model": data.get("model"),
                "turn_count": data.get("turn_count"),
                "max_turns": data.get("max_turns"),
                "preview": preview[:80],
            }
        )
    entries.sort(key=lambda e: e.get("session_id") or "", reverse=True)
    return entries


def delete_session(session_id: str, session_dir: Path = SESSION_DIR) -> bool:
    path = session_path(session_id, session_dir)
    if path.exists():
        path.unlink()
        return True
    return False


def delete_all_sessions(session_dir: Path = SESSION_DIR) -> int:
    """Delete every saved session. Returns the number of files removed."""
    if not session_dir.exists():
        return 0
    removed = 0
    for path in session_dir.glob("*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed