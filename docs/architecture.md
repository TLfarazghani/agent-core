# Architecture

One model family (LFM2.5), three runtimes, one shared JSON contract. The core is portable; platforms are thin adapters.

## System diagram

```mermaid
graph TB
    subgraph Core["Shared Agent Core (portable, no platform code)"]
        ST[AgentState JSON]
        TR[ToolRegistry JSON Schema]
        PARSE["Tool-call parser<br/>&lt;|tool_call_start|&gt;...&lt;|tool_call_end|&gt; (ast-based)"]
        GATE["Approval gate<br/>hardcoded in dispatch(), not model-controlled"]
        LOOP["Loop: build prompt → generate →<br/>parse tool_call → gate check → execute → append tool result → repeat"]
        ST --> LOOP
        TR --> LOOP
        PARSE --> LOOP
        GATE --> LOOP
    end

    subgraph Win["Windows native (current)"]
        W1["llama.cpp llama-server<br/>GGUF, --jinja, /v1/chat/completions"]
        W2["run_code sandbox: Docker (docker-py)"]
    end

    subgraph And["Android native (planned port)"]
        A1["LEAP Android SDK (Kotlin/JNI)<br/>Conversation + ChatMessage + LeapFunctionCall"]
        A2["run_code: NOT available on-device —<br/>delegate to reachable Windows instance, or omit"]
    end

    subgraph Web["Browser / WebGPU (planned port)"]
        B1["Transformers.js v3+ → ONNX Runtime Web<br/>device: 'webgpu', WASM fallback"]
        B2["run_code sandbox: Pyodide (already tab-sandboxed)"]
    end

    Core -->|OpenAI-shape JSON over localhost| Win
    Core -->|Kotlin object, same schema serialized via Gson| And
    Core -->|same schema, in-memory JS object| Web
```

**Rule:** the agent loop, tool registry, state machine, and **approval gate** live in the Core and are transport-agnostic. Each platform only implements a thin adapter that turns `AgentState → provider request` and `provider response → parsed ToolCall[]`, plus its own `run_code` sandbox backend.

## Build order (forced by dependency)

```mermaid
graph LR
    P0["Phase 0 — Core<br/>AgentState + parser + registry + approval gate"] --> P1["Phase 1 — Networked tools<br/>search, email, messaging (MCP-remote)"] --> P2["Phase 2 — Local doc-gen<br/>docx/pptx"] --> P3["Phase 3 — Code execution + sandbox<br/>highest-risk, own subsystem"] --> P4["Phase 4 — Cross-platform parity<br/>Android + WebGPU ports"] --> P5["Phase 5 — Cognitive layer<br/>identity, memory, planning, reflection"] --> P6["Phase 6 — Context window<br/>token budget for long/resumed sessions"]
```

## Module layout

```
agent-core/
  schemas/                 # authoritative JSON Schemas (contracts)
    tool_definition.schema.json
    chat_message.schema.json
    agent_state.schema.json
  core/                    # portable agent core (Python, no platform code)
    __init__.py
    state.py               # pydantic AgentState / ChatMessage / PendingApproval / Plan
    parser.py              # ast-based Pythonic tool-call extraction, no eval (stdlib only)
    tool_registry.py       # schema validation on register(), approval gate in dispatch()
    loop.py                # step() / resolve_approval() / retry-once, platform-agnostic
    context.py             # token budget: estimate_tokens (chars/4) + trim_to_budget (Phase 6)
    meta.py                # AGENT_NAME, agent_bio, inspect_self (Phase 5 identity)
    memory.py              # ~/.agent-core/memory/ JSON store, traversal guard, bounded recall
    planner.py             # make_plan / update_plan on AgentState.plan
    reflection.py          # one-line lessons written to memory on terminal turns
  tools/                   # real tool handlers
    registry.json          # all tool definitions (echo, run_code, networked, doc-gen, cognitive)
    remote.py              # McpClient — stdlib JSON-RPC MCP tools/call transport (portable)
    __init__.py            # register_networked_tools / register_docgen_tools / register_runcode_tool / register_cognitive_tools
    cognitive.py           # state-aware handlers: inspect_self / remember / recall / make_plan / update_plan
    create_docx.py         # python-docx handler (Phase 2)
    create_pptx.py         # python-pptx handler (Phase 2)
    run_code.py            # Docker sandbox handler, watchdog timeout (Phase 3)
  windows/                 # Windows transport (llama.cpp) — Phase 4
    server_config.ps1      # llama-server launch params
    orchestrator.py        # LlamaCppProvider: AgentState → OpenAI v3 request → ChatMessage
  cli.py                   # REPL: renders AgentState, streams text, resolves approvals
  Core-agent.bat           # one-click launcher: llama-server + Docker check + web UI + browser
  web/                     # Web UI + agent API server (stdlib only)
    server.py              # ThreadingHTTPServer; SSE API (token/tool_call/approval/done); sessions
    index.html             # SPA shell
    style.css              # dark local-first theme (approval amber, tool gray)
    app.js                 # renders AgentState, tool cards, approval modal, session sidebar
  web/context.js           # JS port of core/context.py (Phase 6)
  web/engine.js            # in-browser loop, JS port of core/loop.py (+ plan/retry/lesson)
  web/memory.js            # JS port of core/memory.py (localStorage/IndexedDB backend)
  web/planner.js           # JS port of core/planner.py
  web/worker.js            # module worker: Transformers.js + Pyodide + browser tools
  core/sessions.py         # shared session lifecycle: system prompt, save/load/list/delete
  android/                 # planned port — structure reserved
    AgentCore.kt           # Kotlin port of core/ control flow
  test_smoke.py            # Phase 0 smoke test (approval gate + retry-once + reflection)
  test_networked_tools.py  # Phase 1 (stdlib mock MCP-remote)
  test_docgen_tools.py     # Phase 2 (real .docx/.pptx round-trip)
  test_runcode.py          # Phase 3 (fake docker client)
  test_orchestrator.py     # Phase 4 (stubbed OpenAI client)
  test_web.py              # Web UI (fake provider + ephemeral server)
  test_context.py          # Phase 6 (token budget invariants)
  test_memory.py           # Phase 5 (memory persistence + traversal guard)
  test_planner.py          # Phase 5 (plan create/update/serialize)
  test_meta.py             # Phase 5 (bio block + inspect_self accuracy)
  requirements.txt         # pydantic, jsonschema, python-docx, python-pptx, docker, openai
  docs/                    # this documentation set
  models/                  # GGUF weights (gitignored)
  vendor/                  # llama.cpp binaries (gitignored)
```

## Data flow (Windows path, Phase 4)

```mermaid
sequenceDiagram
    participant CLI as cli.py
    participant CORE as core/loop.py
    participant ORCH as windows/orchestrator.py
    participant SERVER as llama-server (--jinja)
    participant REG as core/tool_registry.py

    CLI->>CORE: step(state)
    CORE->>ORCH: generate(state, tools=)
    ORCH->>SERVER: POST /v1/chat/completions (tools=)
    SERVER-->>ORCH: message with tool_calls[]
    ORCH-->>CORE: ChatMessage (OpenAI shape)
    CORE->>CORE: parse / validate function_calls
    CORE->>REG: dispatch(call)
    alt requires_approval=True
        REG-->>CORE: set pending_approval, HALT
        CORE-->>CLI: pending approval (loop refuses step)
        CLI->>CORE: resolve_approval(approved=bool)
    else ordinary tool
        REG-->>CORE: tool result appended
    end
    CORE-->>CLI: updated state
```

## Approval gate (enforced in code, not convention)

1. Model emits `run_code(...)`.
2. `dispatch()` sees `requires_approval=True` → sets `AgentState.pending_approval`, loop halts.
3. UI surfaces the pending call to the human.
4. `resolve_approval(state, registry, approved=bool)` — only on `approved=True` does the Docker container / Pyodide worker actually run.

`pending_approval` is non-null exactly when a `requires_approval` tool call is waiting. The loop refuses to `step()` again until `resolve_approval()` clears it.

## Cognitive layer (Phase 5)

The agent gets a brain and a self. Everything is core-level, stdlib-only, and transport-agnostic — the web/Android targets reuse the same contract (JS ports: `web/memory.js`, `web/planner.js`, plus plan/retry/lesson in `web/engine.js`).

- **Identity:** `core/meta.py` owns the single shared constant `AGENT_NAME = "Agent Core"`. `agent_bio(state, registry)` produces the identity block (name, target, model, context + turn budgets, live tool list) injected as a system message at session creation (CLI, web SSE server, and `web/worker.js`), so "who are you / what can you do" is answered from fact. `inspect_self` (no approval) returns a live snapshot.
- **Memory:** `core/memory.py` stores one JSON file per key under `~/.agent-core/memory/` with the same traversal guard as sessions. `remember` / `recall` (no approval) read/write persistent facts, preferences, and lessons; `new_agent_state()` seeds each session with a bounded recall (≈512 tokens, newest first).
- **Planning:** `AgentState.plan` (`{goal, steps:[{id, description, status, result}]}`) with `make_plan` / `update_plan` (no approval). Plan steps are **bookkeeping only** — each step that calls a tool still hits the hardcoded approval gate independently; there is no plan-based approval bypass.
- **Reflection + retry-once:** `core/reflection.py` writes a one-line `kind=lesson` memory on terminal turns when a tool failed or the user rejected a call. `core/loop.py` implements retry-once: after a tool failure the loop allows exactly one more generate+execute cycle per user turn (`RETRY_ONCE_LIMIT = 1`, `retry_count` reset on each new user message), then gives up cleanly. A rejected call is never auto-retried — `resolve_approval(approved=False)` stays the only way through.

State-aware tool handlers are marked with `takes_state` and invoked as `handler(state, arguments)`; `ToolRegistry.execute(call, state=None)` remains backward-compatible for plain handlers.

## Sandbox decisions (Phase 3)

| Tool | Windows | Android | WebGPU |
|---|---|---|---|
| create_docx | python-docx | Apache POI (heavy — reconsider for v1) | docx.js |
| create_pptx | python-pptx | Apache POI (heavy — reconsider for v1) | pptxgenjs |
| run_code | **Docker** (docker-py, `--network none` + limits) | **None** (delegate to Windows or omit) | **Pyodide** (tab-sandboxed) |

## Model selection (research doc §2)

Start with **LFM2.5-1.2B-Instruct** everywhere — one model, one prompt format, one set of quirks across all targets. Split models per platform only after measured data justifies it.

| Model | Target | Notes |
|---|---|---|
| LFM2.5-1.2B-Instruct | Windows default, Android mid-tier | native tool-calling; **the default** |
| LFM2.5-1.2B-Thinking | multi-step planning | higher latency; don't default |
| LFM2.5-2.6B | Windows daily-driver | needs ~8GB laptop/desktop |
| LFM2.5-8B-A1B (MoE) | Windows power tier / WebGPU | big first-load download |
| LFM2.5-350M / 230M | WebGPU / low-RAM | needs fine-tune to be usable for tools |

## Protocol decision

LFM2.5 emits native **Pythonic** calls (`func(arg="value")`) between the sentinel tokens. `core/parser.py` uses Python's `ast` module to extract them safely (no eval). On Windows, `--jinja` handles parsing natively; the parser covers raw paths (WebGPU, fallbacks).
