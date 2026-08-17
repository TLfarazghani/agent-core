# Build Plan

Full phased build plan for the agent-core Windows-first build. Source: `local-agent-lfm25-research.md` (2026-08-13), verified against Liquid Docs (2026-08-17) and the host machine.

## Locked decisions

| Decision | Choice |
|---|---|
| Initial tool set | Stub/reference tools (`echo`, `get_time`, `fs_list_dir`, `fs_read_file`) to prove the loop, then swap in real pipeline tools |
| Interface | Interactive CLI REPL + `run_turn(state)` library API |
| Scope | Windows only; parser/schemas kept portable for Android/Web ports |
| Location | `C:\hermes\agent-core\` |
| Model | `LiquidAI/LFM2.5-1.2B-Instruct-GGUF`, file `lfm2.5-1.2b-instruct-q4_k_m.gguf` |
| Server | Prebuilt llama.cpp CUDA binary (`llama-b7075-bin-win-cuda-12.4-x64.zip`) — NOT built from source, NOT Ollama |

## Environment facts (measured 2026-08-17)

- Python 3.13.5 on PATH, but `pip` resolves to a Python 3.10 site-packages → **mandatory venv** (`py -3.13 -m venv .venv`)
- No `cmake`, no MSVC `cl` on PATH → use official prebuilt CUDA binaries
- NVIDIA RTX 4060 Ti 8GB, CUDA 12.9 toolkit (cu12.4 prebuilt binary runs on the 12.9 driver)
- Ollama 0.32.9 installed but rejected: research doc §4.1 flags the `output_norm` GGUF sync trap

## Server launch flags (canonical)

```powershell
vendor\llama\llama-server.exe `
  -m models\lfm2.5-1.2b-instruct-q4_k_m.gguf `
  --alias "LFM2.5-1.2B-Instruct" `
  --threads -1 --n-gpu-layers 99 --ctx-size 32768 `
  --port 8001 --temp 0.1 --top-k 50 --top-p 0.1 `
  --repeat-penalty 1.05 --jinja
```

`--jinja` applies LFM2.5's own chat template and emits structured `tool_calls` in the OpenAI response shape — this is what makes the Windows path trivial.

## Phase 0 — Scaffolding & prerequisites

- [ ] Create `agent-core/` layout (done — this file set)
- [ ] `git init` + `.gitignore` (`.venv/`, `models/`, `vendor/`, `__pycache__/`, `.pytest_cache/`)
- [ ] `py -3.13 -m venv .venv`; install `openai`, `pydantic`, `huggingface-hub`, `pytest`
- [ ] Download `llama-b7075-bin-win-cuda-12.4-x64.zip` → extract to `vendor\llama\` (record exact version in `docs/benchmarks.md`)
- [ ] `huggingface_hub` download GGUF → `models\`
- [ ] Smoke test: launch server, `curl http://127.0.0.1:8001/v1/chat/completions`, confirm reply + note tok/s

**Gate:** plain chat completion succeeds against the real model on the real server.

## Phase 1 — Data contracts (`core/schemas.py` + `schemas/*.json`)

- [ ] Write authoritative JSON Schemas: `tool_definition`, `chat_message`, `agent_state`
- [ ] pydantic models mirroring them: `ToolDefinition`, `ToolCall`, `ChatMessage` (content blocks + `functionCalls` + `reasoningContent`), `AgentState`
- [ ] `core/schemas.py` validates every message entering/leaving the core
- [ ] Tests: schema-validate the sample payloads from `docs/data-contracts.md`; round-trip serialization; invalid payload rejection

**Gate:** pytest suite green; no test imports platform code.

## Phase 2 — Tool registry (`core/tool_registry.py`)

- [ ] Load `tools/registry.json`, validate each entry against `tool_definition.schema.json` at load time
- [ ] Dispatch table `name → callable`; arguments validated via pydantic before dispatch
- [ ] Stub tools: `echo`, `get_time`, `fs_list_dir`, `fs_read_file` (deterministic, safe paths only)
- [ ] Tests: invalid definition rejected; valid dispatch; invalid args rejected with clean error

**Gate:** registry loads from JSON and dispatches all four stub tools correctly.

## Phase 3 — Tool-call parser (`core/parser.py`)

- [ ] Pure stdlib, zero external deps — the cross-language contract for later `parser.js` / `AgentCore.kt` ports
- [ ] Parse `<|tool_call_start|>...<|tool_call_end|>` → `ToolCall[]`; JSON-shaped arguments only (protocol decision in AGENTS.md)
- [ ] Handle: multiple calls in one message, partial/malformed input, plain text with no tool call
- [ ] Tests: multi-call extraction, malformed input, no-tool-call passthrough

**Gate:** pytest green; file imports nothing outside stdlib.

## Phase 4 — Agent loop (`core/loop.py`)

- [ ] `run_turn(state) -> state`: build prompt (system prompt forces JSON-shaped calls) → `generate(state)` → parse tool_calls → dispatch via registry → append tool result message → repeat until no tool call or `turn_count >= max_turns`
- [ ] Loop depends on a `generate()` provider interface; the Windows orchestrator implements it
- [ ] State machine owned entirely in `core/`; platforms never mutate state directly
- [ ] Tests: fake `generate()` provider (scripted tool calls) proves loop termination, history correctness, turn-cap enforcement — no server needed

**Gate:** loop converges with a fake provider; turn cap fires at `max_turns` (8).

## Phase 5 — Windows orchestrator (`windows/orchestrator.py`)

- [ ] Implement `generate()` via `openai.OpenAI(base_url="http://127.0.0.1:8001/v1", api_key="sk-no-key-required")`, passing `tools=` so llama.cpp `--jinja` parses calls
- [ ] Server lifecycle helper: start `llama-server`, wait on `/v1/models`, healthcheck, shutdown
- [ ] `windows/server_config.ps1` with the canonical flags above
- [ ] Manual verification: one full agent turn against the real model using the four stub tools

**Gate:** an end-to-end `run_turn` against the real server dispatches a real tool call and returns its result.

## Phase 6 — CLI REPL (`cli.py`)

- [ ] `python -m cli`: interactive loop, streams assistant text, prints tool calls + results, enforces turn cap
- [ ] Reuses the library API — REPL is a thin UI over `run_turn`
- [ ] Manual: chat with the agent, watch it call `fs_list_dir` / `fs_read_file` / `echo` / `get_time`

**Gate:** a recorded REPL session exercises at least one tool call end-to-end.

## Phase 7 — Verification & benchmarks (audit hooks from research doc §7)

- [ ] Full pytest suite green
- [ ] Measure tok/s + tool-call accuracy on the four stub tools against the real model; record in `docs/benchmarks.md`
- [ ] Confirm all three contracts are schema-enforced before any real tool is added
- [ ] Pin and record exact versions (llama.cpp binary, GGUF, Python packages)

**Gate:** Windows path is shipped and measured. Per research doc §7, **no** Android/Web port and **no** second model until this gate passes.

## Out of scope (structure only, for later)

- `android/AgentCore.kt`, `web/worker.js`, `web/parser.js` — the schemas and the stdlib parser in `core/parser.py` are the port contract.

## Traps

1. Ollama `output_norm` GGUF trap → direct llama.cpp binary + direct HF GGUF (never Ollama).
2. `--jinja` is mandatory for OpenAI-shaped `tool_calls`.
3. Python/pip version mismatch → everything inside `.venv`.
4. Pin exact binary + model versions for reproducible ports.
5. Keep `core/parser.py` dependency-free — it is the cross-language contract.
