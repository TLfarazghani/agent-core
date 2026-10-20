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
  "read_only": true,
  "parameters": { "type": "object", "properties": { "...": "..." }, "required": [] }
}
```

| Field | Type | Rules |
|---|---|---|
| `name` | string | required; unique within a registry |
| `description` | string | required; how the model decides to call it |
| `requires_approval` | boolean | required; default `false`. `run_code` is **always** `true`, hardcoded in `tool_registry.dispatch()`, never model-decided |
| `read_only` | boolean | default `false` (Phase 8); marks a non-mutating tool. `plan` permission mode runs only `read_only` tools, and `strict` parks everything |
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
  "pending_calls": [],
  "retry_count": 0,
  "permission_mode": "default",
  "plan": null,
  "tasks": [],
  "active_subagents": 0,
  "tool_allowlist": null,
  "usage": []
}
```

| Field | Type | Rules |
|---|---|---|
| `session_id` | string | uuid |
| `target` | enum | `windows`, `android`, `webgpu` |
| `model` | string | model identifier for the provider |
| `messages` | array of ChatMessage | the full conversation history |
| `max_turns` | int | per-session loop cap, default 8; overridable per session via `AGENT_CORE_MAX_TURNS` env var (new sessions) or `POST /api/sessions {"max_turns": N}` / CLI `/turns <n>`; serialized with the session so it survives resume |
| `turn_count` | int | incremented by the loop each generate+execute cycle |
| `pending_approval` | object \| null | `{ "call_id", "tool_name", "arguments" }`; non-null exactly when a `requires_approval` call is waiting on a human |
| `pending_calls` | array of ToolCall | remaining tool calls of the current turn parked behind `pending_approval`; resumed in order by `resolve_approval()` |
| `retry_count` | int | consecutive tool failures in the current user turn (reset on each new user message); retry-once stops the loop past `RETRY_ONCE_LIMIT` (1) |
| `permission_mode` | enum | `default`, `plan`, `auto`, `strict` (Phase 8); enforced inside `tool_registry.dispatch()`, never model-decided. Set via `POST /api/sessions {"permission_mode": ...}`, the web mode selector, or `/mode [mode]` in the CLI and web UI. Serialized with the session so it survives resume |
| `plan` | object \| null | `{ "goal", "steps": [{ "id", "description", "status": "pending\|in_progress\|done\|failed\|skipped", "result" }] }`; explicit multi-step task tracking (Phase 5) |
| `tasks` | array of Task | Phase 11 durable backlog, distinct from `plan`: `[{ "id": "task_N", "description", "status": "pending\|in_progress\|done\|failed\|skipped", "result" }]`. Serialized with the session. Managed by `task_create`/`task_update`/`task_list` and `/tasks` |
| `active_subagents` | int | Phase 10; number of sub-agents currently running. The child is seeded with `MAX_CONCURRENT_SUBAGENTS` (1) so a nested `agent` call is refused — one level deep |
| `tool_allowlist` | array of string \| null | Phase 10; when set (by `run_skill`) `tool_registry.dispatch()` denies any tool not in the list. Cleared at the next user turn. A **restriction, not an approval bypass** — allowed calls still hit the gate |
| `usage` | array of TurnUsage | Phase 12 telemetry; one record per provider generate: `{ "prompt_tokens", "completion_tokens", "total_tokens", "latency_s", "at" }` (server-reported tokens + generation latency, `at` = unix epoch seconds). Recorded by the provider after each generate (`core.telemetry.record_usage`), serialized with the session, folded into summaries by `core.telemetry.session_usage(state)`, and rendered by `/cost` (tokens only — local inference is never metered) and the web sidebar readout |

`pending_approval` is enforced in code: `loop.step()` refuses to run until `resolve_approval()` clears it. It is never a convention. `pending_calls` exists so multi-call turns like `[run_code, echo]` don't silently drop the calls after the approval-gated one. Plan steps are bookkeeping only — every step that calls a tool still hits the hardcoded approval gate independently; there is **no plan-based approval bypass**.

### Permission modes (Phase 8)

| Mode | Behavior |
|---|---|
| `default` | Park a tool when `requires_approval` is true; run everything else (pre-Phase-8 behavior) |
| `plan` | Run only `read_only` tools; park every other call (propose, don't mutate) |
| `auto` | Auto-approve `requires_approval` tools; `HARDCODED_APPROVAL_TOOLS` (`run_code`) is still parked |
| `strict` | Park **every** tool call, read-only included |

The mode is read in `ToolRegistry._requires_approval(definition, state)`. The hardcoded `run_code` check runs first, so no mode can bypass it — only `resolve_approval(approved=True)` executes a parked `run_code` call. `dispatch()` is the sole enforcement point; the browser mirror is `requiresApproval()` in `web/engine.js`.

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

`web_search` backends: `web` → DuckDuckGo HTML, `news` → Google News RSS, `wikipedia` → Wikipedia Search API. In the browser, both tools proxy through `web/server.py` (`/api/search`, `/api/fetch`) to avoid CORS; neither requires approval and both are `read_only: true`. Both sit behind the SSRF destination guard (2026-10-20) in `tools/web_search.py`: http(s)-only scheme, destinations resolved and checked after DNS (private/loopback/link-local/metadata excluded), every redirect target re-checked, per-hop 1 MB byte cap + existing `max_chars` text cap.

**Networked — identical implementation shape across all platforms (remote HTTP/SSE, uniform MCP-remote, no subprocess transport issue), opt-in via `MCP_BASE_URL` (`send_email` migrated to native SMTP in the default registry, see its risk class below; `send_message` remains here):**

```json
{ "name": "send_email", "parameters": { "to": "string", "subject": "string", "body": "string", "attachments": "string[]?" }, "requires_approval": true }
{ "name": "send_message", "parameters": { "channel": "whatsapp|telegram", "to": "string", "text": "string" }, "requires_approval": true }
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

**External sends — own risk class (2026-10-20). `send_email` (native SMTP) with `requires_approval` always true, plus `ALWAYS_CONFIRM_TOOLS = {"send_email", "send_message"}` checked in `tool_registry.dispatch()`: no permission mode — `auto` included — bypasses the confirmation; only `resolve_approval(approved=True)` sends.** `send_email` moved from the opt-in MCP remote into the default registry (`tools/email.py`, config `AGENT_CORE_SMTP_*`); `send_message` stays MCP-only and also `requires_approval: true`.

**Cognitive (Phase 5) — identity, memory, planning. All `requires_approval: false`; `inspect_self`, `recall`, `make_plan` and `update_plan` are `read_only: true` (only `remember` writes, so `plan` mode parks it). Handlers are state-aware (`takes_state`), invoked as `handler(state, arguments)`. Stored memory lives at `~/.agent-core/memory/` (same traversal guard as sessions):**

```json
{ "name": "inspect_self", "parameters": {} }
{ "name": "remember", "parameters": { "key": "string", "content": "string", "kind": "fact|preference|lesson|session_summary?" } }
{ "name": "recall", "parameters": { "topic": "string?", "limit": "integer? 1-20" } }
{ "name": "make_plan", "parameters": { "goal": "string", "steps": "string[]" } }
{ "name": "update_plan", "parameters": { "step_id": "string", "status": "pending|in_progress|done|failed|skipped", "result": "string?" } }
```

Memory entry record: `{ "key", "content", "kind", "created_at", "source_session" }` — one JSON file per key. `new_agent_state()` seeds each session with a bounded recall (`~512` tokens, newest first) so knowledge survives CLI ↔ web resumes. `make_plan`/`update_plan` never bypass the approval gate: each plan step that calls a tool still hits the hardcoded gate in `dispatch()`.

**File & code intelligence (Phase 9) — rooted at `AGENT_CORE_FILE_ROOT` (default: repo root). Reads are `requires_approval: false` + `read_only: true`; writes are `requires_approval: true`:**
```json
{ "name": "read_file", "parameters": { "path": "string", "max_chars": "integer?" } }
{ "name": "write_file", "parameters": { "path": "string", "content": "string" }, "requires_approval": true }
{ "name": "edit_file", "parameters": { "path": "string", "old_string": "string", "new_string": "string" }, "requires_approval": true }
{ "name": "list_dir", "parameters": { "path": "string?" } }
{ "name": "glob", "parameters": { "pattern": "string", "root": "string?" } }
{ "name": "grep", "parameters": { "pattern": "string", "path": "string?", "max_results": "integer?" } }
```
`resolve_path()` resolves the target (symlinks included) and rejects anything that escapes the root; `edit_file` replaces exactly one occurrence and fails when `old_string` is absent or non-unique; all writes are atomic (temp file + `os.replace`). In the browser, `BROWSER_TOOLS` proxies these through `POST /api/file` (loopback/CSRF-guarded); `write_file`/`edit_file` are refused unless the body carries `approved: true`, which the worker only sends after the browser engine's own gate.

**Autonomy (Phases 10–11) — sub-agents, skills, tasks. All `requires_approval: false`; `list_skills`, `task_create`/`task_update`/`task_list` are `read_only: true`. `agent` is transport-registered (it needs a provider):**
```json
{ "name": "agent", "parameters": { "goal": "string", "max_turns": "integer?" } }
{ "name": "run_skill", "parameters": { "name": "string" } }
{ "name": "list_skills", "parameters": {} }
{ "name": "task_create", "parameters": { "description": "string" } }
{ "name": "task_update", "parameters": { "id": "task_N", "status": "pending|in_progress|done|failed|skipped", "result": "string?" } }
{ "name": "task_list", "parameters": {} }
```
A skill is a JSON file `{ "name", "description", "prompt", "tools"? }` under `skills/`; `run_skill` sets `tool_allowlist` and injects the prompt — the allowlist is a **denial in `dispatch()`, never an approval bypass**. `agent` runs a bounded nested loop (`MAX_SUBAGENT_TURNS=4`, one level deep, one at a time); a gated tool inside the sub-agent bubbles to this session's `pending_approval`. Tasks persist in `AgentState.tasks` and are managed by `task_*` / `/tasks`. The browser mirror exposes skills via `GET /api/skills` and mirrors the allowlist denial in `web/engine.js`.

## Observability (Phase 12)

`core/telemetry.py` is stdlib-only and local-only (nothing is collected, nothing phones home). The provider records one **TurnUsage** per generate into `AgentState.usage` — `record_usage(state, usage, latency_s)` where `usage` is the server-reported OpenAI usage object/dict the provider already captures (`stream_options.include_usage`):

```json
{
  "prompt_tokens": 1234,
  "completion_tokens": 567,
  "total_tokens": 1801,
  "latency_s": 3.4,
  "at": 1792460000.0
}
```

| Field | Type | Rules |
|---|---|---|
| `prompt_tokens` | int | server-reported prompt tokens for that generate (0 when the server does not report) |
| `completion_tokens` | int | server-reported completion tokens |
| `total_tokens` | int | server-reported total; `session_usage()` falls back to `prompt + completion` when a provider skips it |
| `latency_s` | float | generate wall time in seconds (feeds per-turn tok/s) |
| `at` | float | unix epoch seconds at call completion |

`session_usage(state)` folds the records into `{turns, prompt_tokens, completion_tokens, total_tokens, generate_seconds, tokens_per_second, last_tokens_per_second}` — the same numbers `docs/benchmarks.md` measures. Records serialize with the session (survive CLI ↔ web resumes). `/cost` renders the summary (tokens-only, always `$0.00` — local inference is never metered); `/context` adds a "Real usage (server-reported)" line beside the chars/4 estimate; `inspect_self` includes the summary. Pure summary/format logic mirrors to `web/telemetry.js` (`sessionUsage`/`formatTokens`/`formatCost`, byte-identical text).

`/doctor` (`run_doctor` / `format_doctor`) is host-only: llama-server `/health`, `/v1/models` first id vs `AgentState.model`, client `max_context_tokens` vs `EXPECTED_CTX_BY_MODEL` (the per-model `--ctx-size` table), Docker for `run_code` (`docker info`), and session/memory dir writability. Every probe is **injectable** (`urlopen=` / `probe=` / `CommandContext.doctor_probes`) so tests run with fake servers and daemons — a probe failure is reported as an `ok: false` check, never raised. In the browser the web UI runs `/doctor` server-side through `POST /api/sessions/<id>/command`.

## Enforcement

- **Windows (Python):** pydantic in `core/state.py`; jsonschema on `register()`.
- **Android (Kotlin):** `kotlinx.serialization` — same shape, same field names.
- **Web (JS):** zod or manual guards — same shape.
- Not "whatever the model happens to emit." Schema enforcement runs before any tool executes.

## Test fixtures

`test_smoke.py` uses the contracts above: an ordinary tool executes; `run_code` sets `pending_approval` and halts; `resolve_approval(approved=False)` clears state without executing; `resolve_approval(approved=True)` runs.
