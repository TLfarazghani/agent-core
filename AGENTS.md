---
name: project-guidelines
description: Core rules, architecture, and commands for AI agents working in this repository.
---

# Project Overview
- **Name:** agent-core
- **Purpose:** A transport-agnostic agent core for local LFM2.5 model inference, first targeting Windows (llama.cpp), with Android (LEAP SDK) and WebGPU (Transformers.js) as planned ports. One model family, three runtimes, one shared JSON contract.
- **Tech Stack:** Python 3.13 (core + Windows orchestrator), pydantic, llama.cpp (llama-server, GGUF), OpenAI Python client. Later: Kotlin/JNI (Android), Transformers.js + ONNX Runtime Web (WebGPU).

# Essential Commands
- **Create venv:** `py -3.13 -m venv .venv`
- **Install deps:** `.venv\Scripts\python -m pip install openai pydantic huggingface-hub pytest`
- **Run tests:** `.venv\Scripts\python -m pytest`
- **Run CLI agent:** `.venv\Scripts\python -m cli` (requires llama-server running)
- **Download model:** `.venv\Scripts\python -m huggingface_hub download LiquidAI/LFM2.5-1.2B-Instruct-GGUF lfm2.5-1.2b-instruct-q4_k_m.gguf --local-dir models`
- **Start server:** see `windows/server_config.ps1` (requires `vendor\llama\llama-server.exe`)

# Architecture Rules (non-negotiable)
- The agent loop, tool registry, and state machine live in `core/` and are **transport-agnostic**. Platform code (`windows/`, `android/`, `web/`) only adapts `AgentState → provider request` and `provider response → parsed ToolCall[]`.
- Never write agent logic inside platform code. It must be duplicated, and it will drift.
- `AgentState`, `ChatMessage`, and `ToolDefinition` are validated by pydantic against the JSON schemas in `schemas/` before any code runs.
- The tool-call parser is **ONE implementation** (`core/parser.py`), pure stdlib, portable. Ports (parser.js, AgentCore.kt) must be line-for-line ports, never rewritten regexes.
- No second target and no second model until the Windows path has shipped and been measured (see `docs/benchmarks.md`).

# Protocol Decision (documented, do not drift)
- LFM2.5 emits Pythonic calls (`func(arg="value")`) by default. This project standardizes on **JSON-shaped tool calls**, forced via the system-prompt instruction in the core prompt builder.
- On Windows, `llama.cpp --jinja` emits structured `tool_calls` natively; the parser is used for raw-completion paths (WebGPU and any non-jinja fallback).

# Data Contracts
- **ToolDefinition:** `schemas/tool_definition.schema.json` — name, description, JSON-Schema parameters.
- **ChatMessage:** `schemas/chat_message.schema.json` — OpenAI-compatible; `content` blocks, `functionCalls`, `reasoningContent`. Reused across all targets so payloads are interchangeable.
- **AgentState:** `schemas/agent_state.schema.json` — session_id, target, model, messages, tool_registry, max_turns (8), turn_count.
- See `docs/data-contracts.md` for full definitions.

# Code Style & Rules
- Python 3.13, type hints everywhere, pydantic models for all data entering the core.
- `core/parser.py` must stay **stdlib-only** (it is the cross-language contract).
- Keep modules small and single-purpose. Tests live in `tests/` mirroring module names.
- Never modify `schemas/*.json` without updating `core/schemas.py` and both contract docs.
- No code comments unless they explain a non-obvious decision; prefer self-documenting names.

# Workflow & Error Handling
- If a build or test fails, read the error output completely before attempting a fix.
- Do not guess library methods; verify parameters against the docs (Liquid Docs MCP, llama.cpp README, pydantic docs).
- Known trap: GGUF pulled through Ollama may throw `missing tensor 'output_norm'` for LFM2.5. Use the direct llama.cpp binary + direct GGUF from HF, never Ollama, unless the sync status is confirmed.
- Pin exact versions (llama.cpp binary, GGUF, Python packages) and record them; reproducibility is a requirement for porting.

# Verification Gates
- Phase complete only when the phase's pytest suite is green AND any real-server steps pass manually.
- Phase 7 requires recording tok/s + tool-call accuracy in `docs/benchmarks.md` before considering the Windows path shipped.
