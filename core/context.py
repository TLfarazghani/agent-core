"""Context-window budget helpers (stdlib-only, cross-language contract).

Providers grow unboundedly: every turn re-sends the whole ``messages`` list, and
sessions persist across CLI/web, so history eventually exceeds the model's
context window. These helpers enforce an explicit token budget.

Contract notes (port to ``web/context.js`` / ``AgentCore.kt``):

- ``estimate_tokens`` is a cheap ``chars/4`` heuristic so it stays dependency-free
  and portable (no ``tiktoken``). Providers that know real token counts
  (llama-server returns ``usage.prompt_tokens``) should validate against it.
- ``trim_to_budget`` operates on **whole user turns**: a ``user`` message and
  everything after it up to the next ``user`` message are kept or dropped
  together. This is what guarantees an assistant ``tool_calls`` message is never
  separated from the ``tool`` results that answer it (OpenAI rejects that shape).
- The system prompt (everything before the first ``user`` message) and the last
  (in-flight) user turn are **always** kept. ``pending_approval`` / ``pending_calls``
  only ever reference the last turn, so a live approval gate is never trimmed.
- Neither function mutates its input.

Input messages may be pydantic ``ChatMessage`` objects or plain dicts with the
same snake_case keys (``role``/``content``/``function_calls``/``tool_call_id``).
"""

from __future__ import annotations

import json
from typing import Any, Sequence


def _attr(message: Any, name: str, default: Any = "") -> Any:
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def _message_text(message: Any) -> str:
    text = _attr(message, "content", "") or ""
    for call in _attr(message, "function_calls", []) or []:
        text += _attr(call, "name", "") + json.dumps(
            _attr(call, "arguments", {}), separators=(",", ":")
        )
    text += _attr(message, "tool_call_id", "") or ""
    return text


def estimate_message_tokens(message: Any) -> int:
    """Rough token count for one message: ``chars/4``, minimum 1."""
    return max(1, (len(_message_text(message)) + 3) // 4)


def estimate_tokens(messages: Sequence[Any]) -> int:
    """Rough total token count for a message list."""
    return sum(estimate_message_tokens(m) for m in messages)


def _user_indices(messages: Sequence[Any]) -> list[int]:
    return [i for i, m in enumerate(messages) if _attr(m, "role", None) == "user"]


def trim_to_budget(
    messages: Sequence[Any],
    budget_tokens: int | None,
) -> tuple[list[Any], list[Any]]:
    """Return ``(kept, dropped)``: the conversation trimmed to ``budget_tokens``.

    Keeps, unconditionally: everything before the first ``user`` message (the
    system prompt) and the last user turn (the in-flight exchange). Drops the
    oldest complete user turns first, then newer ones, until the rest fits.

    If even the always-kept minimum overflows ``budget_tokens``, that minimum
    is returned anyway (the gate is a floor, not a hard cap). ``budget_tokens``
    of ``None``/``<= 0`` disables trimming and returns everything unchanged.
    """
    if budget_tokens is None or budget_tokens <= 0:
        return list(messages), []

    user_idx = _user_indices(messages)
    if not user_idx:
        return list(messages), []

    prefix_end = user_idx[0]
    last_user = user_idx[-1]

    turns: list[tuple[int, int]] = []
    for k, u in enumerate(user_idx):
        end = user_idx[k + 1] if k + 1 < len(user_idx) else len(messages)
        turns.append((u, end))

    always_start, always_end = turns[-1]
    budget_left = (
        budget_tokens
        - estimate_tokens(messages[:prefix_end])
        - estimate_tokens(messages[always_start:always_end])
    )

    kept_indices: set[int] = set(range(prefix_end))
    kept_indices.update(range(always_start, always_end))

    for start, end in reversed(turns[:-1]):
        cost = estimate_tokens(messages[start:end])
        if cost > budget_left:
            break
        budget_left -= cost
        kept_indices.update(range(start, end))

    kept = [m for i, m in enumerate(messages) if i in kept_indices]
    dropped = [m for i, m in enumerate(messages) if i not in kept_indices]
    return kept, dropped
