@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "SERVER_PS=%~dp0windows\server_config.ps1"
set "LLAMA_HEALTH=http://127.0.0.1:8001/health"
set "WEB_HEALTH=http://127.0.0.1:8002/api/health"
set "WEB_URL=http://127.0.0.1:8002"

echo =============================================
echo   agent-core launcher  -  LFM2.5 1.2B (local)
echo =============================================
echo.

if not exist "%VENV_PY%" (
  echo [ERROR] venv not found at %VENV_PY%
  echo         Run: py -3.13 -m venv .venv
  echo         Run: .venv\Scripts\python -m pip install -r requirements.txt
  pause
  exit /b 1
)

REM ---------- 1) llama-server ----------
echo [1/3] model server ...
call :is_up "%LLAMA_HEALTH%"
if "%UP%"=="1" (
  echo        llama-server already running on :8001
) else (
  echo        starting llama-server ...
  start "llama-server" powershell -NoProfile -ExecutionPolicy Bypass -File "%SERVER_PS%"
  set "UP=0"
  for /l %%i in (1,1,30) do (
    timeout /t 2 /nobreak >nul
    call :is_up "%LLAMA_HEALTH%"
    if "!UP!"=="1" goto llama_ok
  )
  echo [ERROR] llama-server did not come up on :8001 in 60s.
  echo         Check vendor\llama\llama-server.exe and models\LFM2.5-1.2B-Instruct-Q4_K_M.gguf
  pause
  exit /b 1
)
:llama_ok
echo        model online.

REM ---------- 2) Docker Desktop (optional, for run_code) ----------
echo [2/3] docker sandbox ...
docker info >nul 2>&1
if "%errorlevel%"=="0" (
  echo        Docker running - run_code sandbox available.
) else (
  echo        Docker not running - run_code will fail until Docker Desktop starts.
  echo        (Chat and docx/pptx tools still work without it.)
)

REM ---------- 3) web UI ----------
echo [3/3] web UI ...
call :is_up "%WEB_HEALTH%"
if "%UP%"=="1" (
  echo        web UI already running at %WEB_URL%
) else (
  start "agent-core-web" "%VENV_PY%" web\server.py 8002
  set "UP=0"
  for /l %%i in (1,1,15) do (
    timeout /t 1 /nobreak >nul
    call :is_up "%WEB_HEALTH%"
    if "!UP!"=="1" goto web_ok
  )
  echo [ERROR] web UI did not come up on :8002.
  pause
  exit /b 1
)
:web_ok
echo        web UI online.

start "" "%WEB_URL%"
echo.
echo Launched. Open %WEB_URL% in your browser if it did not open.
echo (CLI alternative: %VENV_PY% cli.py)
echo.
pause
exit /b 0

REM ---------- helpers ----------
:is_up
set "UP=0"
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -Uri '%1' -TimeoutSec 2; exit 0 } catch { exit 1 }" >nul 2>&1
if "%errorlevel%"=="0" set "UP=1"
exit /b 0
