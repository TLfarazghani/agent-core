# Build Plan

Full phased build plan for agent-core. Source: `local-agent-lfm25-research.md` (2026-08-13, updated 2026-08-17), verified against Liquid Docs and the host machine.

**Status (2026-10-19):** completed & gates passed — phases 0, 1, 1b, 2, 3, 5, 6, 7, **8**. Phase 4 Windows path shipped but its Android port and in-browser WebGPU model are unverified; Phase 6b compaction is not built. **Phases 9–12 below are planned, not built** (no code). Verified test run: 197 pass (165 pytest + 28 JS engine/context/Phase-8 + 4 parser parity).

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

- [x] Make the model selectable, not hardcoded: `windows/server_config.ps1` takes the model path/alias/ctx as a parameter (env or arg, default 1.2B so nothing breaks); `core/sessions.py` `new_agent_state()` seeds `model` from the same source; `Core-agent.bat` label + error text updated
- [x] Download: `.venv\Scripts\hf.exe download LiquidAI/LFM2.5-2.6B-GGUF LFM2.5-2.6B-Q4_K_M.gguf --local-dir models` — **done 2026-08-19** (1.67GB)
- [x] Launch 2.6B on the canonical flags (`--jinja`, `-c 131072`, `-fa on`, `-ngl 99`, docs sampling); verify a `tool_calls` turn against the existing tool set — **verified: `make_plan` with goal+3 steps emitted and executed**
- [x] **Re-run the full benchmark suite** (`docs/benchmarks.md` §2 tool-call accuracy, §1 tok/s, incl. the previously-failing under-specified prompts) with 2.6B — **recorded 2026-08-19: 12/15 = 80% vs 1.2B 15/18 = 83%**; new script `benchmark_tool_accuracy.py` (minimal/full registry modes)
- [x] Decide default on data: **KEEP 1.2B default** — 2.6B accuracy did not improve past the 83% bar (its under-specified-prompt failure is a deterministic hallucinated `run_code` instead of 1.2B's ask-for-clarification); tok/s ~110 ≥ 50. **2.6B ships as opt-in** (`AGENT_CORE_MODEL=LFM2.5-2.6B`, `AGENT_CORE_MAX_CONTEXT_TOKENS=131072`)
- [x] Gate A: 2.6B measured and shipped as documented opt-in — **PASSED 2026-08-19**
- [x] 8B-A1B: verify current `vendor\llama` b10456 build loads `lfm2moe` (else bump the pinned prebuilt binary — still never build from source); download `LFM2.5-8B-A1B-Q4_K_M.gguf`; find the max ctx that fits 8GB VRAM; re-measure; ship as power-tier opt-in — **DONE 2026-08-19**: `lfm2moe` compiled into `llama.dll` (no bump); Q4_K_M downloaded (5.16GB); **full 128K train ctx fits** (7740/8188 MiB, table `CtxSize` 32768→128000); tool-call acc **80%** @ ~150 tok/s; **shipped as power-tier opt-in**
- [x] Gate B: 8B-A1B runs on the same loop/tool set with `--jinja` tool calls; VRAM ceiling documented — **PASSED 2026-08-19**. Side note: the OpenAI-style request overrides server sampling flags, so the orchestrator now mirrors each model's table sampling via `default_sampling(model)` (`SAMPLING_BY_MODEL`) — without it 8B-A1B's run_code collapsed to 0/5 (JSON-text calls), with it 5/5.
- [x] Docs sync: `AGENTS.md` model/commands, `docs/plan.md`, `docs/benchmarks.md` pinned versions + measurements, `README.md` status table — **ALL DONE 2026-08-19**

### Fourth target

Not defined by the research doc — the three targets are Windows, Android, WebGPU. Opening a fourth target is gated on the second model shipping (both Gates A and B), and its first candidate would be macOS via MLX (first-party 8-bit MLX exists for every LFM2.5 checkpoint; it's the only remaining first-party runtime). No fourth target work begins before Gate B.

### Gate

Second-model gates (A, B) recorded in `docs/benchmarks.md`; Windows default decision made on measured data, not vibes.

## Phases 8–12 — Claude Code architecture review (Phase 8–9 built; 10–12 planned)

**Source:** an architectural review of the leaked Claude Code source (`codeaashu/claude-code`, leaked 2026-03-31). That repository archives **unlicensed, proprietary** Anthropic code, so Phases 8–12 borrow **patterns only, never source**. They are the subset of its architecture that fits a local, privacy-first LFM2.5 assistant; the rest is explicitly rejected (see the mapping table below). Everything stays stdlib-only and transport-agnostic with JS/Android parity, and the hardcoded approval gate (`core/tool_registry.py:18`, `:102`) is never weakened.

## Phase 8 — Command & control surface (slash-command registry + permission modes)

**Status: built (Phase 8 complete).** The CLI previously hardcoded its own `if/elif` chain and the web sidebar had no command surface at all. Claude Code's UX is driven by ~85 slash commands plus explicit permission modes; Agent Core now ports the *shape*, not the coding-specific commands.

- [x] Command registry in `core/commands.py` (stdlib-only): one `COMMANDS` table of `{name, description, usage, handler}` plus `CommandContext` / `CommandResult` (`text, state, quit, clear, error, save`), consumed by **both** `cli.py` and `web/server.py` (`POST /api/sessions/<id>/command`, `GET /api/commands`). The CLI's old `if/elif` chain is gone.
- [x] Commands that map to real Agent Core state: `/help`, `/new`, `/resume`, `/turns`, `/mode`, `/tools`, `/status` (`inspect_self`), `/context` (`core/context.py` budget), `/plan` (render `AgentState.plan`), `/memory` (`core/memory.py` recall), `/approve`, `/reject`, `/quit`. `/compact` and `/doctor` + `/cost` remain for Phases 11 and 12. Coding-only commands (`/commit`, `/pr_comments`, `/vim`, `/desktop`, `/mobile`) are rejected.
- [x] **Permission modes** — `default` | `plan` | `auto` | `strict` — on `AgentState.permission_mode` (`core/state.py` + `schemas/agent_state.schema.json` + `docs/data-contracts.md`), plus `ToolDefinition.read_only`. Enforced **inside `core/tool_registry.dispatch()`** (`_requires_approval`), never model-decided: `default` = ask for `requires_approval` tools (pre-Phase-8 behavior); `plan` = run only `read_only` tools, park everything else; `auto` = auto-approve gated tools except `HARDCODED_APPROVAL_TOOLS`; `strict` = park every tool call. `run_code` is never bypassable in any mode — only `resolve_approval(approved=True)` executes it.
- [x] JS parity: `web/commands.js` (same table shape, 9 browser commands), `requiresApproval()` in `web/engine.js`, `permission_mode` in `newState`/`createEngine`/`setPermissionMode`, `read_only` flags in `web/worker.js` `BROWSER_TOOLS`; parity cases in `test_webgpu_engine.mjs`.
- [x] UI: web sidebar permission-mode selector (`#permission-mode`), slash commands routed from the composer (`POST /api/sessions/<id>/command` or the worker's `command` message), mode carried through session create/clear and restored when selecting a session.
- [x] Tests: `test_commands.py` (registry shape, every command, CLI/web parity of the table, approve/reject through the gate), `test_permission_modes.py` (each mode's dispatch outcome + `run_code` never bypassable), `test_web.py` (`/api/command`, `/api/commands`, per-session mode, clear preserves mode), `test_webgpu_engine.mjs` (mode gate + command parity).

**Gate:** the same command produces the same result in CLI and web; every permission mode still routes `run_code` through the approval gate. Both hold.

## Phase 9 — File & code-intelligence tools

**Status: built (Phase 9 complete).** Claude Code's largest tool category is file I/O + search (`FileReadTool`, `FileWriteTool`, `FileEditTool`, `GlobTool`, `GrepTool`). Agent Core had none — `run_code` was the only filesystem-capable path, forcing the approval gate for trivial reads.

- [x] `tools/files.py` — handlers `read_file(path, max_chars?)`, `write_file(path, content)`, `edit_file(path, old_string, new_string)` (string-replacement, Claude Code's `FileEditTool` semantics), `list_dir(path)`, `glob(pattern, root?)`, `grep(pattern, path?, max_results?)`; registered via `register_files_tools` in `tools/__init__.py` + `windows/orchestrator.py:default_registry()`.
- [x] **Approval policy:** reads/list/glob/grep are `requires_approval: false` **and** `read_only: true`; `write_file` / `edit_file` are `requires_approval: true` — enforced by definition *and* the same hardcoded gate as `run_code`.
- [x] **Security:** `AGENT_CORE_FILE_ROOT` (default repo root) is the allowed root; `resolve_path()` resolves then asserts the target stays under it (symlink escapes included), and traversal is rejected. Writes are atomic (temp file + `os.replace`); `edit_file` fails when `old_string` is absent or non-unique.
- [x] Web/Android parity: browser `BROWSER_TOOLS` proxies the six tools through `POST /api/file` (`web/server.py` `_file_dispatch`, loopback/CSRF-guarded); the endpoint refuses `write_file`/`edit_file` without `approved: true`. Documented ceiling: host files only, under the server's file root.
- [x] Tests: `test_files.py` (tempfile root) — read/edit round-trip, unique-match enforcement, traversal rejection, approval halts `write_file`/`edit_file`; `/api/file` in `test_web.py`; `test_phase9_file_tool_flags_parity` in `test_webgpu_engine.mjs`.

**Gate:** the agent can read, search, and edit files; every mutating call halts at the approval gate; path traversal is rejected.

## Phase 10 — Sub-agents & skills

Claude Code spawns bounded sub-agents (`AgentTool` + `coordinator/`) and runs reusable workflows (`skills/` + `SkillTool`). Both map onto Agent Core's existing loop.

- [ ] `core/subagent.py` — `spawn_subagent(state, goal, max_turns)` runs a **nested, bounded** `core.loop.run()` with an isolated message list and its own turn/context budget, returning one summary string. The approval gate and retry-once rules apply unchanged inside it; approvals bubble to the parent loop and are never auto-approved. Scope: one level deep, hard cap on concurrent sub-agents.
- [ ] `agent` tool (`requires_approval: false`, state-aware) registered in `default_registry`; `inspect_self` reports active sub-agents.
- [ ] `tools/skills.py` + `skills/` — a skill is a declarative `{name, description, prompt, tools?}` JSON; `run_skill(name, args)` injects the skill prompt and constrains the tool set (a **tool allowlist**, not a permission bypass — every gated call still hits `dispatch()`).
- [ ] JS parity: `web/subagent.js` + skill loader; `test_webgpu_engine.mjs` coverage.
- [ ] Tests: `test_subagent.py` (bounded turns, approval propagation, no infinite recursion), `test_skills.py` (allowlist enforced, unknown skill errors).

**Gate:** a sub-agent completes a bounded task and returns its summary to the parent; a skill runs with its allowlist; neither can bypass the approval gate.

## Phase 11 — Context & memory automation

Claude Code's `compact/` (summarization) and `extractMemories/` (automatic capture) complete Agent Core's Phase 5 (memory/reflection) and Phase 6 (budget).

- [ ] **Phase 6b compaction (now scheduled):** when `core/context.trim_to_budget` would drop turns, summarize the dropped turns into one replacement system message via **one extra generate turn** — only if measurement (`docs/benchmarks.md`) shows base trimming loses too much. Never compacts the live approval turn or splits a tool/assistant pair.
- [ ] **Automatic memory extraction:** an `extractMemories` analog — after a terminal turn, an extended `core/reflection.py` proposes durable facts/preferences into `core/memory.py` (`kind=fact|preference`), bounded and deduped by key, rather than only failure lessons.
- [ ] **Tasks vs. plan:** add a lightweight `tasks` collection distinct from `AgentState.plan` (Claude Code separates `TaskCreateTool`/`TaskUpdateTool` from plan mode), persisted per session and surfaced in CLI + web.
- [ ] Tests: `test_context.py` compaction cases (summary replaces dropped turns, tool pairs never split), memory-extraction dedupe, task persistence.

**Gate:** a long resumed session stays under budget via compaction (or is proven unnecessary by measurement); memory extraction writes deduped facts; tasks persist across resume.

## Phase 12 — Observability (cost, context, doctor)

Claude Code surfaces `/cost`, `/context`, and `/doctor`. Agent Core already captures `usage` in `windows/orchestrator.py` but never shows it.

- [ ] `core/telemetry.py` (stdlib-only, **local only — no network**): accumulate per-session prompt/completion tokens and turn timing from the provider usage already captured; expose `session_usage(state)`.
- [ ] `/cost` + `/context` commands and a web sidebar readout: tokens used vs `max_context_tokens`, per-turn tok/s, tokens-only cost (inference is local).
- [ ] `/doctor` — self-diagnostics extending `inspect_self`: llama-server reachability, Docker availability for `run_code`, model/ctx match (the `Core-agent.bat` `:server_model` probe already hits `/v1/models`), session/memory dir writability.
- [ ] Tests: usage accumulation and formatting; doctor output with fake/unavailable dependencies (no live server).

**Gate:** `/cost` and `/context` report accurate session usage; `/doctor` correctly reports each dependency's state without a live model.

## Claude Code mapping (borrowed vs. rejected)

| Claude Code concept | Decision | Where it lands |
|---|---|---|
| Slash-command registry | **Borrow** (shape) | Phase 8 — `core/` command table |
| Permission modes | **Borrow** (hardcoded) | Phase 8 — `dispatch()` |
| File / glob / grep tool category | **Borrow** | Phase 9 — `tools/files.py` |
| Sub-agents (`AgentTool`, `coordinator/`) | **Borrow** (bounded) | Phase 10 — `core/subagent.py` |
| Skills / plugins | **Borrow** (skills only) | Phase 10 — `tools/skills.py` |
| `compact/`, `extractMemories/` | **Borrow** | Phase 11 |
| `tasks/` | **Borrow** | Phase 11 |
| `/cost`, `/context`, `/doctor` | **Borrow** | Phase 12 — `core/telemetry.py` |
| Bun / React / Ink / TypeScript stack | **Reject** | stdlib-only core + vanilla JS is the contract |
| OAuth, cloud, remote sessions | **Reject** | privacy-first / local-first |
| Telemetry, GrowthBook feature flags | **Reject** | nothing is collected; no phone-home |
| IDE bridge, vim, voice, keybindings | **Reject** | out of scope for the local assistant |

## Phase 8–12 ordering

Forced by dependency: Phase 8's permission modes gate Phase 9's mutating tools; Phases 10–11 build on Phase 8's command surface; Phase 12 only needs the usage already captured in Phase 4. Recommended order: **8 → 9 → 12 → 11 → 10** (observability before compaction; sub-agents last as highest-complexity).

## Traps

1. Ollama `output_norm` GGUF trap → direct llama.cpp binary + direct HF GGUF (never Ollama).
2. `--jinja` is mandatory for OpenAI-shaped `tool_calls`.
3. Python/pip version mismatch → everything inside `.venv`.
4. Pin exact binary + model versions for reproducible ports.
5. Keep `core/parser.py` dependency-free — it is the cross-language contract.
6. Android `run_code` has no sandbox story — a platform ceiling, not a forgotten task.
