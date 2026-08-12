@echo off
REM Registers the Claude Quota collector to start automatically at logon
REM using the Windows Task Scheduler (no admin rights required: /RL LIMITED).
REM The collector auto-refreshes its OAuth token (see CLAUDE.md) and logs to
REM collector.log in this folder; use restart_collector.bat to bounce it
REM manually (e.g. after an update or if the port is already in use).
setlocal
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_PATH=%SCRIPT_DIR%claude_quota.py"

where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PYTHON_EXE=pythonw"
) else (
    set "PYTHON_EXE=python"
)

for /f "delims=" %%P in ('where %PYTHON_EXE% 2^>nul') do (
    if not defined PYTHON_FULL_PATH set "PYTHON_FULL_PATH=%%P"
)

if not defined PYTHON_FULL_PATH (
    echo Could not find %PYTHON_EXE% on PATH. Install Python 3.9+ and try again.
    exit /b 1
)

schtasks /Create /TN "ClaudeQuotaCollector" /TR "\"%PYTHON_FULL_PATH%\" \"%SCRIPT_PATH%\"" /SC ONLOGON /RL LIMITED /F
if %ERRORLEVEL% NEQ 0 (
    echo Failed to create scheduled task.
    exit /b 1
)

schtasks /Run /TN "ClaudeQuotaCollector"

echo Claude Quota collector installed and started (runs at logon).
endlocal
