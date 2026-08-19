"""Long-term memory: persistent JSON key/value store.

Cross-session, survives CLI <-> web resumes. Layout mirrors
``core/sessions.py`` (one JSON file per key under ``~/.agent-core/memory/``)
and applies the same traversal guard on keys. Entries:
``{key, content, kind: fact|preference|lesson|session_summary, created_at,
source_session}``.

Stdlib-only and transport-agnostic (the JS port is ``web/memory.js``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

MEMORY_DIR = Path.home() / ".agent-core" / "memory"
KINDS = ("fact", "preference", "lesson", "session_summary")


def memory_path(key: str, memory_dir: Optional[Path] = None) -> Path:
    """Resolve the file for a memory key, rejecting path traversal.

    Keys come straight from the model's tool arguments. On Windows both ``/``
    and ``\\`` are separators, so an unvalidated key like ``..\\..\\Users\\x``
    would escape the memory dir. Reject anything that is not a bare token.
    ``memory_dir=None`` means the default ``MEMORY_DIR``.
    """
    if memory_dir is None:
        memory_dir = MEMORY_DIR
    if not key or key in (".", ".."):
        raise ValueError(f"invalid memory key: {key!r}")
    if "/" in key or "\\" in key or ":" in key:
        raise ValueError(f"invalid memory key: {key!r}")
    lowered = key.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise ValueError(f"invalid memory key: {key!r}")
    path = (memory_dir / f"{key}.json").resolve()
    resolved_dir = memory_dir.resolve()
    if resolved_dir not in path.parents:
        raise ValueError(f"invalid memory key: {key!r}")
    return path


def save_memory(
    key: str,
    content: str,
    kind: str = "fact",
    source_session: str | None = None,
    memory_dir: Optional[Path] = None,
) -> dict:
    """Persist one memory entry. Overwrites an existing key.

    ``memory_dir=None`` means the default ``MEMORY_DIR`` (also what the
    cognitive handlers and reflection pass through).
    """
    if memory_dir is None:
        memory_dir = MEMORY_DIR
    if kind not in KINDS:
        raise ValueError(f"invalid memory kind: {kind!r} (expected one of {KINDS})")
    memory_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "key": key,
        "content": content,
        "kind": kind,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_session": source_session,
    }
    path = memory_path(key, memory_dir)
    path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    return entry


def load_memory(key: str, memory_dir: Optional[Path] = None) -> Optional[dict]:
    if memory_dir is None:
        memory_dir = MEMORY_DIR
    path = memory_path(key, memory_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_memories(memory_dir: Optional[Path] = None) -> list[dict]:
    if memory_dir is None:
        memory_dir = MEMORY_DIR
    if not memory_dir.exists():
        return []
    entries = []
    for path in memory_dir.glob("*.json"):
        try:
            entries.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    entries.sort(key=lambda e: e.get("created_at", ""))
    return entries


def recall_memories(
    topic: str,
    memory_dir: Optional[Path] = None,
    limit: int = 5,
) -> list[dict]:
    """Keyword match over key + content + kind. Empty topic recalls newest."""
    if memory_dir is None:
        memory_dir = MEMORY_DIR
    topic_lower = (topic or "").lower()
    matches = []
    for entry in reversed(list_memories(memory_dir)):
        haystack = (
            f"{entry.get('key', '')} {entry.get('content', '')} "
            f"{entry.get('kind', '')}".lower()
        )
        if not topic_lower or topic_lower in haystack:
            matches.append(entry)
        if len(matches) >= limit:
            break
    return matches


def recall_bounded(
    topic: str,
    memory_dir: Optional[Path] = None,
    max_tokens: int = 512,
) -> str:
    """Return matching memories trimmed to a token budget (chars/4 heuristic).

    Used to seed ``new_agent_state()`` so a session's recall fits the
    context window instead of growing unboundedly. Newest matches first.
    ``memory_dir=None`` means the default ``MEMORY_DIR``.
    """
    if memory_dir is None:
        memory_dir = MEMORY_DIR
    entries = recall_memories(topic, memory_dir)
    if not entries:
        return ""
    lines: list[str] = []
    used = 0
    for entry in entries:
        line = f"[{entry.get('kind')}] {entry.get('key')}: {entry.get('content')}"
        cost = max(1, (len(line) + 3) // 4)
        if lines and used + cost > max_tokens:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines)
