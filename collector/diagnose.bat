@echo off
REM Runs the Claude Quota collector in diagnostic mode: does NOT start the
REM server and does NOT call the (rate-limited) usage endpoint. Writes a
REM sanitized data\diag.json describing local credential state, with no
REM token text ever included. Uses plain "python" (not pythonw) so any
REM error is visible in this console window.
setlocal
cd /d "%~dp0"

python "%~dp0claude_quota.py" --diag
if errorlevel 1 (
    echo.
    echo diag failed -- see the error above.
    pause
) else (
    echo diag written to data\diag.json
)

endlocal
