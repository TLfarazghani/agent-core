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

- [ ] `tools/web_search.py` — `web_search(query)`
- [ ] `tools/send_email.py` — `send_email(to, subject, body, attachments?)`
- [ ] `tools/send_message.py` — `send_message(channel, to, text)` (whatsapp|telegram)
- [ ] Uniform MCP-remote transport so all three platforms share the identical HTTP/SSE implementation shape (no subprocess transport issue)
- [ ] Tool tests with mocked remotes

**Gate:** each networked tool dispatch returns schema-valid results with a mocked remote.

## Phase 2 — Local doc-gen (per-platform backends)

- [ ] `tools/create_docx.py` — `create_docx(title, sections)` via python-docx
- [ ] `tools/create_pptx.py` — `create_pptx(title, slides)` via python-pptx
- [ ] Backends: Windows python-docx/python-pptx; Web docx.js/pptxgenjs; Android Apache POI (heavy — reconsider for v1)
- [ ] Tool tests (file produced, opens, content matches)

**Gate:** generated .docx/.pptx validate; tools registered and dispatched.

## Phase 3 — Code execution + sandbox (highest risk)

| Mechanism | Isolation | Android | Browser | Install footprint | Licensing |
|---|---|---|---|---|---|
| **Docker (chosen, Windows)** | Strong (namespace/cgroup) | No | N/A | Docker Desktop required | Free under ~250 employees/$10M |
| Windows Sandbox API | Strong (VM-lite) | No | N/A | Windows Pro/Enterprise only | Free |
| AppContainer / job object | Weak-medium | No | N/A | None | Free |
| **Pyodide (chosen, browser)** | Strong (WASM) | N/A | Yes, no install | None | Free |

- [ ] `tools/run_code.py` — `run_code(language, code, timeout_seconds)`, `requires_approval: true` hardcoded
- [ ] Windows handler: docker-py, `--network none`, memory/cpu limits, timeout
- [ ] Web handler: Pyodide worker (already tab-sandboxed, zero install)
- [ ] Android: no sandbox — delegate to reachable Windows instance or omit; tracked as open platform-ceiling item
- [ ] Tests: approval halts loop, approval runs, rejection clears, timeout kills

**Gate:** `run_code` executes only after `resolve_approval(approved=True)`; sandbox limits enforced.

## Phase 4 — Full cross-platform parity

- [ ] `windows/orchestrator.py` — wraps core with OpenAI client (llama.cpp `--jinja`)
- [ ] `windows/run_code` docker handler wired
- [ ] `android/AgentCore.kt` — Kotlin port of core control flow (ported, not reinvented)
- [ ] `web/worker.js` + `web/parser.js` — line-for-line port of `core/parser.py`
- [ ] Measure tok/s + tool-call accuracy; record in `docs/benchmarks.md`

**Gate:** Windows path shipped and measured. Per research doc §7, no fourth target / second model before this gate.

## Out of scope until shipped gate

Second model (e.g. 2.6B / 8B-A1B) and any fourth target.

## Traps

1. Ollama `output_norm` GGUF trap → direct llama.cpp binary + direct HF GGUF (never Ollama).
2. `--jinja` is mandatory for OpenAI-shaped `tool_calls`.
3. Python/pip version mismatch → everything inside `.venv`.
4. Pin exact binary + model versions for reproducible ports.
5. Keep `core/parser.py` dependency-free — it is the cross-language contract.
6. Android `run_code` has no sandbox story — a platform ceiling, not a forgotten task.
