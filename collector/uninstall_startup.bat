@echo off
REM Removes the Claude Quota collector scheduled task created by install_startup.bat.
setlocal
schtasks /Delete /TN "ClaudeQuotaCollector" /F
if %ERRORLEVEL% NEQ 0 (
    echo No scheduled task named "ClaudeQuotaCollector" found (or delete failed).
    exit /b 1
)
echo Claude Quota collector startup task removed.
endlocal
