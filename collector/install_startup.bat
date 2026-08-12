@echo off
REM Registers the Claude Quota collector to start automatically at logon.
REM Tries the Windows Task Scheduler first (/RL LIMITED asks for no-admin
REM rights, but some machines still refuse ONLOGON task creation with
REM "Access is denied"); if that fails, falls back to a per-user
REM HKCU...\Run registry value, which never needs admin rights. The
REM collector auto-refreshes its OAuth token (see CLAUDE.md) and logs to
REM collector.log in this folder; use restart_collector.bat to bounce it
REM manually (e.g. after an update or if the port is already in use).
REM
REM Resolves the FULL PATH of a real Python interpreter before registering
REM the task, because plain "python" is often the Microsoft Store's "App
REM execution alias" stub (prints "Python was not found", exit code 9009)
REM rather than a real interpreter, and schtasks needs a concrete path
REM anyway. Each candidate returned by "where" is validated by actually
REM running it with a harmless exit-code test; the Store stub fails that
REM test, a real interpreter passes it instantly. Checks pythonw results
REM first, then pyw results, and keeps the first one that validates.
setlocal
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_PATH=%SCRIPT_DIR%claude_quota.py"
set "PYTHON_FULL_PATH="

for /f "delims=" %%P in ('where pythonw 2^>nul') do (
    if not defined PYTHON_FULL_PATH (
        "%%P" -c "raise SystemExit(0)" >nul 2>nul
        if not errorlevel 1 set "PYTHON_FULL_PATH=%%P"
    )
)

if not defined PYTHON_FULL_PATH (
    for /f "delims=" %%P in ('where pyw 2^>nul') do (
        if not defined PYTHON_FULL_PATH (
            "%%P" -c "raise SystemExit(0)" >nul 2>nul
            if not errorlevel 1 set "PYTHON_FULL_PATH=%%P"
        )
    )
)

if not defined PYTHON_FULL_PATH (
    echo.
    echo Could not find a working Python interpreter: pythonw or pyw.
    echo Install Python from https://www.python.org/downloads/ and be sure to
    echo tick "Add python.exe to PATH" during setup, then run this script again.
    echo.
    pause
    exit /b 1
)

echo Registering collector with interpreter: %PYTHON_FULL_PATH%

schtasks /Create /TN "ClaudeQuotaCollector" /TR "\"%PYTHON_FULL_PATH%\" \"%SCRIPT_PATH%\"" /SC ONLOGON /RL LIMITED /F 2>nul
if not errorlevel 1 (
    echo Registered autostart via Task Scheduler.
    goto :registered
)

echo Task Scheduler needs admin rights - using the per-user Run key instead.

reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ClaudeQuotaCollector /t REG_SZ /d "\"%PYTHON_FULL_PATH%\" \"%SCRIPT_PATH%\"" /f
if not errorlevel 1 (
    echo Registered autostart via registry Run key.
    goto :registered
)

echo.
echo Could not register autostart via Task Scheduler or the registry Run key.
echo.
pause
exit /b 1

:registered
start "" "%PYTHON_FULL_PATH%" "%SCRIPT_PATH%"
echo Collector started now. Log: %SCRIPT_DIR%collector.log
endlocal
