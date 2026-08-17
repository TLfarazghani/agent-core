---
name: project-guidelines
description: Core rules, architecture, and commands for AI agents working in this repository.
---

# Project Overview
- **Name:** agent-core
- **Purpose:** A general-purpose local "JARVIS" assistant on LFM2.5 — research/search, email, messaging, DOCX/PPTX generation, and arbitrary code execution — built from one transport-agnostic agent core.
- **Scope (locked 2026-08-17):** full feature set built at once rather than sequenced. Accepted trade-off: competes on distribution against established free projects (Open Interpreter, Jan, Goose, AGiXT, LibreChat); there is no domain moat at this scope.
- **Tech Stack:** Python 3.13 (core + Windows), pydantic + jsonschema, llama.cpp llama-server (GGUF, `--jinja`), docker-py (code sandbox). Later: Kotlin/JNI (Android, LEAP SDK), Transformers.js + ONNX Runtime Web (WebGPU, Pyodide sandbox).

# Essential Commands
- **Create venv:** `py -3.13 -m venv .venv`
- **Install deps:** `.venv\Scripts\python -m pip install -r requirements.txt`
- **Run tests:** `.venv\Scripts\python test_smoke.py`
- **Run CLI agent:** `.venv\Scripts\python -m cli` (requires llama-server running)
- **Start server:** `.\windows\server_config.ps1` (requires `vendor\llama\llama-server.exe` + `models\`)

# Architecture Rules (non-negotiable)
- The agent loop, tool registry, state machine, and **approval gate** live in `core/` and are **transport-agnostic**. Platform code only adapts `AgentState → provider request` and `provider response → parsed ToolCall[]`, plus its own `run_code` sandbox backend.
- Never write agent logic inside platform code. It must be duplicated, and it will drift.
- `AgentState`, `ChatMessage`, and `ToolDefinition` are validated by pydantic against the JSON schemas in `schemas/` before any code runs.
- The tool-call parser is **ONE implementation** (`core/parser.py`), stdlib-only. Ports (parser.js, AgentCore.kt) must be line-for-line ports, never rewritten regexes.
- The `run_code` approval gate is **hardcoded in `tool_registry.dispatch()`**, never model-decided or per-platform. It is verified by `test_smoke.py`.
- No fourth target and no second model until the Windows path has shipped and been measured (see `docs/benchmarks.md`).
- Android has **no run_code sandbox** (platform ceiling) — it either delegates to a reachable Windows instance or ships without it. This is tracked as an open item, not forgotten.

# Protocol Decision (documented, do not drift)
- LFM2.5 emits native **Pythonic** tool calls (`func(arg="value")`) between `<|tool_call_start|>` / `<|tool_call_end|>`. The parser uses Python's `ast` module to extract them into `arguments` dicts — **no eval, no regex**.
- On Windows, `llama.cpp --jinja` emits structured `tool_calls` natively; the parser covers raw-completion paths (WebGPU and any non-jinja fallback).

# Data Contracts
- **ToolDefinition:** `schemas/tool_definition.schema.json` — name, description, `requires_approval` (bool), JSON-Schema `parameters`.
- **ChatMessage:** `schemas/chat_message.schema.json` — `role`, string `content`, `tool_call_id`, snake_case `function_calls`. Shared verbatim across targets.
- **AgentState:** `schemas/agent_state.schema.json` — session_id, target, model, messages, max_turns (8), turn_count, `pending_approval`.
- **Tool contracts by category:**
  - Networked (uniform MCP-remote): `web_search(query)`, `send_email(to, subject, body, attachments?)`, `send_message(channel, to, text)`
  - Local compute (per-platform backend): `create_docx(title, sections)`, `create_pptx(title, slides)`
  - Code execution (`requires_approval` always true): `run_code(language, code, timeout_seconds)`
- See `docs/data-contracts.md` for full definitions.

# Code Style & Rules
- Python 3.13, type hints everywhere, pydantic models for all data entering the core.
- `core/parser.py` must stay **stdlib-only** (it is the cross-language contract).
- Keep modules small and single-purpose. Tests live in `test_smoke.py` (Phase 0) and grow per phase.
- Never modify `schemas/*.json` without updating `core/state.py` and both contract docs.
- No code comments unless they explain a non-obvious decision; prefer self-documenting names.

# Workflow & Error Handling
- If a build or test fails, read the error output completely before attempting a fix.
- Do not guess library methods; verify parameters against the docs (Liquid Docs MCP, llama.cpp README, pydantic docs).
- Known trap: GGUF pulled through Ollama may throw `missing tensor 'output_norm'` for LFM2.5. Use the direct llama.cpp binary + direct GGUF from HF, never Ollama, unless the sync status is confirmed.
- Pin exact versions (llama.cpp binary, GGUF, Python packages) and record them; reproducibility is a requirement for porting.

# Verification Gates
- Phase complete only when the phase's tests pass AND any real-server steps pass manually.
- Phase 3 (code execution) gate is the approval flow: `step()` halts on `pending_approval`; `resolve_approval()` only executes on `approved=True`.
- Phase 4 (parity) requires recorded tok/s + tool-call accuracy in `docs/benchmarks.md` before the Windows path is considered shipped.
