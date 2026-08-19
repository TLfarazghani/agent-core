# Benchmarks & Measurements

Output of the shipped gate (Phase 4). The Windows path is **not** considered shipped until this file has real measured numbers. Per the research doc audit hooks, no fourth target and no second model before these gates pass.

## Pinned versions

Record exact versions for reproducibility — ports must reproduce the same model behavior.

| Component | Version / ID | Downloaded (date) |
|---|---|---|
| llama.cpp binary | `llama-b10456-bin-win-cuda-12.4-x64.zip` | 2026-08-17 |
| GGUF | `LiquidAI/LFM2.5-1.2B-Instruct-GGUF` / `LFM2.5-1.2B-Instruct-Q4_K_M.gguf` (697MB) | 2026-08-17 |
| GGUF (2.6B, opt-in) | `LiquidAI/LFM2.5-2.6B-GGUF` / `LFM2.5-2.6B-Q4_K_M.gguf` (1.67GB) | 2026-08-19 |
| Python | 3.13.5 | |
| pydantic | 2.13.4 | |
| openai | 3.1.0 (v3 API — orchestrator must target v3, not the research doc's v1 sample) | |
| huggingface-hub | 1.27.0 (use `hf.exe` CLI, not `python -m huggingface_hub`) | |
| pytest | 9.1.1 | |
| jsonschema | 4.26.0 | 2026-08-17 |
| python-docx | 1.2.0 | 2026-08-17 |
| python-pptx | 1.0.2 | 2026-08-17 |
| docker-py | 7.2.0 | 2026-08-17 |
| Docker engine | 28.5.1 (desktop-linux) | verified 2026-08-17 |
| Server smoke test | `LFM2.5-1.2B-Instruct` Q4_K_M @ `127.0.0.1:8001`, 32768 ctx — replied "hello world" | 2026-08-17 |

## Environment

- Host: Windows, NVIDIA RTX 4060 Ti 8GB, CUDA driver (record driver version)
- Server: `llama-server` flags per `windows/server_config.ps1`
- Quantization: `Q4_K_M`

## Metrics

All agent-turn measurements below ran through the shipped Windows path: `cli.py`/E2E driver → `core.loop` → `windows/orchestrator.py` → llama-server (`--jinja`, tools=) → real tool dispatch. Measured 2026-08-17.

### 1. Inference speed

| Test | Prefill tok/s | Decode tok/s | Notes |
|---|---|---|---|
| Plain chat, short prompt | — | ~215 | measured as agent-turn decode (streaming); server `/health` timings not recorded separately |
| Plain chat, long prompt (~4k) | — | — | not measured |
| Agent turn with tool call | — | **207–220** | mean 215.7 across benchmark turns (884 completion tokens / 4.1s) |

### 2. Tool-call accuracy

Tool set: `create_docx`, `create_pptx`, `run_code`, plus a no-tool control. `web_search`/`send_email`/`send_message` not measured — no MCP remote was running. N=3 trials per prompt, prompts scripted so the only correct action is the target tool (or plain answer). With a system prompt instructing tool selection + "fill missing args with defaults".

| Tool | Trials | Correct calls | Correct args | Accuracy | Notes |
|---|---|---|---|---|---|
| create_docx | 6 | 3 | 3 | 50% | "make a docx titled Quarterly Report" 3/3; ambiguous "meeting notes" (no title/content) 0/3 — model asks for details instead of defaulting |
| create_pptx | 3 | 3 | 3 | 100% | all dispatched args passed schema validation and produced a real .pptx |
| run_code | 6 | 6 | 6 | 100% | python + bash, both languages, sandbox executed (approved) |
| none (answer directly) | 3 | 3 | 3 | 100% | "what is the capital of France" → plain answer, no tool call |
| **All dispatched** | 15/18 | 15 | 15 | **83%** | every call that was made was correct AND schema-valid |

Method: scripted prompts where the only correct action is the target tool with fixed arguments. Count: call made (`correct calls`), arguments exactly right (`correct args`). Note: 1.2B model — fails deterministically on under-specified prompts it interprets as needing clarification.

### 3. Loop + approval-gate behavior

| Check | Result | Notes |
|---|---|---|
| Multi-turn tool chaining works | PASS | E2E: `run_code` → approval → result → final answer (2-step, real server) |
| Turn cap at `max_turns=8` fires | PASS | `MaxTurnsError` path covered in unit tests; CLI prints "Turn budget reached" |
| No tool call → clean terminal answer | PASS | benchmark "None" control + unit tests |
| Malformed tool call → graceful handling | PASS | `_parse_arguments` degrades to `{"raw": ...}` (unit-tested) |
| `run_code` sets `pending_approval`, loop halts | PASS | real E2E + CLI piped run |
| `resolve_approval(approved=False)` clears without executing | PASS | unit-tested (rejection never runs sandbox) |
| `resolve_approval(approved=True)` runs the sandbox | PASS | real Docker: `print(42)` → `42` |
| Sandbox limits (`--network none`, memory/cpu, timeout) enforced | PASS | real Docker: outbound socket blocked, `sleep 300` killed |

### 4. run_code sandbox (Windows, Docker)

| Check | Result | Notes |
|---|---|---|
| Python / JS / bash images run | **PASS** | python `print(2+2)` → `4`; bash `echo` + `uname` OK (2026-08-17) |
| No network by default | **PASS** | outbound socket to 1.1.1.1:80 → `NETWORK: OFF` |
| Timeout kills runaway code | **PASS** | `sleep 300` killed by watchdog at timeout, container removed |
| Memory/cpu limits hold | PASS (flags asserted in unit tests) | `mem_limit=256m`, `nano_cpus=0.5` verified against fake client; real load not measured |

## Gate decision

Recorded 2026-08-17 after real-server measurement (see above):

- [x] tok/s acceptable for target use — bar: ≥ 50 tok/s interactive; measured **~215 tok/s** (RTX 4060 Ti, Q4_K_M)
- [x] Tool-call accuracy — bar: ≥ 80% on explicit task prompts; measured **83%** (100% on well-specified prompts; under-specified prompts fail deterministically on 1.2B)
- [x] All contracts schema-enforced (verified in `test_smoke.py` + orchestrator unit tests)
- [x] Approval gate verified end-to-end (real llama-server + real Docker)
- [x] **Windows path SHIPPED** — Android/Web ports may begin

Follow-up (non-blocking): re-measure with the 2.6B model once the second-model gate opens; add prefill timings via `/health`; measure networked tools when an MCP remote is running.

## Phase 7 — 2.6B second model (measured 2026-08-19)

Same harness as §1-2 above (`benchmark_tool_accuracy.py`), 2.6B Q4_K_M @ 128K ctx (`-fa on`), served by the same b10456 build with `--jinja`. **Registry kept minimal** (create_docx/create_pptx/run_code only — identical to the 1.2B baseline) so accuracy is apples-to-apples; the full registry was also probed for real-world tool preference.

### Side-by-side tool-call accuracy (N=3/prompt)

| Prompt | 1.2B (2026-08-17) | 2.6B (2026-08-19) |
|---|---|---|
| create_docx "make a docx titled Quarterly Report" | 3/3 | **3/3** |
| create_docx "meeting notes" (under-specified) | 0/3 — asks for details, no call | **0/3 — hallucinated a broken recursive `run_code` (3/3 deterministic)** |
| create_pptx "…project status with 3 slides" | 3/3 | **3/3** |
| run_code "run python code that prints 42" | 6/6 | **3/3** |
| none "what is the capital of France" | 3/3 | **3/3** |
| **Total** | **15/18 = 83%** | **12/15 = 80%** |

- **Decode speed:** 2.6B ~**107–115 tok/s** (agent-turn decode) vs 1.2B ~**215 tok/s** — still 2× over the 50 tok/s interactive bar.
- **Full-registry probe (2.6B):** the agentic model over-triggers `web_search` — "meeting notes" → `web_search`+`recall`, "capital of France" → `web_search` (schema-valid, but the SYSTEM_PROMPT says to answer direct questions without tools). 60% on the same prompts with full tools; those are interpretation differences, not malformed calls.

### Gate A decision (2026-08-19)

- [x] 2.6B downloads, launches on the canonical flags (`--jinja`, `-c 128K`, `-fa on`), and emits a schema-valid `make_plan` tool-call turn through the real pipeline
- [x] Full benchmark re-run recorded above
- [x] **Default decision (per plan.md rule — switch default only if accuracy improves ≥ 83% AND tok/s ≥ 50): accuracy did NOT improve (80% < 83%), tok/s ~110 ≥ 50 → KEEP 1.2B as Windows default; 2.6B ships as OPT-IN** (`AGENT_CORE_MODEL=LFM2.5-2.6B` + `AGENT_CORE_MAX_CONTEXT_TOKENS=131072`)
- [ ] 8B-A1B (Gate B): verify b10456 loads `lfm2moe`, then download + measure — pending
