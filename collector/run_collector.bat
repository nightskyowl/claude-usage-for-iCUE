@echo off
REM Runs the Claude Quota collector from this script's own directory,
REM so double-clicking works regardless of the current working directory.
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" pythonw "%~dp0claude_quota.py"
) else (
    python "%~dp0claude_quota.py"
)

endlocal
