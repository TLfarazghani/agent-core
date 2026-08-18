"""Tool registry: schema-validated registration + hardcoded approval gate.

The approval gate for high-risk tools (``run_code``) is enforced HERE in
``dispatch()``, never model-decided and never re-implemented per platform.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import jsonschema

from .state import AgentState, ChatMessage, PendingApproval, ToolCall, ToolDefinition

# Tools that always require human approval regardless of their definition.
HARDCODED_APPROVAL_TOOLS = frozenset({"run_code"})

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

ToolHandler = Callable[[dict[str, Any]], str]


def _load_schema(name: str) -> dict:
    with (_SCHEMA_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._tool_schema = _load_schema("tool_definition.schema.json")

    def register(self, definition: ToolDefinition | dict, handler: ToolHandler) -> None:
        if isinstance(definition, dict):
            self._validate_definition(definition)
            definition = ToolDefinition(**definition)
        else:
            self._validate_definition(definition.model_dump())
        if definition.name in self._definitions:
            raise ValueError(f"duplicate tool registration: {definition.name}")
        self._definitions[definition.name] = definition
        self._handlers[definition.name] = handler

    def register_many(self, entries: list[tuple[ToolDefinition | dict, ToolHandler]]) -> None:
        for definition, handler in entries:
            self.register(definition, handler)

    def load_json(
        self,
        path: str | Path,
        handlers: dict[str, ToolHandler],
        names: set[str] | None = None,
    ) -> None:
        """Register tools from a JSON file.

        ``names`` restricts which entries are registered (used to load a
        subset such as only the networked tools); ``None`` registers all.
        """
        with Path(path).open(encoding="utf-8") as fh:
            entries = json.load(fh)
        for definition in entries:
            name = definition["name"]
            if names is not None and name not in names:
                continue
            self._validate_definition(definition)
            if name not in handlers:
                raise ValueError(f"no handler provided for tool '{name}'")
            self.register(definition, handlers[name])

    def _validate_definition(self, definition: dict) -> None:
        try:
            jsonschema.validate(instance=definition, schema=self._tool_schema)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"invalid tool definition: {exc.message}") from exc

    def definition(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return list(self._definitions.values())

    def _requires_approval(self, definition: ToolDefinition) -> bool:
        return definition.name in HARDCODED_APPROVAL_TOOLS or definition.requires_approval

    def _validate_call(self, call: ToolCall, definition: ToolDefinition) -> str | None:
        """Return an error string if ``call.arguments`` fail the schema."""
        try:
            jsonschema.validate(instance=call.arguments, schema=definition.parameters)
        except jsonschema.ValidationError as exc:
            return f"error: invalid arguments for '{call.name}': {exc.message}"
        return None

    def dispatch(self, state: AgentState, call: ToolCall) -> ChatMessage | None:
        """Execute ``call`` or halt for approval.

        Returns the tool-result ``ChatMessage``, or ``None`` when the call is
        waiting on human approval (``state.pending_approval`` is set).

        Arguments are validated BEFORE the approval gate: a malformed call is
        rejected immediately instead of being offered to the human for approval.
        """
        definition = self._definitions.get(call.name)
        if definition is None:
            return ChatMessage(
                role="tool",
                tool_call_id=call.id,
                content=f"error: unknown tool '{call.name}'",
            )
        invalid = self._validate_call(call, definition)
        if invalid is not None:
            return ChatMessage(role="tool", tool_call_id=call.id, content=invalid)
        if self._requires_approval(definition):
            state.pending_approval = PendingApproval(
                call_id=call.id, tool_name=call.name, arguments=call.arguments
            )
            return None
        return self.execute(call)

    def execute(self, call: ToolCall) -> ChatMessage:
        """Run ``call`` regardless of approval flag.

        Used by ``loop.resolve_approval`` on ``approved=True``. Validation of
        the tool's arguments against its JSON Schema is applied here.
        """
        definition = self._definitions.get(call.name)
        if definition is None:
            return ChatMessage(
                role="tool",
                tool_call_id=call.id,
                content=f"error: unknown tool '{call.name}'",
            )
        invalid = self._validate_call(call, definition)
        if invalid is not None:
            return ChatMessage(role="tool", tool_call_id=call.id, content=invalid)
        try:
            result = self._handlers[call.name](call.arguments)
        except Exception as exc:  # noqa: BLE001 - surface handler errors as tool results
            result = f"error: {type(exc).__name__}: {exc}"
        return ChatMessage(role="tool", tool_call_id=call.id, content=result)