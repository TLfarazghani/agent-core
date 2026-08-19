# llama-server launch config for agent-core (Windows)
# Model is selectable (Phase 7): set AGENT_CORE_MODEL to one of the table keys
# below (default LFM2.5-1.2B-Instruct), or pass -Model explicitly.
#
# Usage: .\windows\server_config.ps1            # uses $env:AGENT_CORE_MODEL or 1.2B
#        $env:AGENT_CORE_MODEL="LFM2.5-2.6B"; .\windows\server_config.ps1
#        .\windows\server_config.ps1 -Model LFM2.5-2.6B

param(
    [string]$Model = $env:AGENT_CORE_MODEL
)

$ErrorActionPreference = "Stop"

$server = "vendor\llama\llama-server.exe"

# Known model configs. Key = alias (matches core/sessions.default_model()).
$models = @{
    "LFM2.5-1.2B-Instruct" = @{
        Gguf     = "models\LFM2.5-1.2B-Instruct-Q4_K_M.gguf"
        CtxSize  = 32768
        Fa       = $false
        Temp     = 0.1
        TopK     = 50
        TopP     = 0.1
        Repeat   = 1.05
    }
    "LFM2.5-2.6B" = @{
        Gguf     = "models\LFM2.5-2.6B-Q4_K_M.gguf"
        CtxSize  = 131072
        Fa       = $true
        Temp     = 0.1
        TopK     = 50
        TopP     = 0.1
        Repeat   = 1.1
    }
    "LFM2.5-8B-A1B" = @{
        Gguf     = "models\LFM2.5-8B-A1B-Q4_K_M.gguf"
        CtxSize  = 128000
        Fa       = $true
        Temp     = 0.2
        TopK     = 80
        TopP     = 0.2
        Repeat   = 1.05
    }
}

if (-not $Model) { $Model = "LFM2.5-1.2B-Instruct" }
if (-not $models.ContainsKey($Model)) {
    throw "Unknown model '$Model'. Known: $($models.Keys -join ', ')"
}

$cfg  = $models[$Model]
$alias = $Model
$gguf = $cfg.Gguf

if (-not (Test-Path $server)) { throw "llama-server not found: $server (run Phase 0 download first)" }
if (-not (Test-Path $gguf))   { throw "model not found: $gguf (run the Phase 7 download first)" }

Write-Host "Starting llama-server with $Model ($gguf) ..." -ForegroundColor Cyan

$llamaArgs = @(
    "-m", $gguf,
    "--alias", $alias,
    "--threads", "-1",
    "--n-gpu-layers", "99",
    "--ctx-size", [string]$cfg.CtxSize,
    "--port", "8001",
    "--temp", [string]$cfg.Temp,
    "--top-k", [string]$cfg.TopK,
    "--top-p", [string]$cfg.TopP,
    "--repeat-penalty", [string]$cfg.Repeat,
    "--jinja"
)
if ($cfg.Fa) { $llamaArgs += "-fa", "on" }

& $server @llamaArgs