# agent-core

A general-purpose local "JARVIS" assistant on LFM2.5: research/search, email, messaging, DOCX/PPTX generation, and arbitrary code execution — built from one transport-agnostic agent core.

One model family, three runtimes, one shared JSON contract.

- **Windows (primary, in progress):** llama.cpp `llama-server`, GGUF, OpenAI-compatible `/v1/chat/completions`, Docker `run_code` sandbox
- **Android (planned port):** LEAP SDK (Kotlin/JNI), same schemas via kotlinx/Gson, no on-device `run_code` (platform ceiling)
- **WebGPU (planned port):** Transformers.js + ONNX Runtime Web, WASM fallback, Pyodide `run_code` sandbox

The agent loop, tool registry, state machine, and approval gate are transport-agnostic. Each platform implements only a thin adapter between `AgentState` and the provider, plus its own sandbox backend.

## Status

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Core — AgentState + tool-call parser + registry + approval gate | **in progress** |
| 1 | Networked tools — search, email, messaging (uniform MCP-remote) | pending |
| 2 | Local doc-gen — docx/pptx, 3 platform-specific backends | pending |
| 3 | Code execution + sandbox — Docker / Pyodide, approval-gated | pending |
| 4 | Full cross-platform parity — Android + WebGPU ports | pending |

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
