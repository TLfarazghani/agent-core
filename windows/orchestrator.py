"""Windows orchestrator: adapter between llama-server (OpenAI v3 API) and core.

Turns ``AgentState`` into OpenAI-shaped chat completions requests and maps the
llama.cpp ``--jinja`` response back to the snake_case core contract.

Only this module talks to the model server. It never re-implements the loop or
the approval gate — it implements ``core.loop.GenerateProvider``.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from openai import OpenAI

from core import AgentState, ChatMessage, ToolCall, ToolRegistry

OPENAI_API_KEY = "local-no-key-required"
DEFAULT_BASE_URL = "http://127.0.0.1:8001/v1"
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TOP_K = 50
DEFAULT_TOP_P = 0.1
DEFAULT_REPETITION_PENALTY = 1.05

TokenSink = Callable[[str], None]


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
        stream: bool = True,
        stream_callback: TokenSink | None = None,
    ) -> None:
        self.registry = registry
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_k = top_k
        self.top_p = top_p
        self.repetition_penalty = repetition_penalty
        self.stream = stream
        self.stream_callback = stream_callback
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=120)
        self.last_usage: dict[str, int] = {}
        self.last_latency_s: float = 0.0

    def __call__(self, state: AgentState) -> ChatMessage:
        messages = [_to_openai_message(message) for message in state.messages]
        tools = _to_openai_tools(self.registry)
        kwargs: dict[str, Any] = {
            "model": state.model,
            "messages": messages,
            "tools": tools,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "extra_body": {
                "top_k": self.top_k,
                "repetition_penalty": self.repetition_penalty,
            },
        }

        started = time.monotonic()
        if not self.stream or self.stream_callback is None:
            response = self.client.chat.completions.create(**kwargs)
            self.last_latency_s = time.monotonic() - started
            self.last_usage = response.usage.model_dump() if response.usage else {}
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


def default_registry() -> ToolRegistry:
    """Full local registry (web search + doc-gen + run_code)."""
    from tools import (
        register_docgen_tools,
        register_runcode_tool,
        register_web_tools,
    )

    registry = ToolRegistry()
    register_web_tools(registry)
    register_docgen_tools(registry)
    register_runcode_tool(registry)
    return registry
