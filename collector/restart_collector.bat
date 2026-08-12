@echo off
REM Stops any currently-running Claude Quota collector process, then starts a
REM fresh one from this script's own directory. Use this after the token in
REM %USERPROFILE%\.claude\.credentials.json changes underneath a running
REM collector, after a code update, or whenever run_collector.bat reports the
REM port is already in use.
setlocal
cd /d "%~dp0"

echo Stopping any running Claude Quota collector instances...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*claude_quota.py*'} | ForEach-Object {Stop-Process -Id $_.ProcessId -Force}"

REM Give the OS a moment to release the port before we rebind it.
timeout /t 2 /nobreak >nul

echo Starting Claude Quota collector...
where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" pythonw "%~dp0claude_quota.py"
) else (
    python "%~dp0claude_quota.py"
)

echo log: collector.log

endlocal
