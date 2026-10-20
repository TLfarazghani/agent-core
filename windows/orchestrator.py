"""Windows orchestrator: adapter between llama-server (OpenAI v3 API) and core.

Turns ``AgentState`` into OpenAI-shaped chat completions requests and maps the
llama.cpp ``--jinja`` response back to the snake_case core contract.

Only this module talks to the model server. It never re-implements the loop or
the approval gate — it implements ``core.loop.GenerateProvider``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from openai import OpenAI

from core import AgentState, ChatMessage, ToolCall, ToolRegistry
from core.context import compact_to_budget, trim_to_budget
from core.telemetry import DEFAULT_SERVER_URL, record_usage

logger = logging.getLogger(__name__)

OPENAI_API_KEY = "local-no-key-required"
# Canonical constant lives in core/telemetry.py (Phase 12 doctor probes use it
# too); re-exported under the Phase 4 name so cli.py / web/server.py imports
# keep working.
DEFAULT_BASE_URL = DEFAULT_SERVER_URL
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TOP_K = 50
DEFAULT_TOP_P = 0.1
DEFAULT_REPETITION_PENALTY = 1.05
DEFAULT_MAX_CONTEXT_TOKENS = 32768

TokenSink = Callable[[str], None]


def default_compaction_enabled() -> bool:
    """Phase 6b: opt-in compaction (``AGENT_CORE_COMPACT=1``) replaces dropped
    turns with a one-extra-generate summary instead of silently forgetting."""
    raw = os.environ.get("AGENT_CORE_COMPACT", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def default_max_context_tokens() -> int:
    """Context budget for the wire request (must match the server's ``--ctx-size``).

    Phase 7: the 2.6B model runs at 128K ctx, so this must be selectable, not
    hardcoded. ``AGENT_CORE_MAX_CONTEXT_TOKENS`` overrides the 32768 default.
    """
    raw = os.environ.get("AGENT_CORE_MAX_CONTEXT_TOKENS")
    if raw is None:
        return DEFAULT_MAX_CONTEXT_TOKENS
    try:
        return max(1024, int(raw))
    except ValueError:
        return DEFAULT_MAX_CONTEXT_TOKENS


# Per-model sampling that mirrors the server_config.ps1 table. The OpenAI-style
# request always carries temperature/top_k/top_p/repetition_penalty, which would
# otherwise override the server-side flags and silently apply 1.2B's sampling to
# every model (measured: 8B-A1B's run_code reliability collapses under 0.1/50/0.1).
SAMPLING_BY_MODEL = {
    "LFM2.5-1.2B-Instruct": (0.1, 50, 0.1, 1.05),
    "LFM2.5-2.6B": (0.1, 50, 0.1, 1.1),
    "LFM2.5-8B-A1B": (0.2, 80, 0.2, 1.05),
}


def default_sampling(model: str) -> tuple[float, int, float, float]:
    """(temperature, top_k, top_p, repetition_penalty) for a model, falling back
    to the 1.2B defaults for unknown models."""
    return SAMPLING_BY_MODEL.get(
        model, (DEFAULT_TEMPERATURE, DEFAULT_TOP_K, DEFAULT_TOP_P, DEFAULT_REPETITION_PENALTY)
    )


def _to_openai_message(message: ChatMessage) -> dict[str, Any]:
    """Map one core ``ChatMessage`` to the OpenAI request shape."""
    if message.role == "assistant" and message.function_calls:
        tool_calls = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.function_calls
        ]
        return {"role": "assistant", "content": message.content, "tool_calls": tool_calls}
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    return {"role": message.role, "content": message.content}


def _to_openai_tools(registry: ToolRegistry) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.parameters,
            },
        }
        for definition in registry.definitions()
    ]


def _parse_arguments(raw: str) -> dict[str, Any]:
    """Parse the model's JSON-encoded arguments, degrading gracefully."""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    except (ValueError, TypeError):
        return {"raw": raw}


def _from_openai_message(message: Any) -> ChatMessage:
    """Map one OpenAI response message back to the core snake_case shape."""
    function_calls = None
    if getattr(message, "tool_calls", None):
        function_calls = [
            ToolCall(
                id=tool_call.id,
                name=tool_call.function.name,
                arguments=_parse_arguments(tool_call.function.arguments),
            )
            for tool_call in message.tool_calls
        ]
    return ChatMessage(
        role="assistant",
        content=message.content or "",
        function_calls=function_calls,
    )


class LlamaCppProvider:
    """``GenerateProvider`` backed by llama.cpp's OpenAI-compatible endpoint."""

    def __init__(
        self,
        registry: ToolRegistry,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = OPENAI_API_KEY,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        top_k: int = DEFAULT_TOP_K,
        top_p: float = DEFAULT_TOP_P,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
        max_context_tokens: int | None = None,
        stream: bool = True,
        stream_callback: TokenSink | None = None,
        compaction: bool | None = None,
    ) -> None:
        self.registry = registry
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_k = top_k
        self.top_p = top_p
        self.repetition_penalty = repetition_penalty
        self.max_context_tokens = (
            max_context_tokens
            if max_context_tokens is not None
            else default_max_context_tokens()
        )
        self.stream = stream
        self.stream_callback = stream_callback
        self.compaction = (
            default_compaction_enabled() if compaction is None else compaction
        )
        self._summarizing = False
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=120)
        self.last_usage: dict[str, int] = {}
        self.last_latency_s: float = 0.0
        explicit = (temperature, top_k, top_p, repetition_penalty) != (
            DEFAULT_TEMPERATURE,
            DEFAULT_TOP_K,
            DEFAULT_TOP_P,
            DEFAULT_REPETITION_PENALTY,
        )
        self._sampling = explicit or None

    def _summarize(self, model: str, dropped: list[ChatMessage]) -> str:
        """One non-streaming generate that summarizes dropped turns.

        Guarded by ``_summarizing`` so a summarize request can never recurse
        into compaction, and never raises: any failure falls back to a plain
        trim (the summary is simply omitted).
        """
        if self._summarizing:
            return ""
        self._summarizing = True
        try:
            transcript = "\n".join(
                f"{message.role}: {message.content or ''}"
                for message in dropped
                if message.content or message.function_calls
            )
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarize the earlier conversation in a few "
                            "sentences. Preserve facts, decisions, user "
                            "preferences, and any unfinished tasks."
                        ),
                    },
                    {"role": "user", "content": transcript[:8000]},
                ],
                temperature=0.1,
                max_tokens=256,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception:  # noqa: BLE001 - compaction must never break the turn
            return ""
        finally:
            self._summarizing = False

    def __call__(self, state: AgentState) -> ChatMessage:
        if self.compaction:
            kept, dropped, summary = compact_to_budget(
                state.messages,
                self.max_context_tokens,
                lambda dropped: self._summarize(state.model, dropped),
            )
        else:
            kept, dropped = trim_to_budget(state.messages, self.max_context_tokens)
            summary = None
        if dropped:
            logger.info(
                "trimmed %d old message(s) to fit %d-token context budget%s",
                len(dropped),
                self.max_context_tokens,
                " (compacted into a summary)" if summary else "",
            )
        messages = [_to_openai_message(message) for message in kept]
        tools = _to_openai_tools(self.registry)
        if self._sampling is None:
            temperature, top_k, top_p, repeat = default_sampling(state.model)
        else:
            temperature, top_k, top_p, repeat = self._sampling
        kwargs: dict[str, Any] = {
            "model": state.model,
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": self.max_tokens,
            "top_p": top_p,
            "extra_body": {
                "top_k": top_k,
                "repetition_penalty": repeat,
            },
        }

        started = time.monotonic()
        if not self.stream or self.stream_callback is None:
            response = self.client.chat.completions.create(**kwargs)
            self.last_latency_s = time.monotonic() - started
            self.last_usage = response.usage.model_dump() if response.usage else {}
            self._warn_on_overflow()
            record_usage(state, self.last_usage, self.last_latency_s)
            return _from_openai_message(response.choices[0].message)

        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        order: list[int] = []
        stream = self.client.chat.completions.create(
            **kwargs, stream=True, stream_options={"include_usage": True}
        )
        for chunk in stream:
            if chunk.usage:
                self.last_usage = chunk.usage.model_dump()
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                self.stream_callback(delta.content)
            if delta.tool_calls:
                for tool_call in delta.tool_calls:
                    index = tool_call.index
                    entry = tool_calls.setdefault(index, {"id": "", "name": "", "args": []})
                    if tool_call.id:
                        entry["id"] = tool_call.id
                    if tool_call.function:
                        if tool_call.function.name:
                            entry["name"] = tool_call.function.name
                        if tool_call.function.arguments:
                            entry["args"].append(tool_call.function.arguments)
                    if index not in order:
                        order.append(index)
        self.last_latency_s = time.monotonic() - started

        self._warn_on_overflow()
        record_usage(state, self.last_usage, self.last_latency_s)

        function_calls = None
        if tool_calls:
            function_calls = [
                ToolCall(
                    id=entry["id"] or f"call_{index}",
                    name=entry["name"] or "unknown",
                    arguments=_parse_arguments("".join(entry["args"])) if entry["args"] else {},
                )
                for index in order
                for entry in [tool_calls[index]]
            ]
        return ChatMessage(
            role="assistant",
            content="".join(content_parts),
            function_calls=function_calls,
        )

    def _warn_on_overflow(self) -> None:
        """Ground-truth check: llama-server reports real ``prompt_tokens``.

        The chars/4 trim heuristic is an estimate; if the actual prompt still
        overflows the budget, log it so a too-aggressive heuristic is visible
        instead of silently corrupting long sessions.
        """
        prompt_tokens = self.last_usage.get("prompt_tokens")
        if prompt_tokens and prompt_tokens > self.max_context_tokens:
            logger.warning(
                "prompt is %d tokens but context budget is %d — chars/4 estimate "
                "under-counted; consider raising max_context_tokens",
                prompt_tokens,
                self.max_context_tokens,
            )


def default_registry() -> ToolRegistry:
    """Full local registry (web search + doc-gen + run_code + email + cognitive
    + files + skills + tasks). The ``agent`` (sub-agent) tool is
    transport-registered because it needs a provider -- see
    ``tools.subagent.register_subagent_tool``.
    """
    from tools import (
        register_cognitive_tools,
        register_docgen_tools,
        register_email_tools,
        register_files_tools,
        register_runcode_tool,
        register_skills_tools,
        register_task_tools,
        register_web_tools,
    )

    registry = ToolRegistry()
    register_web_tools(registry)
    register_docgen_tools(registry)
    register_runcode_tool(registry)
    register_email_tools(registry)
    register_cognitive_tools(registry)
    register_files_tools(registry)
    register_skills_tools(registry)
    register_task_tools(registry)
    return registry
