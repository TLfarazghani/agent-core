# agent-core

A transport-agnostic agent core for local LFM2.5 inference. One model family, three runtimes, one shared JSON contract.

- **Windows (primary, in progress):** llama.cpp `llama-server`, GGUF, OpenAI-compatible `/v1/chat/completions`
- **Android (planned port):** LEAP SDK (Kotlin/JNI), same schemas via kotlinx/Gson
- **WebGPU (planned port):** Transformers.js + ONNX Runtime Web, WASM fallback

The agent loop, tool registry, and state machine are transport-agnostic. Each platform implements only a thin adapter between `AgentState` and the provider.

## Status

Phase 0 scaffolding — in progress. See [docs/plan.md](docs/plan.md) for the phased build plan.

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Scaffolding, venv, model + llama.cpp download, server smoke test | **in progress** |
| 1 | Data contracts (`core/schemas.py` + `schemas/*.json`) | pending |
| 2 | Tool registry (`core/tool_registry.py`) | pending |
| 3 | Tool-call parser (`core/parser.py`) | pending |
| 4 | Agent loop (`core/loop.py`) | pending |
| 5 | Windows orchestrator (`windows/orchestrator.py`) | pending |
| 6 | CLI REPL (`cli.py`) | pending |
| 7 | Verification & benchmarks | pending |

## Quickstart

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install openai pydantic huggingface-hub pytest
.venv\Scripts\python -m huggingface_hub download LiquidAI/LFM2.5-1.2B-Instruct-GGUF lfm2.5-1.2b-instruct-q4_k_m.gguf --local-dir models
# start server, then:
.venv\Scripts\python -m cli
```

See `windows/server_config.ps1` for the exact llama-server launch flags.

## Key documents

- [docs/plan.md](docs/plan.md) — phased build plan with verification gates
- [docs/architecture.md](docs/architecture.md) — system diagram and module layout
- [docs/data-contracts.md](docs/data-contracts.md) — ToolDefinition / ChatMessage / AgentState definitions
- [docs/benchmarks.md](docs/benchmarks.md) — measurement template for Phase 7
- [AGENTS.md](AGENTS.md) — rules and commands for AI agents working here
