# agent-core

A general-purpose local "JARVIS" assistant on LFM2.5: research/search, email, messaging, DOCX/PPTX generation, and arbitrary code execution — built from one transport-agnostic agent core.

One model family, three runtimes, one shared JSON contract.

- **Windows (shipped):** llama.cpp `llama-server`, GGUF, OpenAI-compatible `/v1/chat/completions`, Docker `run_code` sandbox
- **Android (planned port):** LEAP SDK (Kotlin/JNI), same schemas via kotlinx/Gson, no on-device `run_code` (platform ceiling)
- **WebGPU (code written, unverified in a browser):** Transformers.js + ONNX Runtime Web, WASM fallback, Pyodide `run_code` sandbox

The agent loop, tool registry, state machine, and approval gate are transport-agnostic. Each platform implements only a thin adapter between `AgentState` and the provider, plus its own sandbox backend.

## Status

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Core — AgentState + tool-call parser + registry + approval gate | **DONE** — gate passed |
| 1 | Networked tools — email, messaging (uniform MCP-remote, opt-in) | **DONE** — gate passed |
| 1b | Local web search — `web_search` (DuckDuckGo / Google News / Wikipedia) + `fetch_url`, keyless | **DONE** — gate passed |
| 2 | Local doc-gen — docx/pptx, 3 platform-specific backends | **DONE** — Windows backend shipped |
| 3 | Code execution + sandbox — Docker / Pyodide, approval-gated | **DONE** — Windows Docker shipped; browser Pyodide written (needs live WebGPU verification) |
| 4 | Full cross-platform parity — Android + WebGPU ports | **Windows done**; WebGPU code written (unverified in a real browser); Android pending |
| 5 | **Agent Core brain/self** — identity, cross-session memory, planning, reflection | **DONE** — `core/meta.py`, `core/memory.py`, `core/planner.py`, `core/reflection.py` + `tools/cognitive.py`; retry-once in the loop; JS parity (`web/memory.js`, `web/planner.js`) |
| 6 | Context window — token budget so long/resumed sessions never exceed the model's window | **DONE** — `core/context.py` + `web/context.js`, wired into orchestrator + browser engine |

Test suites: core 21/21, networked 7/7, web search 8/8, doc-gen 9/9, run_code 8/8, orchestrator 11/11, web UI 18/18, JS engine 17/17, parser parity 4/4, context 12/12, memory 12/12, planner 9/9, meta 6/6. Verified live on llama-server (Phase 4 gate, ~215 tok/s) and against live DuckDuckGo / Google News / Wikipedia.

Build order is forced by dependency: Phase 3 (code execution) dispatches through the same loop Phase 0 builds.

## Quickstart

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python test_smoke.py          # Phase 0 core checks
.\windows\server_config.ps1                 # start llama-server
```

## Key documents

- [docs/plan.md](docs/plan.md) — phased build plan with verification gates
- [docs/architecture.md](docs/architecture.md) — system diagram, module layout, sandbox decisions
- [docs/data-contracts.md](docs/data-contracts.md) — ToolDefinition / ChatMessage / AgentState / tool contracts
- [docs/benchmarks.md](docs/benchmarks.md) — measurement template for the shipped gate
- [docs/ui-ux-design.md](docs/ui-ux-design.md) — CLI / Web / Android UX spec, approval flow, design tokens
- [AGENTS.md](AGENTS.md) — rules and commands for AI agents working here
