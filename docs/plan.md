# Build Plan

Full phased build plan for agent-core. Source: `local-agent-lfm25-research.md` (2026-08-13, updated 2026-08-17), verified against Liquid Docs and the host machine.

## Locked decisions (2026-08-17)

| Decision | Choice |
|---|---|
| Scope | General-purpose local assistant (search, email, messaging, docx/pptx, code exec) — full feature set, built in dependency order |
| Model | `LiquidAI/LFM2.5-1.2B-Instruct-GGUF`, file `LFM2.5-1.2B-Instruct-Q4_K_M.gguf` (697MB) |
| Server | Prebuilt llama.cpp CUDA binary (`llama-b10456-bin-win-cuda-12.4-x64.zip`) — NOT built from source, NOT Ollama |
| Parser | ast-based Pythonic extraction (`<|tool_call_start|>...<|tool_call_end|>`), no eval, no regex |
| Approval gate | Hardcoded in `core/tool_registry.dispatch()`, never model-decided |
| run_code sandbox | Windows: Docker (docker-py, `--network none` + limits); WebGPU: Pyodide; Android: none (platform ceiling) |
| First target | Windows — only platform where `run_code` has a real answer today |

## Environment facts (measured 2026-08-17)

- Python 3.13.5 on PATH, but `pip` resolves to a Python 3.10 site-packages → **mandatory venv**
- No `cmake`, no MSVC `cl` → use official prebuilt CUDA binaries
- NVIDIA RTX 4060 Ti 8GB, CUDA 12.9 toolkit (cu12.4 prebuilt binary runs on the 12.9 driver)
- Ollama 0.32.9 installed but rejected: research doc §4.1 flags the `output_norm` GGUF sync trap
- Downloaded: llama.cpp `b10456` cu12.4 build → `vendor\llama\`; GGUF Q4_K_M → `models\`

## Server launch flags (canonical)

```powershell
vendor\llama\llama-server.exe `
  -m models\LFM2.5-1.2B-Instruct-Q4_K_M.gguf `
  --alias "LFM2.5-1.2B-Instruct" `
  --threads -1 --n-gpu-layers 99 --ctx-size 32768 `
  --port 8001 --temp 0.1 --top-k 50 --top-p 0.1 `
  --repeat-penalty 1.05 --jinja
```

`--jinja` applies LFM2.5's own chat template and emits structured `tool_calls` in the OpenAI response shape.

## Phase 0 — Core (agent loop, parser, registry, approval gate)

Target state per research doc §6:

- [x] `schemas/tool_definition.schema.json` — name, description, `requires_approval`, parameters
- [x] `schemas/chat_message.schema.json` — role, string content, `tool_call_id`, snake_case `function_calls`
- [x] `schemas/agent_state.schema.json` — session_id, target, model, messages, max_turns, turn_count, `pending_approval`
- [x] `core/state.py` — pydantic `AgentState` / `ChatMessage` / `PendingApproval`
- [x] `core/parser.py` — ast-based Pythonic tool-call extraction, no eval
- [x] `core/tool_registry.py` — schema validation on `register()`, approval gate in `dispatch()`
- [x] `core/loop.py` — `step()` / `resolve_approval()`, platform-agnostic
- [x] `test_smoke.py` — proves ordinary tool call executes, `run_code` halts until approval, rejection clears without executing
- [x] `requirements.txt` — `pydantic`, `jsonschema`

Approval flow (enforced in `core/loop.py` + `core/tool_registry.py`, not per-platform):
1. Model emits `run_code(...)`.
2. `dispatch()` sees `requires_approval=True` → sets `AgentState.pending_approval`, loop halts.
3. UI surfaces the pending call to the human.
4. `resolve_approval(state, registry, approved=bool)` — only on `approved=True` does the sandbox actually run.

**Gate:** `test_smoke.py` passes: ordinary call executes, `run_code` halts, rejection clears without executing.

## Phase 1 — Networked tools (uniform MCP-remote)

- [x] `tools/remote.py` — `McpClient` (stdlib-only JSON-RPC over HTTP, MCP `tools/call` shape; ports to JS/Kotlin)
- [x] `tools/send_email.py` — `send_email(to, subject, body, attachments?)`
- [x] `tools/send_message.py` — `send_message(channel, to, text)` (whatsapp|telegram)
- [x] Uniform MCP-remote transport so all three platforms share the identical HTTP implementation shape (no subprocess transport issue)
- [x] Tool tests with mocked remotes (`test_networked_tools.py` — stdlib HTTP mock, no real network)

**Gate:** each networked tool dispatch returns schema-valid results with a mocked remote. **PASSED 2026-08-17** (6/6).

## Phase 1b — Local web search (keyless, no MCP remote) **PASSED 2026-08-18**

- [x] `tools/web_search.py` — **local** `web_search(query, kind?, max_results?)` + `fetch_url(url, max_chars?)`: real, keyless, stdlib-only. `kind="web"` → DuckDuckGo HTML; `kind="news"` → Google News RSS; `kind="wikipedia"` → Wikipedia Search API. `fetch_url` returns a trimmed plain-text page extract. Injectable `urlopen` so tests never touch the network (mirrors run_code's injectable docker client)
- [x] Registered in `default_registry()` on Windows (`web/server.py` + `cli.py`) — **no MCP_BASE_URL required**; `send_email`/`send_message` remain opt-in networked
- [x] Browser proxy: `web/server.py` `GET /api/search` + `GET /api/fetch` (avoids CORS), and `web/worker.js` in-browser `web_search`/`fetch_url` tools route through them
- [x] Tests: `test_web_search.py` (8 tests, fake urlopen: DDG/News/Wikipedia parsing, fetch text extraction, arg validation) + 2 web-proxy tests in `test_web.py`. Live-verified 2026-08-18: real DDG results, real Google News headlines, real Wikipedia entries, real page fetch

## Phase 2 — Local doc-gen (per-platform backends)

- [x] `tools/create_docx.py` — `create_docx(title, sections)` via python-docx (writes to output dir, unique filenames)
- [x] `tools/create_pptx.py` — `create_pptx(title, slides)` via python-pptx
- [x] `tools/_paths.py` — shared output-dir / slug / unique-filename helpers
- [x] Backends: Windows python-docx/python-pptx; Web docx.js/pptxgenjs; Android Apache POI (heavy — reconsider for v1)
- [x] Tool tests (`test_docgen_tools.py` — writes real files to temp dir, reopens and verifies content)

**Gate:** generated .docx/.pptx validate; tools registered and dispatched. **PASSED 2026-08-17** (6/6).

## Phase 3 — Code execution + sandbox (highest risk)

| Mechanism | Isolation | Android | Browser | Install footprint | Licensing |
|---|---|---|---|---|---|
| **Docker (chosen, Windows)** | Strong (namespace/cgroup) | No | N/A | Docker Desktop required | Free under ~250 employees/$10M |
| Windows Sandbox API | Strong (VM-lite) | No | N/A | Windows Pro/Enterprise only | Free |
| AppContainer / job object | Weak-medium | No | N/A | None | Free |
| **Pyodide (chosen, browser)** | Strong (WASM) | N/A | Yes, no install | None | Free |

- [x] `tools/run_code.py` — `run_code(language, code, timeout_seconds)`, `requires_approval: true` hardcoded
- [x] Windows handler: docker-py, `--network none`, memory/cpu limits, watchdog-thread timeout (kills container)
- [x] Injectable docker client so the test suite runs without a daemon (`test_runcode.py` — fake client)
- [x] Tests: approval halts loop, approval runs, rejection clears, timeout kills, nonzero exit, unsupported language
- [x] Real-Docker verification (2026-08-17): python `print(2+2)` → `4`, outbound socket to 1.1.1.1 → **NETWORK: OFF**, bash echo OK, `sleep 300` killed at timeout, no stray containers
- [ ] Web handler: Pyodide worker (already tab-sandboxed, zero install) — deferred to Phase 4 web port
- [ ] Android: no sandbox — delegate to reachable Windows instance or omit; tracked as open platform-ceiling item

**Gate:** `run_code` executes only after `resolve_approval(approved=True)`; sandbox limits enforced. **PASSED 2026-08-17** (8/8 unit + real Docker check).

## Phase 4 — Full cross-platform parity

- [x] `windows/orchestrator.py` — wraps core with OpenAI v3 client (llama.cpp `--jinja`); maps camelCase `tool_calls` → snake_case `function_calls`; streaming + usage capture (`stream_options.include_usage`)
- [x] `cli.py` — REPL per `docs/ui-ux-design.md`: streaming text, tool cards, `[y/N]` approval prompt (default N), `/new /resume /tools /approve /reject /quit`, session persistence to `~/.agent-core/sessions/`, cp1252-safe when piped
- [x] `test_orchestrator.py` — 9 unit tests (message mapping, tools wrapping, malformed-args degradation) with a stubbed client; no server needed
- [x] Real E2E on llama-server: `run_code` tool call → approval gate → Docker `print(7)` → `7` → final answer
- [x] **Web UI (v1)** — `web/server.py` (stdlib `ThreadingHTTPServer` + SSE: `token`/`tool_call`/`tool_result`/`approval`/`done` events; endpoints for sessions/tools/health/approve/reject), `web/index.html` + `style.css` + `app.js` (vanilla SPA: chat canvas, tool cards, approval modal with `a`/`r`/Esc keys, session sidebar, model status), `test_web.py` (8 tests, fake provider, no llama needed). Verified live: real model streams tokens; run_code → approval → Docker `99` → streaming reply.
- [x] `Core-agent.bat` — one-click launcher: starts llama-server if down, checks Docker, starts web UI, opens browser
- [x] `web/parser.js` — line-for-line JS port of `core/parser.py` (UMD: CommonJS / browser global / worker global; tokenizer + recursive-descent parser, no eval; `ParserSyntaxError` vs `ParserError` split matches Python's lenient/strict semantics). Verified by `test_parser_js.py` (4 parity tests, Node harness) — **all pass**
- [x] `web/engine.js` — in-browser loop, pure JS port of `core/loop.py` (`step`/`start`/`resolve_approval`, hardcoded approval gate, turn cap). Verified by `test_webgpu_engine.mjs` (7 tests, fake provider, real `web/parser.js`, no WebGPU) — **all pass**
- [x] `web/worker.js` — module worker (ESM): Transformers.js v4.2.0 (`LiquidAI/LFM2.5-1.2B-Instruct-ONNX`, `device:"webgpu"`, `dtype:"q4"`, WASM fallback) + Pyodide v314.0.5 for `run_code`; in-browser `web_search`/`fetch_url` tools proxy through `/api/search` + `/api/fetch`; streams `token`/`tool_call`/`tool_result`/`approval`/`error`/`done` mirroring the SSE server. WebGPU inference itself needs a real browser to verify
- [x] Web UI transport toggle (`web/index.html` + `app.js`): `local` (llama-server SSE) ↔ `in-browser` (`new Worker("worker.js", {type:"module"})`, in-memory session, Pyodide approval gate) — model load requires a real browser + WebGPU
- [ ] `android/AgentCore.kt` — Kotlin port of core control flow (ported, not reinvented) — deferred, gate passed
- [x] Measure tok/s + tool-call accuracy; record in `docs/benchmarks.md` (see below)

**Measurements (2026-08-17):** ~215 tok/s mean (RTX 4060 Ti, Q4_K_M); tool-call accuracy **15/18 = 83%** (100% on well-specified prompts, deterministic failure on under-specified ones). Full tables in `docs/benchmarks.md`.

**Gate:** Windows path shipped and measured. Per research doc §7, no fourth target / second model before this gate. **PASSED 2026-08-17** — Android/Web ports may begin.

## Phase 5 — Agent Core: self, memory, planning, reflection (the cognitive layer)

**The agent gets a brain and a self.** Agent Core's loop, registry, and approval gate are shipped (Phase 4) and its context window is budgeted (Phase 6). Phase 5 layers persistent cognition on top — an identity the agent can introspect, cross-session memory, explicit planning, and self-correction. Everything is core-level and stdlib-only so it ports to the web (`web/*.js`) and Android (`AgentCore.kt`) targets with the same contract, exactly like the parser. The agent's name is a shared constant — **Agent Core** — everywhere.

### 5.1 Identity — the "Agent Core" persona + self-introspection

- [x] `core/meta.py` — stdlib-only: `agent_bio(state, registry)` returns the Agent Core identity block (name, target, model, context budget, turn budget, live tool list) that gets injected into the system prompt, so the agent answers "who are you / what can you do" from fact, not hallucination.
- [x] `core/sessions.py` `SYSTEM_PROMPT` updated: the assistant introduces itself as **Agent Core** (shared `AGENT_NAME` constant used by CLI, web SSE server, and `web/worker.js`) and states its capabilities + limits before the existing tool-selection rules.
- [x] `inspect_self` tool (`requires_approval: false`) — returns a live snapshot: `session_id`, `target`, `model`, `turn_count`/`max_turns`, estimated context usage (Phase 6 estimate), `pending_approval` state, and available tool names. Registered in `default_registry`; JS mirror added to `web/engine.js` + `web/worker.js` `BROWSER_TOOLS`.
- [x] **Gate:** Agent Core answers identity questions from its own bio block; `inspect_self` reports accurate session/context/tool state in CLI and web.

### 5.2 Long-term memory (cross-session, persistent)

- [x] `core/memory.py` — JSON store at `~/.agent-core/memory/` mirroring `core/sessions.py`'s layout; same traversal-guard on keys (reject `/`, `\`, `:`, `.`/`..`). Entries: `{key, content, kind: fact|preference|lesson|session_summary, created_at, source_session}`.
- [x] `remember(key, content, kind)` and `recall(topic)` tools (`requires_approval: false`) — write facts/preferences/lessons that persist across sessions; `recall` does keyword matching over the memory dir.
- [x] `new_agent_state()` seeds each new session with a bounded `recall` of session-relevant memories (fits the Phase 6 context budget), so knowledge survives CLI ↔ web resumes.
- [x] JS parity: `web/memory.js` (localStorage/IndexedDB backend, same tool contract) + `web/worker.js` registration.
- [x] **Gate:** a fact saved in one session is recallable in a fresh CLI session *and* a fresh web session; keys with path-traversal rejected.

### 5.3 Planning & task tracking

- [x] `core/planner.py` + `AgentState.plan` field (`{goal, steps: [{id, description, status, result}]}`) — pydantic model + `schemas/agent_state.schema.json` (sync `docs/data-contracts.md` per AGENTS.md).
- [x] `make_plan(goal, steps)` + `update_plan(step_id, status)` tools (`requires_approval: false`); plan renders in CLI and web UI alongside tool cards, survives session serialize/resume.
- [x] Loop interaction documented: each plan step that calls a tool still hits the hardcoded approval gate independently (no plan-based approval bypass); turn budget vs plan length interplay noted in `docs/ui-ux-design.md`.
- [x] JS parity: `web/planner.js` port + `web/engine.js` plan state.
- [x] **Gate:** a multi-step task ("research X, draft a docx, summarize it") executes through an explicit plan with visible per-step status in both UIs.

### 5.4 Reflection & self-correction

- [x] `core/reflection.py` — on a terminal turn, emit a one-line lesson into memory (`kind=lesson`) when a tool failed or the user corrected the agent; bounded so it never blocks the loop.
- [x] Retry-once: on a tool error the loop allows one corrected re-attempt within the same turn (still approval-gated for `run_code`; never auto-retries a rejected call).
- [x] JS parity: `web/engine.js` reflection + retry-once hook.
- [x] **Gate:** a failing tool call is retried exactly once with corrected args then gives up cleanly; the lesson persists in memory.

### 5.5 (stretch, gated) Reasoning depth

Only if `docs/benchmarks.md` long-session measurement shows a real deficit: a `think` step or the LFM2.5-1.2B-Thinking checkpoint. Not part of the 5.1–5.4 gate.

### Tests & docs sync

- [x] `test_memory.py` (persistence, traversal guard, cross-session recall), `test_planner.py` (create/update/serialize round-trip), `test_meta.py` (bio block, `inspect_self` accuracy), reflection + retry-once tests in `test_smoke.py`/orchestrator suite, JS parity in `test_webgpu_engine.mjs`.
- [x] Docs: `AGENTS.md` built list, `docs/architecture.md` module layout + data flow, `docs/data-contracts.md` (plan/memory), `README.md` status table.

### Gate

5.1–5.4 all pass on the Windows path with unit + JS-parity tests; identity, memory, planning, and reflection verified in CLI and web.

## Phase 6 — Context window management (unbounded session growth)

**Problem (applies to the shipped path today, not gated on Phase 7):** `windows/orchestrator.py` (`__call__`) and `web/engine.js` (`provider.generate(state.messages)`) send the **full** `state.messages` list every turn. Sessions serialize and resume from `~/.agent-core/sessions/` across CLI and web, so history grows until it exceeds the model's context window — 32768 on the 1.2B server config, 128K on 2.6B/8B. Today nothing trims, so a resumed long session eventually either errors at the server or gets silently corrupted as llama.cpp drops tokens. Token budget must be explicit, not emergent.

### Invariants (do not violate)

- **Never split a tool/assistant pair.** OpenAI requires a `tool` message to immediately follow the assistant `tool_calls` message it answers; trimming one but keeping the other produces a request llama-server rejects. Trimming must operate on whole turns.
- Keep the **system prompt** and the **in-flight (last) user message** unconditionally.
- The approval gate and `pending_calls` reference message ids that must still exist — trim only history the loop has already fully consumed; never touch a message referenced by a live `pending_approval`.
- Core stays the owner of the state machine; trimming is a **pure, platform-agnostic helper** that providers call before building the wire request (platforms stay thin adapters — they don't decide policy).
- Stdlib-only for parity: no `tiktoken` — a `chars/4` heuristic must be portable to `web/parser.js`/`engine.js`/`AgentCore.kt`. Where the server reports ground truth, use it: llama-server already returns `usage.prompt_tokens` (captured in `self.last_usage` via `stream_options.include_usage`) — trim to budget and confirm against actual prompt tokens.

### Plan

- [x] `core/context.py` — pure, stdlib-only: `estimate_tokens(messages)` (chars/4) and `trim_to_budget(messages, budget_tokens)` returning `(kept, dropped)`. Operates on whole turns (an assistant `tool_calls` message + the `tool` results that follow it are trimmed or kept together); never drops system or the last user message.
- [x] Provider config gains `max_context_tokens` (default 32768, matching `--ctx-size`; 131072 when the 2.6B config lands — wire it to the same env/arg source as Phase 7's model select). `LlamaCppProvider.__call__` applies `trim_to_budget` to the OpenAI-mapped messages before the request; optionally assert `last_usage.prompt_tokens <= max_context_tokens` and log a warning on overflow.
- [x] Session storage is **unchanged**: the full history still persists to disk (users can still read it); only the wire request is trimmed. Optionally record the last-sent token count on `AgentState` for UI surfacing.
- [x] Web parity: `web/context.js` — JS port of `core/context.py` (same contract as `parser.js`/`engine.js`); `web/engine.js` trims before `provider.generate`; `web/worker.js` passes the ONNX model's real ctx.
- [ ] **Phase 6b (opt-in, after base lands): summarization compaction.** When dropping would discard genuinely-needed context, ask the model to summarize the dropped turns into a replacement system message (one extra generate turn). Only ship if base trimming provably loses too much on long sessions — measure first.
- [x] Tests: `test_context.py` (token estimate, budget enforcement, never splits a tool/assistant pair, keeps system + last user, budget-smaller-than-one-message boundary); orchestrator test with a fake response whose usage exceeds budget proving the request was trimmed and remains schema-valid; `test_webgpu_engine.mjs` parity test for the same invariants.

**Gate:** a long-session E2E — resume a session past 32K prompt tokens and confirm no server error and tool calls still correlate. Record the token-budget decision (trim-threshold vs `max_context_tokens`) in `docs/benchmarks.md`.

## Phase 7 — Second model (2.6B → 8B-A1B) and the fourth-target gate

The Windows shipped gate **PASSED 2026-08-17**, so this item moves out of scope-hold and into a plan. Order is forced: 2.6B first (low risk), 8B-A1B second (MoE arch risk), fourth target only after both model gates ship.

### Why 2.6B first

| | 2.6B (primary) | 8B-A1B (secondary) |
|---|---|---|
| GGUF | `LFM2.5-2.6B-Q4_K_M.gguf` (1.67GB) | `LFM2.5-8B-A1B-Q4_K_M.gguf` (5.16GB) |
| Architecture | dense (same family as 1.2B) | **MoE `lfm2moe`** |
| VRAM on 8GB card | comfortable | tight — needs `-c` reduction (5.2GB weights + KV) |
| Binary risk | none (existing b10456 + `--jinja`) | must verify current build loads `lfm2moe` (Ollama needed ≥0.17.1-rc0 for this arch; llama.cpp compat unverified) |
| Docs sampling | `--temp 0.1 --top-k 50 --repeat-penalty 1.1` | `--temp 0.2 --top-k 80 --repeat-penalty 1.05`; has ` thinking` CoT |
| Context | 128K (`-c 131072`, needs `-fa on`) | 128K — reduce for 8GB |

2.6B is the explicit follow-up already recorded in `docs/benchmarks.md` ("re-measure with the 2.6B model once the second-model gate opens").

### Plan

- [ ] Make the model selectable, not hardcoded: `windows/server_config.ps1` takes the model path/alias/ctx as a parameter (env or arg, default 1.2B so nothing breaks); `core/sessions.py` `new_agent_state()` seeds `model` from the same source; `Core-agent.bat` label + error text updated
- [ ] Download: `.venv\Scripts\hf.exe download LiquidAI/LFM2.5-2.6B-GGUF LFM2.5-2.6B-Q4_K_M.gguf --local-dir models`
- [ ] Launch 2.6B on the canonical flags (`--jinja`, `-c 131072`, `-fa on`, `-ngl 99`, docs sampling); verify a `tool_calls` turn against the existing tool set
- [ ] **Re-run the full benchmark suite** (`docs/benchmarks.md` §2 tool-call accuracy, §1 tok/s, incl. the previously-failing under-specified prompts) with 2.6B — this is the entire point: 1.2B fails deterministically on under-specified prompts; record 2.6B vs 1.2B side-by-side
- [ ] Decide default on data: switch Windows default to 2.6B only if tool-call accuracy improves (≥ the 83% bar) and interactive tok/s stays ≥ 50; else keep 1.2B default and ship 2.6B as opt-in
- [ ] Gate A: 2.6B measured and either defaulted or documented as opt-in
- [ ] 8B-A1B: verify current `vendor\llama` b10456 build loads `lfm2moe` (else bump the pinned prebuilt binary — still never build from source); download `LFM2.5-8B-A1B-Q4_K_M.gguf`; find the max ctx that fits 8GB VRAM; re-measure; ship as power-tier opt-in
- [ ] Gate B: 8B-A1B runs on the same loop/tool set with `--jinja` tool calls; VRAM ceiling documented
- [ ] Docs sync: `AGENTS.md` model/commands, `docs/plan.md`, `docs/benchmarks.md` pinned versions + measurements, `README.md` status table

### Fourth target

Not defined by the research doc — the three targets are Windows, Android, WebGPU. Opening a fourth target is gated on the second model shipping (both Gates A and B), and its first candidate would be macOS via MLX (first-party 8-bit MLX exists for every LFM2.5 checkpoint; it's the only remaining first-party runtime). No fourth target work begins before Gate B.

### Gate

Second-model gates (A, B) recorded in `docs/benchmarks.md`; Windows default decision made on measured data, not vibes.

## Traps

1. Ollama `output_norm` GGUF trap → direct llama.cpp binary + direct HF GGUF (never Ollama).
2. `--jinja` is mandatory for OpenAI-shaped `tool_calls`.
3. Python/pip version mismatch → everything inside `.venv`.
4. Pin exact binary + model versions for reproducible ports.
5. Keep `core/parser.py` dependency-free — it is the cross-language contract.
6. Android `run_code` has no sandbox story — a platform ceiling, not a forgotten task.
