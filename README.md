# agent-core

A general-purpose local "JARVIS" assistant on LFM2.5: research/search, email, messaging, DOCX/PPTX generation, and arbitrary code execution — built from one transport-agnostic agent core.

One model family, three runtimes, one shared JSON contract.

- **Windows (shipped):** llama.cpp `llama-server`, GGUF, OpenAI-compatible `/v1/chat/completions`, Docker `run_code` sandbox
- **Android (planned port):** LEAP SDK (Kotlin/JNI), same schemas via kotlinx/Gson, no on-device `run_code` (platform ceiling)
- **WebGPU (code written, unverified in a browser):** Transformers.js + ONNX Runtime Web, WASM fallback, Pyodide `run_code` sandbox

The agent loop, tool registry, state machine, and approval gate are transport-agnostic. Each platform implements only a thin adapter between `AgentState` and the provider, plus its own sandbox backend.

## Status

**Completed (gates passed):** phases 0, 1, 1b, 2, 3, 5, 6, 7, **8** — Windows path. Phase 4 shipped on Windows; its Android port and in-browser WebGPU model are still pending, and Phase 6b compaction is not built. **Remaining:** phases 9–12 — planned, no code yet.

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
| 7 | Second model — selectable 2.6B dense + 8B-A1B MoE | **Gate B PASSED 2026-08-19** — model selectable via `AGENT_CORE_MODEL` (env / `server_config.ps1 -Model` / `Core-agent.bat`); client sampling auto-mirrors each model's table; 2.6B benchmarked (80%, ~110 tok/s) and 8B-A1B benchmarked (80%, ~150 tok/s, full 128K ctx in 8GB VRAM) → both **opt-in**, 1.2B stays default (83%, ~215 tok/s); `benchmark_tool_accuracy.py` |
| 8 | Command & control surface — shared slash-command registry + permission modes | **DONE** — `core/commands.py` shared by CLI + web; `AgentState.permission_mode` (`default/plan/auto/strict`) enforced in `dispatch()`; JS parity `web/commands.js` + `web/engine.js`; mode selector + `/api/command` in the web UI |
| 9 | File & code-intelligence tools — read/write/edit/glob/grep, write/edit approval-gated | **DONE** — `tools/files.py` rooted at `AGENT_CORE_FILE_ROOT`; browser mirrors via `POST /api/file` |
| 10 | Sub-agents (bounded) & skills (tool-allowlisted) | **PLANNED** |
| 11 | Context compaction + automatic memory extraction + tasks | **PLANNED** |
| 12 | Observability — per-session cost/context + `doctor` self-diagnostics | **PLANNED** |

Phases 8–12 are an architectural review of the leaked Claude Code source (`codeaashu/claude-code`) — **patterns only, no source copied**; see `docs/plan.md`.

Test suites (verified 2026-10-19): **214 pass** — **181** via `.venv\Scripts\python -m pytest`; **29** engine+context+Phase-8/9 parity via `node test_webgpu_engine.mjs`; **4** parser parity via `.venv\Scripts\python test_parser_js.py`. Previously verified live on llama-server (Phase 4 gate, ~215 tok/s 1.2B; 2.6B ~110 tok/s; 8B-A1B ~150 tok/s) and against live DuckDuckGo / Google News / Wikipedia.

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
