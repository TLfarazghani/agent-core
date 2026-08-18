# Data Contracts

The authoritative definitions. The JSON Schemas in `schemas/` are the source of truth; `core/state.py` mirrors them in pydantic. Define these before any code — never "whatever the model happens to emit."

These contracts are shared verbatim across Windows, Android, and Web.

## Core envelope (ToolDefinition)

Source: `schemas/tool_definition.schema.json`. Goes in the system prompt or `tools=` param.

```json
{
  "name": "get_object_info",
  "description": "...",
  "requires_approval": false,
  "parameters": { "type": "object", "properties": { "...": "..." }, "required": [] }
}
```

| Field | Type | Rules |
|---|---|---|
| `name` | string | required; unique within a registry |
| `description` | string | required; how the model decides to call it |
| `requires_approval` | boolean | required; default `false`. `run_code` is **always** `true`, hardcoded in `tool_registry.dispatch()`, never model-decided |
| `parameters` | JSON Schema object | required; `type: "object"` |

## ToolCall

Parsed from a model message. Identified by a stable `id` so tool results can be correlated.

```json
{
  "id": "call_0001",
  "name": "get_object_info",
  "arguments": {}
}
```

## ChatMessage

Source: `schemas/chat_message.schema.json`. OpenAI-compatible, snake_case. Reused everywhere so payloads are interchangeable.

```json
{
  "role": "user | assistant | tool | system",
  "content": "string",
  "tool_call_id": "string | null",
  "function_calls": [ { "id": "call_0001", "name": "...", "arguments": {} } ]
}
```

| Field | Type | Rules |
|---|---|---|
| `role` | enum | `user`, `assistant`, `tool`, `system` |
| `content` | string | plain text; may be empty when `function_calls` present |
| `tool_call_id` | string \| null | correlates a `tool` message to the assistant's `function_calls[].id` |
| `function_calls` | array of ToolCall \| null | absent for user/system/tool messages |

Tool-result messages use `role: "tool"`, carry the result in `content`, and set `tool_call_id` to the call being answered.

## AgentState

Source: `schemas/agent_state.schema.json`. The Core owns this; platforms never mutate it directly.

```json
{
  "session_id": "uuid",
  "target": "windows | android | webgpu",
  "model": "LFM2.5-1.2B-Instruct",
  "messages": [],
  "max_turns": 8,
  "turn_count": 0,
  "pending_approval": null,
  "pending_calls": []
}
```

| Field | Type | Rules |
|---|---|---|
| `session_id` | string | uuid |
| `target` | enum | `windows`, `android`, `webgpu` |
| `model` | string | model identifier for the provider |
| `messages` | array of ChatMessage | the full conversation history |
| `max_turns` | int | loop cap, default 8 |
| `turn_count` | int | incremented by the loop each generate+execute cycle |
| `pending_approval` | object \| null | `{ "call_id", "tool_name", "arguments" }`; non-null exactly when a `requires_approval` call is waiting on a human |
| `pending_calls` | array of ToolCall | remaining tool calls of the current turn parked behind `pending_approval`; resumed in order by `resolve_approval()` |

`pending_approval` is enforced in code: `loop.step()` refuses to run until `resolve_approval()` clears it. It is never a convention. `pending_calls` exists so multi-call turns like `[run_code, echo]` don't silently drop the calls after the approval-gated one.

## Tool contracts by category

**Web search — local, keyless, stdlib-only (no MCP remote required):**

```json
{
  "name": "web_search",
  "parameters": { "query": "string", "kind": "web|news|wikipedia?", "max_results": "integer? 1-10" }
}
{
  "name": "fetch_url",
  "parameters": { "url": "string (http(s) only)", "max_chars": "integer? 200-20000" }
}
```

`web_search` backends: `web` → DuckDuckGo HTML, `news` → Google News RSS, `wikipedia` → Wikipedia Search API. In the browser, both tools proxy through `web/server.py` (`/api/search`, `/api/fetch`) to avoid CORS; neither requires approval (read-only).

**Networked — identical implementation shape across all platforms (remote HTTP/SSE, uniform MCP-remote, no subprocess transport issue), opt-in via `MCP_BASE_URL`:**

```json
{ "name": "send_email", "parameters": { "to": "string", "subject": "string", "body": "string", "attachments": "string[]?" } }
{ "name": "send_message", "parameters": { "channel": "whatsapp|telegram", "to": "string", "text": "string" } }
```

**Local compute — schema identical, backend is a separate implementation per platform:**

```json
{ "name": "create_docx", "parameters": { "title": "string", "sections": [{ "heading": "string", "body": "string" }] } }
{ "name": "create_pptx", "parameters": { "title": "string", "slides": [{ "title": "string", "bullets": "string[]" }] } }
```

| Tool | Windows | Android | WebGPU |
|---|---|---|---|
| create_docx | python-docx | Apache POI (heavy — reconsider for v1) | docx.js |
| create_pptx | python-pptx | Apache POI (heavy — reconsider for v1) | pptxgenjs |

**Code execution — own risk class. `requires_approval` always true, hardcoded in `tool_registry.dispatch()`, never model-decided:**

```json
{
  "name": "run_code",
  "parameters": { "language": "python|javascript|bash", "code": "string", "timeout_seconds": "integer" },
  "requires_approval": true
}
```

## Enforcement

- **Windows (Python):** pydantic in `core/state.py`; jsonschema on `register()`.
- **Android (Kotlin):** `kotlinx.serialization` — same shape, same field names.
- **Web (JS):** zod or manual guards — same shape.
- Not "whatever the model happens to emit." Schema enforcement runs before any tool executes.

## Test fixtures

`test_smoke.py` uses the contracts above: an ordinary tool executes; `run_code` sets `pending_approval` and halts; `resolve_approval(approved=False)` clears state without executing; `resolve_approval(approved=True)` runs.
