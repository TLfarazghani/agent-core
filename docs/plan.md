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

## Out of scope until shipped gate

Second model (e.g. 2.6B / 8B-A1B) and any fourth target.

## Traps

1. Ollama `output_norm` GGUF trap → direct llama.cpp binary + direct HF GGUF (never Ollama).
2. `--jinja` is mandatory for OpenAI-shaped `tool_calls`.
3. Python/pip version mismatch → everything inside `.venv`.
4. Pin exact binary + model versions for reproducible ports.
5. Keep `core/parser.py` dependency-free — it is the cross-language contract.
6. Android `run_code` has no sandbox story — a platform ceiling, not a forgotten task.
