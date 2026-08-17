# Benchmarks & Measurements

Phase 7 output. The Windows path is **not** considered shipped until this file has real measured numbers. Per the research doc audit hooks, no Android/Web port and no second model before these gates pass.

## Pinned versions

Record exact versions for reproducibility — ports must reproduce the same model behavior.

| Component | Version / ID | Downloaded (date) |
|---|---|---|
| llama.cpp binary | (e.g. `llama-b7075-bin-win-cuda-12.4-x64.zip`) | |
| GGUF | `LiquidAI/LFM2.5-1.2B-Instruct-GGUF` / `lfm2.5-1.2b-instruct-q4_k_m.gguf` | |
| Python | 3.13.5 | |
| openai | | |
| pydantic | | |
| huggingface-hub | | |
| pytest | | |

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

Measurement method: llama-server exposes timings in its logs and via `/health` (or the token timings in server responses). Record both if available.

### 2. Tool-call accuracy

Stub tools: `echo`, `get_time`, `fs_list_dir`, `fs_read_file`. N trials each.

| Tool | Trials | Correct calls | Correct args | Accuracy | Notes |
|---|---|---|---|---|---|
| echo | | | | | |
| get_time | | | | | |
| fs_list_dir | | | | | |
| fs_read_file | | | | | |

Method: scripted prompts where the only correct action is the target tool with fixed arguments. Count: call made (`correct calls`), arguments exactly right (`correct args`).

### 3. Loop behavior

| Check | Result | Notes |
|---|---|---|
| Multi-turn tool chaining works | | e.g. list dir → read file |
| Turn cap at `max_turns=8` fires | | |
| No tool call → clean terminal answer | | |
| Malformed tool call → graceful handling | | |

## Gate decision

Record the verdict here once numbers are in:

- [ ] tok/s acceptable for target use (define the bar before measuring)
- [ ] Tool-call accuracy ≥ (define the bar before measuring)
- [ ] All contracts schema-enforced (verified in pytest)
- [ ] **Windows path SHIPPED** — Android/Web ports may begin
