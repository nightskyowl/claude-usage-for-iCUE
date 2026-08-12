@echo off
REM Runs the Claude Quota collector from this script's own directory,
REM so double-clicking works regardless of the current working directory.
REM
REM Picks the first interpreter that actually works. Plain "python" is
REM tried last because the Microsoft Store's "App execution alias" stub
REM shadows it on many machines (prints "Python was not found", exit code
REM 9009) even when a real Python is installed; pythonw/pyw/py are not
REM shadowed the same way. python is only used after probing it with a
REM harmless exit-code test.
setlocal
cd /d "%~dp0"

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
