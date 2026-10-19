"""Reflection: lessons + automatic memory extraction on terminal turns.

Bounded and exception-safe -- reflection never blocks the loop. The retry-once
budget lives in ``core/loop.py``; this module translates a turn's outcome into
``kind=lesson`` memory entries and (Phase 11) proposes durable
``kind=fact|preference`` memories, deduped by key.

Phase 11 adds an exception-safe heuristic extractor (no extra model turn by
default) plus an optional provider-driven extractor (one extra generate) that
a transport may enable.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Optional

from .state import AgentState

_LEARNING_PATTERNS = (
    ("preference", re.compile(r"(?i)\bi (?:prefer|like|love|always|never)\b([^.!?\n]{1,120})")),
    ("fact", re.compile(r"(?i)\bmy (?:name|email|number|timezone|city|team|company|project) is\b([^.!?\n]{1,120})")),
    ("fact", re.compile(r"(?i)\bi use\b([^.!?\n]{1,120})")),
    ("fact", re.compile(r"(?i)\bcall me\b([^.!?\n]{1,60})")),
)


def lesson_from_state(state: AgentState) -> Optional[str]:
    """Return a one-line lesson for the last exchange, or None when nothing
    worth remembering happened."""
    if state.retry_count > 0:
        return (
            "Lesson: a tool call failed; retry exactly once with corrected "
            "arguments, then give up cleanly."
        )
    for message in reversed(state.messages):
        if message.role == "tool" and message.content == "rejected by user":
            return "Lesson: the user rejected a tool call; never retry a rejected call."
        if message.role == "tool" and message.content.startswith("error"):
            return (
                "Lesson: a tool call errored; check its arguments and results "
                "before calling again."
            )
    return None


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (cleaned[:48] or "memory").strip("_")


def _candidate_memories(messages) -> list[dict]:
    """Heuristic fact/preference candidates from the latest user message."""
    latest = ""
    for message in reversed(list(messages)):
        if getattr(message, "role", None) == "user":
            latest = getattr(message, "content", "") or ""
            break
    if not latest:
        return []
    candidates: list[dict] = []
    for kind, pattern in _LEARNING_PATTERNS:
        for match in pattern.finditer(latest):
            phrase = match.group(1).strip().rstrip(".")
            if len(phrase) < 3:
                continue
            content = f"{match.group(0).strip()}"
            candidates.append({"key": _slug(content), "content": content, "kind": kind})
    return candidates


def _provider_candidates(messages, provider) -> list[dict]:
    """One extra generate asking the model for durable memories as JSON."""
    prompt = (
        "Extract durable facts or preferences about the user from this "
        "conversation. Reply with a JSON array of objects "
        '{"key": "...", "content": "...", "kind": "fact"|"preference"}. '
        "Use a short stable key so repeats overwrite. Reply [] if none.\n\n"
    )
    transcript = "\n".join(
        f"{getattr(m, 'role', '')}: {getattr(m, 'content', '')}"
        for m in messages
        if getattr(m, "content", "")
    )
    try:
        from .state import ChatMessage

        reply = provider(
            AgentState(
                target="windows",
                model="extract",
                messages=[ChatMessage(role="user", content=prompt + transcript)],
            )
        )
    except Exception:  # noqa: BLE001 - extraction must never block the loop
        return []
    text = getattr(reply, "content", "") or ""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    candidates = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind", "fact")
        content = str(item.get("content", "")).strip()
        key = str(item.get("key", "")).strip() or _slug(content)
        if kind in ("fact", "preference") and content:
            candidates.append({"key": key, "content": content, "kind": kind})
    return candidates


def extract_memories(
    state: AgentState,
    provider: Optional[Callable] = None,
    limit: int = 5,
    memory_dir=None,
) -> list[dict]:
    """Propose and persist durable memories from the latest turn.

    Deduped by key: an identical key+content already on disk is skipped, so
    re-extraction is idempotent. Bounded by ``limit``. Never raises.
    """
    try:
        from .memory import list_memories, save_memory

        if provider is not None:
            candidates = _provider_candidates(state.messages, provider)
        else:
            candidates = _candidate_memories(state.messages)

        existing = {
            (entry.get("key"), entry.get("content"))
            for entry in list_memories(memory_dir)
        }
        saved = []
        seen = set()
        for candidate in candidates:
            key, content = candidate["key"], candidate["content"]
            if (key, content) in existing or (key, content) in seen:
                continue
            seen.add((key, content))
            saved.append(
                save_memory(
                    key=key,
                    content=content,
                    kind=candidate["kind"],
                    source_session=state.session_id,
                    memory_dir=memory_dir,
                )
            )
            if len(saved) >= limit:
                break
        return saved
    except Exception:  # noqa: BLE001 - reflection must never block the loop
        return []


def maybe_emit_lesson(
    state: AgentState,
    memory_dir=None,
) -> Optional[str]:
    """Write one lesson to memory when warranted. Never raises."""
    lesson = lesson_from_state(state)
    if lesson is None:
        return None
    try:
        from .memory import save_memory

        save_memory(
            f"lesson_{int(time.time())}",
            lesson,
            kind="lesson",
            source_session=state.session_id,
            memory_dir=memory_dir,
        )
        return lesson
    except Exception:  # noqa: BLE001 - reflection must never block the loop
        return None


def maybe_extract_memories(
    state: AgentState,
    memory_dir=None,
    provider: Optional[Callable] = None,
) -> list[dict]:
    """Exception-safe wrapper used by the loop's terminal hook."""
    return extract_memories(state, provider=provider, memory_dir=memory_dir)