"""Reflection: one-line lessons written to memory on terminal turns.

Bounded and exception-safe -- reflection never blocks the loop. The retry-once
budget lives in ``core/loop.py``; this module only translates a turn's outcome
into a ``kind=lesson`` memory entry.
"""

from __future__ import annotations

import time
from typing import Optional

from .state import AgentState


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