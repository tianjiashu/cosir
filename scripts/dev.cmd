@echo off
REM ===========================================================================
REM Coding-Agent Windows Dev Environment Launcher
REM Mirrors scripts/dev.sh: start backend (uvicorn) + desktop (tauri dev).
REM Logs go to logs/backend.log and logs/frontend.log.
REM
REM Usage:  scripts\dev.cmd
REM Stop:   scripts\dev-stop.cmd   (or close the two windows)
REM ===========================================================================

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%A in ("%SCRIPT_DIR%\..") do set "REPO_ROOT=%%~fA"
set "BACKEND_DIR=%REPO_ROOT%\apps\backend"
set "DESKTOP_DIR=%REPO_ROOT%\apps\desktop"
set "LOGS_DIR=%REPO_ROOT%\logs"
set "BACKEND_LOG=%LOGS_DIR%\backend.log"
set "FRONTEND_LOG=%LOGS_DIR%\frontend.log"

echo [dev] Coding-Agent Windows Dev Launcher
echo [dev] Repo root: %REPO_ROOT%
echo [dev] Logs:      %LOGS_DIR%

if not exist "%LOGS_DIR%" mkdir "%LOGS_DIR%"

REM --- Prerequisites ---
where uv >nul 2>nul
if errorlevel 1 (
    echo [dev] ERROR: uv not found. Install from https://docs.astral.sh/uv/
    pause
    exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
    echo [dev] ERROR: node not found.
    pause
    exit /b 1
)
if not exist "%BACKEND_DIR%\.venv\Scripts\python.exe" (
    echo [dev] ERROR: venv missing at %BACKEND_DIR%\.venv\Scripts\python.exe
    echo [dev] Run: cd apps\backend and uv sync
    pause
    exit /b 1
)

echo [dev] Cleaning ports 8000 and 1420 ...
call :free_port 8000
call :free_port 1420

echo [dev] Starting backend (uvicorn) to %BACKEND_LOG%
start "Coding-Agent Backend" /D "%BACKEND_DIR%" cmd /c "set CODING_AGENT_LANGFUSE_ENABLED=false && uv run python -m app > %BACKEND_LOG% 2>&1"

echo [dev] Waiting for backend health (http://127.0.0.1:8000/health) ...
ping -n 6 127.0.0.1 >nul
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8000/health > "%TEMP%\dev_health.txt" 2>nul
set /p HEALTH=<"%TEMP%\dev_health.txt"
if "%HEALTH%"=="200" (
    echo [dev] Backend ready (HTTP 200)
) else if "%HEALTH%"=="" (
    echo [dev] Backend not responding yet. See %BACKEND_LOG%
) else (
    echo [dev] Backend returned HTTP %HEALTH%. See %BACKEND_LOG%
)

echo [dev] Starting desktop (tauri dev) to %FRONTEND_LOG%
start "Coding-Agent Desktop" /D "%DESKTOP_DIR%" cmd /c "npm run tauri dev > %FRONTEND_LOG% 2>&1"

echo [dev]
echo [dev] ==============================================================
echo [dev]  Coding-Agent dev environment is running
echo [dev]    Backend:  http://127.0.0.1:8000
echo [dev]    Desktop:  compiling Rust (first build takes 3-10 min)
echo [dev]    Logs:     %LOGS_DIR%
echo [dev]  Stop:  scripts\dev-stop.cmd  or close the two windows
echo [dev] ==============================================================
echo [dev]
goto :eof

REM ===== Free a TCP port by killing its listener =====
:free_port
set "PORT=%~1"
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" > "%TEMP%\dev_fp_%PORT%.txt" 2>nul
if errorlevel 1 (
    echo [dev]   Port %PORT% free
    goto :eof
)
echo [dev]   Port %PORT% in use, killing listeners:
type "%TEMP%\dev_fp_%PORT%.txt"
for /f "tokens=5" %%p in ("%TEMP%\dev_fp_%PORT%.txt") do (
    echo [dev]     kill PID %%p
    taskkill /F /PID %%p >nul 2>&1
)
ping -n 3 127.0.0.1 >nul
echo [dev]   Port %PORT% freed
goto :eof
