# llama-server launch config for agent-core (Windows)
# Model: LFM2.5-1.2B-Instruct-GGUF Q4_K_M via llama.cpp b10456 (cu12.4 build)
#
# Usage: .\windows\server_config.ps1

$ErrorActionPreference = "Stop"

$server = "vendor\llama\llama-server.exe"
$model  = "models\LFM2.5-1.2B-Instruct-Q4_K_M.gguf"

if (-not (Test-Path $server)) { throw "llama-server not found: $server (run Phase 0 download first)" }
if (-not (Test-Path $model))  { throw "model not found: $model (run Phase 0 download first)" }

Write-Host "Starting llama-server with $model ..." -ForegroundColor Cyan

& $server `
  -m $model `
  --alias "LFM2.5-1.2B-Instruct" `
  --threads -1 `
  --n-gpu-layers 99 `
  --ctx-size 32768 `
  --port 8001 `
  --temp 0.1 `
  --top-k 50 `
  --top-p 0.1 `
  --repeat-penalty 1.05 `
  --jinja
