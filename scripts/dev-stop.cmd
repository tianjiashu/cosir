@echo off
REM ===========================================================================
REM Coding-Agent Dev Environment Stopper
REM Kills backend/desktop windows by title, then cleans up stray processes.
REM
REM Usage:
REM   scripts\dev-stop.cmd
REM ===========================================================================

echo [stop] Stopping Coding-Agent dev environment ...

taskkill /F /FI "WINDOWTITLE eq Coding-Agent Backend*" 2>nul >nul && (
    echo [stop]   Terminated "Coding-Agent Backend"
)
taskkill /F /FI "WINDOWTITLE eq Coding-Agent Desktop*" 2>nul >nul && (
    echo [stop]   Terminated "Coding-Agent Desktop"
)

tasklist /FI "IMAGENAME eq python.exe" 2>nul | findstr /I "python.exe" >nul
if %ERRORLEVEL% equ 0 (
    echo [stop]   Cleaning stray python.exe ...
    for /f "tokens=2" %%p in ('tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH 2^>nul ^| findstr /I "python"') do (
        taskkill /F /PID %%p >nul 2>&1
    )
)

tasklist /FI "IMAGENAME eq cargo.exe" 2>nul | findstr /I "cargo.exe" >nul
if %ERRORLEVEL% equ 0 (
    echo [stop]   Cleaning stray cargo.exe ...
    taskkill /F /IM cargo.exe 2>nul >nul
)

echo [stop] Done. Logs preserved in logs/.
