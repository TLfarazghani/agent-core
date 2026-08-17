# Data Contracts

The authoritative definitions. The JSON Schemas in `schemas/` are the source of truth; `core/schemas.py` mirrors them in pydantic. Define these before any code — never "whatever the model happens to emit."

These contracts are shared verbatim across Windows, Android, and Web. `ChatMessage` mirrors the Leap SDK's OpenAI-compatible shape so payloads are interchangeable between targets.

## ToolDefinition

Source: `schemas/tool_definition.schema.json`. Goes in the system prompt or `tools=` param.

```json
{
  "name": "get_object_info",
  "description": "Return transform, mesh stats, and material slots for a Blender object by name.",
  "parameters": {
    "type": "object",
    "properties": {
      "object_name": { "type": "string" }
    },
    "required": ["object_name"]
  }
}
```

| Field | Type | Rules |
|---|---|---|
| `name` | string | required; unique within a registry |
| `description` | string | required; how the model decides to call it |
| `parameters` | JSON Schema object | required; `type: "object"` |

## ToolCall

The result of parsing a model message. Identified by a stable `id` so tool results can be correlated.

```json
{
  "id": "call_0001",
  "name": "get_object_info",
  "arguments": { "object_name": "Cube.003" }
}
```

**Arguments are always JSON** — protocol decision in AGENTS.md. The model's Pythonic calls (`get_object_info(object_name="Cube.003")`) are forced to JSON via the system prompt.

## ChatMessage

Source: `schemas/chat_message.schema.json`. Mirrors the Leap SDK `ChatMessage` (OpenAI-compatible). Reused everywhere so Windows/Android/Web payloads are interchangeable.

```json
{
  "role": "user | assistant | tool | system",
  "content": [{ "type": "text", "text": "..." }],
  "reasoningContent": null,
  "functionCalls": [
    {
      "id": "call_0001",
      "name": "get_object_info",
      "arguments": { "object_name": "Cube.003" }
    }
  ]
}
```

| Field | Type | Rules |
|---|---|---|
| `role` | enum | `user`, `assistant`, `tool`, `system` |
| `content` | array of blocks | each block `{ "type": "text", "text": "..." }`; may be empty when `functionCalls` present |
| `reasoningContent` | string \| null | unused for Instruct; reserved for Thinking variants |
| `functionCalls` | array of ToolCall \| null | absent for user/system/tool messages |

Tool-result messages use `role: "tool"` and carry the result in `content`, correlated via `functionCalls[].id` on the preceding assistant message.

## AgentState

Source: `schemas/agent_state.schema.json`. The Core owns this; platforms never mutate it directly.

```json
{
  "session_id": "uuid",
  "target": "windows | android | webgpu",
  "model": "LFM2.5-1.2B-Instruct",
  "messages": [],
  "tool_registry": [],
  "max_turns": 8,
  "turn_count": 0
}
```

| Field | Type | Rules |
|---|---|---|
| `session_id` | string | uuid |
| `target` | enum | `windows`, `android`, `webgpu` |
| `model` | string | model identifier for the provider |
| `messages` | array of ChatMessage | the full conversation history |
| `tool_registry` | array of ToolDefinition | available tools for this session |
| `max_turns` | int | loop cap, default 8 |
| `turn_count` | int | incremented by the loop each generate+execute cycle |

## Enforcement

- **Windows (Python):** pydantic models in `core/schemas.py`; every message validated entering/leaving the core.
- **Android (Kotlin):** `kotlinx.serialization` — same shape, same field names.
- **Web (JS):** zod or manual guards — same shape.
- Not "whatever the model happens to emit." Schema enforcement runs before any tool executes.

## Sample payloads for tests

The three JSON documents above are the canonical test fixtures. `tests/test_schemas.py` validates these exact payloads against the schemas and pydantic models, plus invalid variants (bad role, non-object arguments, missing required parameter).
