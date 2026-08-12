@echo off
REM Removes both autostart mechanisms install_startup.bat may have used
REM (Task Scheduler task and/or HKCU Run registry value - either or both may
REM be present depending on which one succeeded at install time), then stops
REM any currently-running collector process.
setlocal

schtasks /Delete /TN "ClaudeQuotaCollector" /F 2>nul
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ClaudeQuotaCollector /f 2>nul

echo Stopping any running Claude Quota collector instances...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*claude_quota.py*'} | ForEach-Object {Stop-Process -Id $_.ProcessId -Force}"

echo Claude Quota collector startup entries removed (Task Scheduler and registry Run key) and collector stopped.
endlocal
