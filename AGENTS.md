---
name: project-guidelines
description: Core rules, architecture, and commands for AI agents working in this repository.
---

# Project Overview
- **agent-core** — a transport-agnostic agent core for local LFM2.5 inference (Windows first, Android/WebGPU planned). General-purpose "JARVIS" assistant: search, email, messaging, docx/pptx, code execution.
- **Built:** Phase 0 core (`core/`), JSON Schemas (`schemas/`), `test_smoke.py`, `windows/server_config.ps1`; Phase 1 networked tools (`tools/remote.py` MCP-client + `tools/__init__.py` handlers + `test_networked_tools.py`); Phase 2 doc-gen (`tools/create_docx.py`, `tools/create_pptx.py`, `tools/_paths.py`, `test_docgen_tools.py`); Phase 3 run_code Docker sandbox (`tools/run_code.py`, `test_runcode.py`); **Phase 4 (gate PASSED 2026-08-17)** `windows/orchestrator.py` (llama.cpp OpenAI v3 adapter, streaming), `cli.py` REPL, `test_orchestrator.py`; **Web UI** `web/server.py` (stdlib SSE agent API), `web/index.html` + `style.css` + `app.js` (vanilla SPA), `test_web.py`, `core/sessions.py` (shared session lifecycle), `Core-agent.bat` launcher.
- **NOT built yet:** in-browser model (WebGPU / Transformers.js + Pyodide run_code) — the web UI currently talks to local llama-server via `web/server.py`; Android port (`android/AgentCore.kt`). Networked MCP tools are opt-in via `MCP_BASE_URL` env.
- Design decisions and phased plan: `docs/plan.md`, `docs/architecture.md`, `docs/data-contracts.md`, `docs/ui-ux-design.md`.

# Essential Commands (run from repo root; always use the venv interpreter)
- **Create venv:** `py -3.13 -m venv .venv` — REQUIRED. `python` is 3.13.5 but system `pip` resolves to a 3.10 site-packages; never call bare `pip`.
- **Install:** `.venv\Scripts\python -m pip install -r requirements.txt` (pydantic, jsonschema)
- **Run all tests:** `.venv\Scripts\python test_smoke.py` (single file at repo root; pytest also picks it up via `.venv\Scripts\python -m pytest`)
- **Model download:** `.venv\Scripts\hf.exe download LiquidAI/LFM2.5-1.2B-Instruct-GGUF LFM2.5-1.2B-Instruct-Q4_K_M.gguf --local-dir models`
  - huggingface-hub 1.x has **no `python -m huggingface_hub` module**; use `hf.exe`/`huggingface-cli.exe` in `.venv\Scripts`.
  - GGUF filename is capitalized `LFM2.5-1.2B-Instruct-Q4_K_M.gguf` — the lowercase name in Liquid docs is stale.
- **Start server:** `.\windows\server_config.ps1` (serves `127.0.0.1:8001`, model `LFM2.5-1.2B-Instruct`)
- **One-click launch:** `Core-agent.bat` — starts llama-server if down, checks Docker, starts the web UI, opens `http://127.0.0.1:8002`
- **Run the agent (CLI):** `.venv\Scripts\python cli.py` (needs llama-server up + Docker Desktop for `run_code`). `MCP_BASE_URL` env opts in the networked tools.
- **Run the agent (Web UI):** `.venv\Scripts\python web\server.py [port]` (default 8002), then open `http://127.0.0.1:8002`. Talk to the browser app with `http://127.0.0.1:8002/api/health`.

# Architecture (non-obvious, do not violate)
- The loop, tool registry, state machine, and **approval gate** live in `core/` and are transport-agnostic. Platform code only adapts `AgentState → provider request` / `response → ToolCall[]`; it never re-implements the loop. UIs render `AgentState` and resolve approvals — see `docs/ui-ux-design.md`.
- `core/parser.py` must stay **stdlib-only** (`ast`-based Pythonic extraction, no `eval`). It is the cross-language contract ported line-for-line to `parser.js` / `AgentCore.kt`. Never add third-party imports.
- Approval gate: `run_code` (and any `requires_approval` tool) halts via `state.pending_approval`, enforced **hardcoded in `core/tool_registry.dispatch()`** — never model-decided, never per-platform. `core/loop.resolve_approval(approved=bool)` is the only way through; only `approved=True` executes.
- `AgentState`/`ChatMessage`/`ToolDefinition` are validated with pydantic against `schemas/*.json` before any tool runs. Tool args are also validated against each tool's `parameters` schema in `ToolRegistry.execute`.
- **Contract is snake_case, not OpenAI camelCase:** `ChatMessage` uses `content` (string), `function_calls[]`, `tool_call_id`. The future `windows/orchestrator.py` must map llama.cpp's camelCase `tool_calls` to this shape.
- Tool handlers are registered by name; `tools/registry.json` is loaded via `ToolRegistry.load_json(path, handlers)` where `handlers` maps tool name → `callable(arguments: dict) -> str`.

# Toolchain gotchas
- **llama.cpp:** prebuilt cu12.4 binary in `vendor\llama\` (works on the CUDA 12.9 driver). No cmake/MSVC on this machine — never build from source. The **`--jinja` flag is mandatory** for OpenAI-shaped `tool_calls`.
- **Never use Ollama** for this GGUF — older syncs throw `missing tensor 'output_norm'`. Use the direct llama.cpp binary + direct HF download.
- **openai is v3** (3.1.0): the v1 `client.chat.completions` API shape in the research docs is outdated; the orchestrator must target the v3 client API.
- `.venv/`, `models/`, `vendor/` are gitignored — binaries/weights are never committed. Repo is a git repo (commits exist); keep docs/code in sync when schemas change.

# Testing quirks
- `test_smoke.py` is deliberately one self-contained file with `assert`-based tests and a `main()` runner; it proves the approval gate (ordinary call executes, `run_code` halts, rejection clears without executing). It runs without pytest installed. Keep this property.
- `test_networked_tools.py` (Phase 1) runs the same way and spins up a stdlib `http.server` mock MCP-remote — no real network, no external services.
- `test_docgen_tools.py` (Phase 2) writes real .docx/.pptx to a `tempfile` dir and reopens them with python-docx/python-pptx to verify content. Output dir defaults to `output/` (override `AGENT_CORE_OUTPUT_DIR`); it is gitignored via `output/` — check `.gitignore` if you add generated artifacts.
- `test_runcode.py` (Phase 3) uses a **fake docker client** — no daemon needed. `tools/run_code.py` has an injectable `client` param; never change it to construct `docker.from_env()` at import time. Timeout uses a watchdog thread that kills the container.
- `test_orchestrator.py` (Phase 4) stubs the OpenAI client — no server needed. Do not import `windows.orchestrator` without `openai` installed (it's in requirements).
- `test_web.py` (web UI) starts the real `web/server.py` on an ephemeral port with a **fake provider + fake registry** — no llama-server, no Docker. It exercises the actual HTTP + SSE + loop + approval plumbing. Do not let web/server tests require a live model.
- `web/server.py` inserts the repo root into `sys.path` itself (it runs from the `web/` directory, so `core` is not otherwise importable). It binds `127.0.0.1` only.
- **run_code needs Docker Desktop running** for real use (`docker info` must succeed). The Python sandbox also needs the `python:3.12-slim` image pulled on first use (same for `node:20-slim`, `bash:5`).
- Sessions (CLI and web) serialize `AgentState` to `~/.agent-core/sessions/*.json` via `core/sessions.py` — a session started in the CLI can be resumed in the web UI and vice versa. `SYSTEM_PROMPT` lives there too; both UIs seed new sessions with it.
- Each test file filters `tools/registry.json` to its own tools via `load_json(..., names={...})`. Adding a tool to `registry.json` will break other suites unless they pass `names=`.
- No `tests/` package and no pytest config exist — don't invent one unless a phase needs it.

# Docs references
- Research source: `C:\hermes\local-agent-lfm25-research.md` and `C:\hermes\New_local-agent-lfm25-research.md` (the latter is authoritative — it defines the current scope, contracts, and Phase 0-4 plan).