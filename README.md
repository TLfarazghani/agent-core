# agent-core

A general-purpose local "JARVIS" assistant on LFM2.5: research/search, email, messaging, DOCX/PPTX generation, and arbitrary code execution — built from one transport-agnostic agent core.

One model family, three runtimes, one shared JSON contract.

- **Windows (shipped):** llama.cpp `llama-server`, GGUF, OpenAI-compatible `/v1/chat/completions`, Docker `run_code` sandbox
- **Android (planned port):** LEAP SDK (Kotlin/JNI), same schemas via kotlinx/Gson, no on-device `run_code` (platform ceiling)
- **WebGPU (code written, unverified in a browser):** Transformers.js + ONNX Runtime Web, WASM fallback, Pyodide `run_code` sandbox

The agent loop, tool registry, state machine, and approval gate are transport-agnostic. Each platform implements only a thin adapter between `AgentState` and the provider, plus its own sandbox backend.

## Status

**Completed (gates passed):** phases 0, 1, 1b, 2, 3, 5, 6, 7, **8, 9, 10, 11, 12** — Windows path. Phase 4 shipped on Windows; its Android port and in-browser WebGPU model are still pending (Phase 6b compaction ships opt-in via `AGENT_CORE_COMPACT=1`, unverified against a live model).

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Core — AgentState + tool-call parser + registry + approval gate | **DONE** — gate passed |
| 1 | Networked tools — email, messaging (uniform MCP-remote, opt-in) | **DONE** — gate passed; email upgraded to native SMTP in the default registry (2026-10-20), `send_message` still opt-in |
| 1b | Local web search — `web_search` (DuckDuckGo / Google News / Wikipedia) + `fetch_url`, keyless | **DONE** — gate passed |
| 2 | Local doc-gen — docx/pptx, 3 platform-specific backends | **DONE** — Windows backend shipped |
| 3 | Code execution + sandbox — Docker / Pyodide, approval-gated | **DONE** — Windows Docker shipped; browser Pyodide written (needs live WebGPU verification) |
| 4 | Full cross-platform parity — Android + WebGPU ports | **Windows done**; WebGPU code written (unverified in a real browser); Android pending |
| 5 | **Agent Core brain/self** — identity, cross-session memory, planning, reflection | **DONE** — `core/meta.py`, `core/memory.py`, `core/planner.py`, `core/reflection.py` + `tools/cognitive.py`; retry-once in the loop; JS parity (`web/memory.js`, `web/planner.js`) |
| 6 | Context window — token budget so long/resumed sessions never exceed the model's window | **DONE** — `core/context.py` + `web/context.js`, wired into orchestrator + browser engine |
| 7 | Second model — selectable 2.6B dense + 8B-A1B MoE | **Gate B PASSED 2026-08-19** — model selectable via `AGENT_CORE_MODEL` (env / `server_config.ps1 -Model` / `Core-agent.bat`); client sampling auto-mirrors each model's table; 2.6B benchmarked (80%, ~110 tok/s) and 8B-A1B benchmarked (80%, ~150 tok/s, full 128K ctx in 8GB VRAM) → both **opt-in**, 1.2B stays default (83%, ~215 tok/s); `benchmark_tool_accuracy.py` |
| 8 | Command & control surface — shared slash-command registry + permission modes | **DONE** — `core/commands.py` shared by CLI + web; `AgentState.permission_mode` (`default/plan/auto/strict`) enforced in `dispatch()`; JS parity `web/commands.js` + `web/engine.js`; mode selector + `/api/command` in the web UI |
| 9 | File & code-intelligence tools — read/write/edit/glob/grep, write/edit approval-gated | **DONE** — `tools/files.py` rooted at `AGENT_CORE_FILE_ROOT`; browser mirrors via `POST /api/file` |
| 10 | Sub-agents (bounded) & skills (tool-allowlisted) | **DONE** — `core/subagent.py` + `tools/subagent.py` (`agent`, one level deep) and `tools/skills.py` + `skills/` (`run_skill`/`list_skills`); JS parity `web/subagent.js`/`web/skills.js` |
| 11 | Context compaction + automatic memory extraction + tasks | **DONE** — `core/context.py` compaction (opt-in `AGENT_CORE_COMPACT=1`), `core/reflection.extract_memories`, `core/tasks.py` + `task_*`/`/tasks`; JS parity `web/tasks.js` |
| 12 | Observability — per-session cost/context + `doctor` self-diagnostics | **DONE** — `core/telemetry.py` (`record_usage` → `AgentState.usage` serializes with the session; `session_usage`/`format_cost`) + `/cost` + enhanced `/context` + `/doctor` (probes llama-server `/health`, `/v1/models` model/ctx match, Docker, dir writability — all injectable); JS parity `web/telemetry.js`, sidebar usage readout |

Phases 8–12 are an architectural review of the leaked Claude Code source (`codeaashu/claude-code`) — **patterns only, no source copied**; see `docs/plan.md`.

Test suites (verified 2026-10-20 after Phase 12): **297 pass** — **248** via `.venv\Scripts\python -m pytest`; **45** engine+context+Phase-8/9/10/11/12 parity via `node test_webgpu_engine.mjs`; **4** parser parity via `.venv\Scripts\python test_parser_js.py`. Previously verified live on llama-server (Phase 4 gate, ~215 tok/s 1.2B; 2.6B ~110 tok/s; 8B-A1B ~150 tok/s) and against live DuckDuckGo / Google News / Wikipedia.

Build order is forced by dependency: Phase 3 (code execution) dispatches through the same loop Phase 0 builds.

## Requirements

- **Windows 10/11** with PowerShell 5.1+ (PowerShell scripts)
- **Python 3.13** (3.13.5 tested) — the machine's system `pip` may point at an older Python (3.10 here); always use `py -3.13` or the venv interpreter
- **NVIDIA GPU** for the pinned CUDA 12.4 llama.cpp build (works on the CUDA 12.9 driver)
- **Docker Desktop** — optional, only for the `run_code` sandbox
- ~800 MB disk for the GGUF + ~240 MB for the llama.cpp prebuilt

## Quickstart

**One-shot (recommended):**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

Creates `.venv` with Python 3.13, installs `requirements.txt` (pydantic, jsonschema, python-docx, python-pptx, docker, **openai v3**, huggingface-hub), downloads the selected model's GGUF into `models\` (default `LFM2.5-1.2B-Instruct`; `-Model LFM2.5-2.6B` / `LFM2.5-8B-A1B`), fetches the pinned llama.cpp binary into `vendor\llama\` if missing, and prints a health-check summary. Safe to re-run — every step no-ops when its artifact exists. Useful switches: `-SkipModelDownload`, `-SkipLlamaDownload`, `-RunTests`. Downloaded artifacts are verified against published sha256 hashes pinned in the script; the exit code is nonzero only for real failures — optional bits (Docker absent, services not yet started) are warnings/notes.

**Manual:**

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python test_smoke.py          # Phase 0 core checks
.\windows\server_config.ps1                 # start llama-server
```

## Email (`send_email`)

Native SMTP in the default registry — every CLI/web session can send mail once configured (approval-gated like `run_code`/`write_file`):

```powershell
$env:AGENT_CORE_SMTP_HOST     = "smtp.gmail.com"
$env:AGENT_CORE_SMTP_PORT     = "587"            # 465 -> implicit TLS
$env:AGENT_CORE_SMTP_USER     = "you@gmail.com"
$env:AGENT_CORE_SMTP_PASSWORD = "<app password>"  # Gmail: generate an App Password, never your login
$env:AGENT_CORE_SMTP_FROM     = "you@gmail.com"   # optional, defaults to AGENT_CORE_SMTP_USER
```

Attachments resolve under `AGENT_CORE_FILE_ROOT`. Without `AGENT_CORE_SMTP_HOST` the tool reports it is unconfigured instead of failing silently. (Messaging — `send_message` — stays opt-in behind `MCP_BASE_URL`.)

## Security model / trust boundary

The web UI + API are **local-only and not authenticated**. Concrete protections and their exact scope:

- `web/server.py` binds `127.0.0.1` only, and requires the request Host header (and Origin, when present) to be loopback — that is a **CSRF / DNS-rebinding defense against your browser**, not authentication.
- **Any other local process running as your user can talk to the API directly** (list/read/delete sessions, call the file tools, or complete its own write confirmations). The real security boundary is your user account, not this server. Do not expose the port beyond loopback.
- Mutating file tools (`write_file`/`edit_file` via `POST /api/file`) are **not** authorized by a caller-supplied flag: the first call returns `202` with a one-time `confirm_token` bound to the exact arguments; the write happens only when the token is resubmitted (within 60 s) with identical arguments.
- POST request bodies are capped at 10 MB (`AGENT_CORE_WEB_MAX_BODY` env override); oversize requests answer `413`.
- `fetch_url`/`web_search` block private/loopback/link-local/metadata destinations after DNS resolution and re-check every redirect (SSRF guard; 1 MB per-hop download cap).
- CLI and web approvals (`run_code`, writes, external sends) are always human-gated via `resolve_approval`; `auto` mode never auto-approves code execution or external sends.
- **`run_code` sandbox hardening (2026-10-20):** the Docker container runs with no network, memory (256m), CPU (0.5), **PID limit (64 — fork bombs die fast)**, a **read-only root filesystem with a `/tmp` tmpfs** (scratch counts against the memory cgroup), **all capabilities dropped** + `no-new-privileges`, a watchdog timeout, a **100k-char output cap** (print bombs get truncated, not streamed), and images **pinned to manifest-list digests** (`tools/run_code.py _PINNED_DIGESTS`) instead of mutable tags.
- Each sandbox knob is injectable (`register_runcode_tool(mem_limit=..., pids_limit=..., max_output_chars=..., read_only=..., cap_drop=...)`); the fake-client test suite asserts every flag so none silently disappears, and the approval gate (`resolve_approval(approved=True)`) remains the only executor.
- Sessions and memory live at `%USERPROFILE%\.agent-core\` — same user scope as the rest of the app.

### File tools and secrets

The agent's file tools (`read_file`, `grep`, `write_file`, `edit_file`, `list_dir`, `glob`) operate on everything under **`AGENT_CORE_FILE_ROOT`** (default: this repository). That root is not an index boundary — **explicit path reads are not limited to what `glob`/`list_dir` return**, so anything placed under the root can reach model context:

- Known secret files (`.env`, `*.env`, `*.pem`, `*.key`, `*.pfx`, `*.p12`, `*.kdbx`, `id_rsa*`/`id_ed25519*`/`id_*` keys, `.ssh/*`, `.aws/*`, `.kube/config`, `.gnupg/*`, `.netrc`, `.npmrc`, `.git-credentials`, `credentials*.json`, `service-account*.json`, `secrets.y*ml`, `.htpasswd`) are **refused as content reads**: `read_file`/`edit_file` fail with the matched pattern named, `grep` skips them while scanning (and reports the skip count) and refuses an explicit grep of one. `list_dir`/`glob` expose names only, never content.
- The escape hatch is `AGENT_CORE_FILE_ALLOW_SENSITIVE=1` — set it per-invocation when you genuinely need to read such a file, never globally.
- **Do not keep real secrets under the file root.** The reliable fix is a narrower root: point `AGENT_CORE_FILE_ROOT` at a dedicated working directory that holds no secrets (recommended on shared or semi-trusted machines; keep the repo-root default only where the whole repository is already as trusted as this agent).
- **Approved writes are modifications**: any `write_file`/`edit_file` you approve can create or replace — or corrupt — files under the root, including secret-named files. Check the path in the approval prompt before approving; the web UI additionally requires a one-time confirm token per write.
- If a secret was read into a session, treat it as exposed to that session's log (`~/.agent-core/sessions/`) and rotate it.

## Key documents

- [docs/plan.md](docs/plan.md) — phased build plan with verification gates
- [docs/architecture.md](docs/architecture.md) — system diagram, module layout, sandbox decisions
- [docs/data-contracts.md](docs/data-contracts.md) — ToolDefinition / ChatMessage / AgentState / tool contracts
- [docs/benchmarks.md](docs/benchmarks.md) — measurement template for the shipped gate
- [docs/ui-ux-design.md](docs/ui-ux-design.md) — CLI / Web / Android UX spec, approval flow, design tokens
- [AGENTS.md](AGENTS.md) — rules and commands for AI agents working here
