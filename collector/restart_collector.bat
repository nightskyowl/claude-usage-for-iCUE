@echo off
REM Stops any currently-running Claude Quota collector process, then starts a
REM fresh one from this script's own directory. Use this after the token in
REM %USERPROFILE%\.claude\.credentials.json changes underneath a running
REM collector, after a code update, or whenever run_collector.bat reports the
REM port is already in use.
REM
REM Picks the first interpreter that actually works. Plain "python" is
REM tried last because the Microsoft Store's "App execution alias" stub
REM shadows it on many machines (prints "Python was not found", exit code
REM 9009) even when a real Python is installed; pythonw/pyw/py are not
REM shadowed the same way. python is only used after probing it with a
REM harmless exit-code test.
setlocal
cd /d "%~dp0"

echo Stopping any running Claude Quota collector instances...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*claude_quota.py*'} | ForEach-Object {Stop-Process -Id $_.ProcessId -Force}"

REM Give the OS a moment to release the port before we rebind it.
timeout /t 2 /nobreak >nul

echo Starting Claude Quota collector...

REM Probe by executing, not "where": a Store stub exists on disk but fails to run.
pythonw -c "raise SystemExit(0)" >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0claude_quota.py"
    goto :started
)

pyw -3 -c "raise SystemExit(0)" >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "%~dp0claude_quota.py"
    goto :started
)

py -3 -c "raise SystemExit(0)" >nul 2>nul
if not errorlevel 1 (
    start "" py -3 "%~dp0claude_quota.py"
    goto :started
)

python -c "raise SystemExit(0)" >nul 2>nul
if not errorlevel 1 (
    start "" python "%~dp0claude_quota.py"
    goto :started
)

echo.
echo Could not find a working Python interpreter (pythonw, pyw, py, or python).
echo Install Python from https://www.python.org/downloads/ and be sure to
echo tick "Add python.exe to PATH" during setup, then run this script again.
echo.
pause
goto :done

:started
echo log: collector.log

:done
endlocal
