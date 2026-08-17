# Benchmarks & Measurements

Output of the shipped gate (Phase 4). The Windows path is **not** considered shipped until this file has real measured numbers. Per the research doc audit hooks, no fourth target and no second model before these gates pass.

## Pinned versions

Record exact versions for reproducibility — ports must reproduce the same model behavior.

| Component | Version / ID | Downloaded (date) |
|---|---|---|
| llama.cpp binary | `llama-b10456-bin-win-cuda-12.4-x64.zip` | 2026-08-17 |
| GGUF | `LiquidAI/LFM2.5-1.2B-Instruct-GGUF` / `LFM2.5-1.2B-Instruct-Q4_K_M.gguf` (697MB) | 2026-08-17 |
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

### 1. Inference speed

| Test | Prefill tok/s | Decode tok/s | Notes |
|---|---|---|---|
| Plain chat, short prompt | | | |
| Plain chat, long prompt (~4k) | | | |
| Agent turn with tool call | | | |

Measurement method: llama-server exposes timings in its logs and via `/health`. Record both if available.

### 2. Tool-call accuracy

Tool set: `web_search`, `send_email`, `send_message`, `create_docx`, `create_pptx`, `run_code`. N trials each.

| Tool | Trials | Correct calls | Correct args | Accuracy | Notes |
|---|---|---|---|---|---|
| web_search | | | | | |
| send_email | | | | | |
| send_message | | | | | |
| create_docx | | | | | |
| create_pptx | | | | | |
| run_code | | | | | |

Method: scripted prompts where the only correct action is the target tool with fixed arguments. Count: call made (`correct calls`), arguments exactly right (`correct args`).

### 3. Loop + approval-gate behavior

| Check | Result | Notes |
|---|---|---|
| Multi-turn tool chaining works | | e.g. search → compose → send |
| Turn cap at `max_turns=8` fires | | |
| No tool call → clean terminal answer | | |
| Malformed tool call → graceful handling | | |
| `run_code` sets `pending_approval`, loop halts | | |
| `resolve_approval(approved=False)` clears without executing | | |
| `resolve_approval(approved=True)` runs the sandbox | | |
| Sandbox limits (`--network none`, memory/cpu, timeout) enforced | | |

### 4. run_code sandbox (Windows, Docker)

| Check | Result | Notes |
|---|---|---|
| Python / JS / bash images run | **PASS** | python `print(2+2)` → `4`; bash `echo` + `uname` OK (2026-08-17) |
| No network by default | **PASS** | outbound socket to 1.1.1.1:80 → `NETWORK: OFF` |
| Timeout kills runaway code | **PASS** | `sleep 300` killed by watchdog at timeout, container removed |
| Memory/cpu limits hold | PASS (flags asserted in unit tests) | `mem_limit=256m`, `nano_cpus=0.5` verified against fake client; real load not measured |

## Gate decision

Record the verdict here once numbers are in:

- [ ] tok/s acceptable for target use (define the bar before measuring)
- [ ] Tool-call accuracy ≥ (define the bar before measuring)
- [ ] All contracts schema-enforced (verified in `test_smoke.py`)
- [ ] Approval gate verified end-to-end
- [ ] **Windows path SHIPPED** — Android/Web ports may begin
