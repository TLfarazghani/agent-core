# bootstrap.ps1 -- one-shot setup for agent-core (Windows).
#
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads BOM-less .ps1
# files as ANSI, so any UTF-8 em-dash/arrow breaks tokenization.
#
# Creates .venv with Python 3.13, pins pip to a known version, installs the
# fully pinned requirements.lock (ranged requirements.txt only as a warned
# fallback), downloads the selected model's GGUF into models/, fetches the
# pinned llama.cpp binary if vendor\llama is empty, then prints a
# health-check summary and next steps. Safe to re-run: every step is a no-op
# when its artifact already exists.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Model LFM2.5-2.6B
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 -SkipModelDownload -RunTests
#
# Options:
#   -Model <alias>       GGUF to download: LFM2.5-1.2B-Instruct (default, or
#                        $env:AGENT_CORE_MODEL) | LFM2.5-2.6B | LFM2.5-8B-A1B
#   -VenvDir <path>      virtualenv location (default .venv)
#   -SkipModelDownload   assume models\<gguf> is already on disk
#   -SkipLlamaDownload   assume vendor\llama\llama-server.exe is already installed
#   -RunTests            run the pytest suite at the end (~30s)
#
# Model + llama.cpp versions are PINNED (repo trap #4): the alias -> repo/file
# table below must stay in sync with windows/server_config.ps1 and the
# llama.cpp zip must stay the pinned cu12.4 build on this driver.
#
# Exit semantics: ONLY Fail-Step conditions (no Python 3.13, venv/pip/
# download/integrity failures, unknown model, bad imports, pytest failures)
# are fatal and produce exit 1. Warn-Step conditions (optional Docker
# absent, ctx-budget mismatch, Skip* with a missing artifact, a RUNNING
# service answering non-200) are summarized but never affect the exit code;
# services expectedly down before first startup print as plain notes.
#
# Integrity: artifacts fetched by this script are verified against published
# hashes PINNED below -- the GitHub release-asset digest for the llama.cpp
# zip and the Hugging Face LFS sha256 for each GGUF. A mismatch deletes the
# bad file and is fatal. When bumping a pinned version, re-query the
# endpoints named next to each hash to re-pin. Verification runs only on a
# fresh download, so re-runs stay instant no-ops.
#
# Dependency reproducibility: step 3 pins pip itself ($pipPinned) instead of
# running an unpinned `pip install --upgrade pip`, and installs
# requirements.lock (fully pinned, transitive-inclusive) so a clean setup
# resolves the same versions indefinitely. Ranged requirements.txt is only a
# WARNED fallback when the lock is absent.

#Requires -Version 5.1
param(
    [string]$Model = $env:AGENT_CORE_MODEL,
    [string]$VenvDir = ".venv",
    [switch]$SkipModelDownload,
    [switch]$SkipLlamaDownload,
    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Two separate outcome lists: $failures is fatal (exit 1), $warnings is
# informational only and never affects the exit code. Warn-Step MUST NOT add
# to $failures -- optional/expected conditions (Docker absent, services not
# yet started) used to make a clean install exit nonzero.
$failures = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]
function Warn-Step($message) {
    $warnings.Add($message) | Out-Null
    Write-Host "   [WARN] $message" -ForegroundColor Yellow
}
function Fail-Step($message) {
    $failures.Add($message) | Out-Null
    Write-Host "   [FAIL] $message" -ForegroundColor Red
}
function Ok-Step($message) { Write-Host "   [ ok ] $message" -ForegroundColor Green }
function Note-Step($message) { Write-Host "   [note] $message" -ForegroundColor DarkGray }

# Keep in sync with windows/server_config.ps1's $models table and the ctx hint
# (matching AGENT_CORE_MAX_CONTEXT_TOKENS is what /doctor's context_budget
# check compares against). The Sha256 fields are the PUBLISHED Hugging Face
# LFS object ids (GET https://huggingface.co/api/models/<repo>/tree/main ->
# entry.lfs.oid, uploaded by Liquid AI); re-query them when bumping a model.
$modelTable = @{
    "LFM2.5-1.2B-Instruct" = @{ Repo = "LiquidAI/LFM2.5-1.2B-Instruct-GGUF"; File = "LFM2.5-1.2B-Instruct-Q4_K_M.gguf"; Sha256 = "b1b3de114215d9507409a662a501a631095a479a419584e8a2ded6304b19b4f5" }
    "LFM2.5-2.6B"          = @{ Repo = "LiquidAI/LFM2.5-2.6B-GGUF";          File = "LFM2.5-2.6B-Q4_K_M.gguf";          Sha256 = "02a8b7e17487d326e46d68ce0ba24211e1b80a14c4cd0597fa73c1cd697f52ed" }
    "LFM2.5-8B-A1B"        = @{ Repo = "LiquidAI/LFM2.5-8B-A1B-GGUF";        File = "LFM2.5-8B-A1B-Q4_K_M.gguf";        Sha256 = "4923ec14f06b968b74d663e5949867d2d9c3bf13a20b8be1a9f9af39989b2bb0" }
}
$ctxByModel = @{ "LFM2.5-1.2B-Instruct" = 32768; "LFM2.5-2.6B" = 131072; "LFM2.5-8B-A1B" = 128000 }

# Lowercase hex sha256 of a file (Get-FileHash emits uppercase; the pinned
# values above are lowercase, so normalize before comparing).
function Get-Sha256([string]$path) {
    return (Get-FileHash -Path $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# llama.cpp pinned prebuilt (never build from source; no cmake/MSVC here).
# Works on the CUDA 12.9 driver despite the cu12.4 toolchain.
# $llamaZipSha256 is the PUBLISHED GitHub release-asset digest for this exact
# zip (GET https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/
# b10456 -> assets[].digest, computed by GitHub from the served bytes);
# re-query it when bumping the pinned build number.
$llamaExtractDir = "vendor\llama"
$llamaServer = Join-Path $llamaExtractDir "llama-server.exe"
$llamaZipUrl = "https://github.com/ggml-org/llama.cpp/releases/download/b10456/llama-b10456-bin-win-cuda-12.4-x64.zip"
$llamaZipSha256 = "10da43a0ac6b0ca67ebfcc5afb9778861819e9c0cd9f1374183e1c1cf6271dfa"

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  agent-core bootstrap"
Write-Host "=============================================" -ForegroundColor Cyan

# ---------------------------------------------------------------- 1) Python 3.13
Write-Host ""
Write-Host "[1/6] Python 3.13"
$pyLauncher = $null   # command array to invoke Python 3.13+
$pyVersion = $null

function Get-PyVersion([string]$pyCmd, [string[]]$pyArgs = @()) {
    try {
        $out = & $pyCmd @pyArgs -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
        if ($LASTEXITCODE -eq 0 -and $out) { return ("$out").Trim() }
    } catch { }
    return $null
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    $v = Get-PyVersion "py" @("-3.13")
    if ($v -and ($v -like "3.13*")) {
        $pyLauncher = @("py", "-3.13"); $pyVersion = $v
    }
}
if (-not $pyLauncher -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $v = Get-PyVersion "python"
    # The 3.10-system-`pip` trap: only accept 3.13+ (lower = fatal), warn on >3.13 (untested).
    if ($v -and ($v -like "3.13*")) {
        $pyLauncher = @("python"); $pyVersion = $v
    } elseif ($v) {
        $pyVersion = $v
    }
}
if (-not $pyLauncher) {
    Fail-Step "no Python 3.13 found (got '$pyVersion'). Install Python 3.13.x, then re-run. The machine's plain 'python'/'pip' pointing at 3.10 is the known trap."
} else {
    Ok-Step "Python $pyVersion ($($pyLauncher -join ' '))"
}

# ---------------------------------------------------------------- 2) venv
Write-Host ""
Write-Host "[2/6] virtualenv ($VenvDir)"
$venvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not $pyLauncher) {
    Fail-Step "skipped (no usable Python)"
} elseif (Test-Path $venvPython) {
    Ok-Step "already exists: $venvPython ($(Get-PyVersion $venvPython))"
} else {
    $createArgs = $pyLauncher + @("-m", "venv", $VenvDir)
    & $createArgs[0] $createArgs[1..($createArgs.Length - 1)]
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Fail-Step "could not create $VenvDir with $($pyLauncher -join ' ')"
    } else {
        Ok-Step "created $venvPython"
    }
}

# ---------------------------------------------------------------- 3) dependencies
Write-Host ""
Write-Host "[3/6] dependencies (pip, inside the venv)"
# Reproducibility: install requirements.lock (fully pinned, transitive-
# inclusive) rather than ranged requirements.txt, and pin pip itself -- the
# previous unconditional `pip install --upgrade pip` made the installer a
# moving target, so two clean setups months apart could resolve differently.
$pipPinned = "26.2.1"
if (Test-Path $venvPython) {
    & $venvPython -m pip install --disable-pip-version-check --quiet "pip==$pipPinned"
    if ($LASTEXITCODE -ne 0) {
        Fail-Step "could not install pinned pip $pipPinned (exit $LASTEXITCODE)"
    }
    $reqSource = "requirements.txt"
    if (Test-Path "requirements.lock") {
        $reqSource = "requirements.lock"
    } else {
        Warn-Step "requirements.lock missing -- falling back to ranged requirements.txt (install is NOT fully pinned)"
    }
    & $venvPython -m pip install --disable-pip-version-check --quiet -r $reqSource
    if ($LASTEXITCODE -ne 0) {
        Fail-Step "pip install -r $reqSource failed (exit $LASTEXITCODE)"
    } else {
        Ok-Step "installed from $reqSource (pinned pip $pipPinned): pydantic, jsonschema, python-docx, python-pptx, docker, openai (v3 client), huggingface-hub (hf CLI), pytest"
    }
} else {
    Fail-Step "skipped (no venv)"
}

# ---------------------------------------------------------------- 4) model
Write-Host ""
Write-Host "[4/6] model GGUF"
if (-not $Model) { $Model = "LFM2.5-1.2B-Instruct" }
if (-not $modelTable.ContainsKey($Model)) {
    Fail-Step "unknown model '$Model'. Known: $($modelTable.Keys -join ', ')"
} else {
    $modelCfg = $modelTable[$Model]
    $ggufPath = "models\$($modelCfg.File)"
    $hfExe = Join-Path $VenvDir "Scripts\hf.exe"
    $hfCmd = Join-Path $VenvDir "Scripts\hf"
    if (Test-Path $ggufPath) {
        $sizeMb = [math]::Round((Get-Item $ggufPath).Length / 1MB)
        Ok-Step "$ggufPath already present ($sizeMb MB) -- launch with server_config.ps1 -Model $Model"
    } elseif (-not $SkipModelDownload) {
        if (-not (Test-Path $hfExe) -and -not (Test-Path $hfCmd)) {
            Fail-Step "hf CLI not found at $hfExe -- is huggingface-hub installed in $VenvDir?"
        } else {
            $hf = $hfExe
            if (-not (Test-Path $hf)) { $hf = $hfCmd }
            Write-Host "   downloading $($modelCfg.Repo)/$($modelCfg.File) ..." -ForegroundColor Cyan
            & $hf download $modelCfg.Repo $modelCfg.File --local-dir models --quiet
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path $ggufPath)) {
                Fail-Step "model download failed (exit $LASTEXITCODE) -- re-run, or fetch manually: hf download $($modelCfg.Repo) $($modelCfg.File) --local-dir models"
            } else {
                # Integrity: verify against the pinned PUBLISHED sha256. The
                # hf client already checks the hash the server reports at
                # transfer time; this pin is independent of that self-report.
                $actualSha = Get-Sha256 $ggufPath
                if ($actualSha -ne $modelCfg.Sha256) {
                    Remove-Item $ggufPath -ErrorAction SilentlyContinue
                    Fail-Step "sha256 mismatch for $ggufPath (expected $($modelCfg.Sha256), got $actualSha) -- deleted; re-run, or fetch manually: hf download $($modelCfg.Repo) $($modelCfg.File) --local-dir models"
                } else {
                    Ok-Step "downloaded $ggufPath (sha256 verified)"
                }
            }
        }
    } else {
        Warn-Step "SkipModelDownload set but $ggufPath is missing -- llama-server will fail to start without it"
    }
}

# ---------------------------------------------------------------- 5) llama.cpp binary
Write-Host ""
Write-Host "[5/6] llama.cpp (pinned b10456 cu12.4 prebuilt)"
if (Test-Path $llamaServer) {
    Ok-Step "llama-server.exe present in $llamaExtractDir"
} elseif (-not $SkipLlamaDownload) {
    Write-Host "   downloading pinned prebuilt (~240 MB): $llamaZipUrl" -ForegroundColor Cyan
    try {
        $zipPath = Join-Path $env:TEMP "llama-b10456-cu12.4.zip"
        Invoke-WebRequest -UseBasicParsing -Uri $llamaZipUrl -OutFile $zipPath -TimeoutSec 600
        # Integrity: verify the downloaded zip against the pinned PUBLISHED
        # GitHub asset digest BEFORE extracting anything from it. A mismatch
        # deletes the bad file and aborts (fatal), never extracts.
        $actualZipSha = Get-Sha256 $zipPath
        if ($actualZipSha -ne $llamaZipSha256) {
            Remove-Item $zipPath -ErrorAction SilentlyContinue
            throw "zip sha256 mismatch (expected $llamaZipSha256, got $actualZipSha) -- deleted; fetch manually from $llamaZipUrl"
        }
        New-Item -ItemType Directory -Force -Path $llamaExtractDir | Out-Null
        Expand-Archive -Path $zipPath -DestinationPath $llamaExtractDir -Force
        if (-not (Test-Path $llamaServer)) {
            # Some builds extract into a single subfolder -- locate and flatten.
            $inner = Get-ChildItem -Path $llamaExtractDir -Recurse -Filter "llama-server.exe" | Select-Object -First 1
            if ($inner -and ((Split-Path $inner.FullName) -ne $llamaExtractDir)) {
                Move-Item -Path $inner.FullName -Destination $llamaExtractDir -Force
            }
        }
        Remove-Item $zipPath -ErrorAction SilentlyContinue
        if (Test-Path $llamaServer) {
            Ok-Step "extracted llama-server.exe into $llamaExtractDir (zip sha256 verified)"
        } else {
            Fail-Step "zip extracted but llama-server.exe not found under $llamaExtractDir"
        }
    } catch {
        Fail-Step "llama.cpp download failed ($($_.Exception.Message)). Fetch manually: $llamaZipUrl"
    }
} else {
    Warn-Step "SkipLlamaDownload set but $llamaServer is missing -- install it, then start with .\windows\server_config.ps1"
}

# ---------------------------------------------------------------- 6) health checks
Write-Host ""
Write-Host "[6/6] health checks"

# Context-budget pairing hint (client budget must match the server ctx table).
$ctx = $env:AGENT_CORE_MAX_CONTEXT_TOKENS
if (-not $ctx) { $ctx = $ctxByModel[$Model] }
if ($ctx -and ([int]$ctx -ne $ctxByModel[$Model])) {
    Warn-Step "AGENT_CORE_MAX_CONTEXT_TOKENS=$ctx does not match the ctx table for $Model (expected $($ctxByModel[$Model])) -- /doctor's context_budget check will flag this"
} else {
    Ok-Step "context budget $ctx matches the $Model server ctx"
}

# Python-side import sanity (proves pydantic/JSON-schema env loads the core).
if (Test-Path $venvPython) {
    & $venvPython -c "import core; from core import run_command, run_doctor, AgentState; import windows.orchestrator"
    if ($LASTEXITCODE -eq 0) {
        Ok-Step "core + windows.orchestrator import cleanly in the venv"
    } else {
        Fail-Step "venv import check failed (exit $LASTEXITCODE)"
    }
}

# Service endpoints (informational; Core-agent.bat starts them). Both being
# down is the NORMAL pre-startup state, so "not running" is a plain note --
# only a RUNNING service answering non-200 is a warning.
$probes = @(
    @{ Name = "llama-server @ http://127.0.0.1:8001/health"; Url = "http://127.0.0.1:8001/health" },
    @{ Name = "web UI @ http://127.0.0.1:8002/api/health";   Url = "http://127.0.0.1:8002/api/health" }
)
foreach ($probe in $probes) {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri $probe.Url -TimeoutSec 3
        if ($resp.StatusCode -eq 200) {
            Ok-Step "$($probe.Name) is up"
        } else {
            Warn-Step "$($probe.Name) returned $($resp.StatusCode)"
        }
    } catch {
        Note-Step "$($probe.Name) not running (start with .\windows\server_config.ps1 / Core-agent.bat)"
    }
}

# Docker (optional -- only run_code needs it, so absence is a WARNING that
# never affects the exit code). cmd /c swallows stderr so the PS 5.1
# "native stderr + 2>&1 + ErrorActionPreference Stop throws" trap cannot
# misreport a down daemon as a missing CLI.
cmd /c "docker info >nul 2>&1"
if ($LASTEXITCODE -eq 0) {
    Ok-Step "Docker daemon reachable (run_code sandbox available)"
} elseif ($LASTEXITCODE -eq 9009) {
    Warn-Step "docker CLI not found -- run_code disabled unless Docker Desktop is installed"
} else {
    Warn-Step "Docker installed but the daemon is down -- run_code needs Docker Desktop (other tools still work)"
}

# Optional: full test suite.
if ($RunTests) {
    Write-Host ""
    Write-Host "[extra] test suite (~30s)"
    if (Test-Path $venvPython) {
        & $venvPython -m pytest -q
        if ($LASTEXITCODE -eq 0) {
            Ok-Step "pytest suite green"
        } else {
            Fail-Step "pytest reported failures (exit $LASTEXITCODE)"
        }
    }
}

# ---------------------------------------------------------------- summary
Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
if ($failures.Count -gt 0) {
    Write-Host "  bootstrap FAILED: $($failures.Count) fatal error(s), $($warnings.Count) warning(s):" -ForegroundColor Red
    foreach ($f in $failures) { Write-Host "    [FAIL] $f" -ForegroundColor Red }
    foreach ($w in $warnings) { Write-Host "    [WARN] $w" -ForegroundColor Yellow }
} elseif ($warnings.Count -gt 0) {
    Write-Host "  bootstrap complete -- $($warnings.Count) warning(s), none fatal:" -ForegroundColor Yellow
    foreach ($w in $warnings) { Write-Host "    [WARN] $w" -ForegroundColor Yellow }
    Write-Host "  (warnings are informational; setup succeeded)" -ForegroundColor Yellow
} else {
    Write-Host "  bootstrap complete -- no problems found." -ForegroundColor Green
}
Write-Host ""
Write-Host "Next steps:"
Write-Host "  .\Core-agent.bat              # one-click: llama-server + web UI"
Write-Host "  .\windows\server_config.ps1   # llama-server only (-Model LFM2.5-2.6B | LFM2.5-8B-A1B)"
Write-Host "  .\.venv\Scripts\python cli.py # CLI REPL"
Write-Host "  /doctor, /cost, /context      # inside the agent: diagnostics + usage"
# Only FATAL failures produce exit 1; warnings never do.
exit $(if ($failures.Count -eq 0) { 0 } else { 1 })
