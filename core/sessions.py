"""Session lifecycle: create (with system prompt), save, load, list, delete.

Sessions are plain serialized ``AgentState`` JSON under
``~/.agent-core/sessions/``. Transport-agnostic: the CLI and the web server
share this so a session started in one can be resumed in the other.
"""

from __future__ import annotations

import json
from pathlib import Path

from .loop import new_state
from .state import AgentState, ChatMessage

SESSION_DIR = Path.home() / ".agent-core" / "sessions"

SYSTEM_PROMPT = (
    "You are a helpful local assistant with access to tools. "
    "Call a tool when the user asks to: create a document (create_docx), "
    "create a presentation (create_pptx), run or execute code (run_code). "
    "When the user asks you to run code or create a file, you MUST call the "
    "matching tool. Do not ask for clarification or details first: if some "
    "arguments are missing, choose sensible defaults and proceed. "
    "If the user just asks a question, answer directly without tools."
)


def new_agent_state() -> AgentState:
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    state.messages.append(ChatMessage(role="system", content=SYSTEM_PROMPT))
    return state


def session_path(session_id: str, session_dir: Path = SESSION_DIR) -> Path:
    return session_dir / f"{session_id}.json"


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