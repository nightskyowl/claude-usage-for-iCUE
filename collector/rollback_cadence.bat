@echo off
REM ---------------------------------------------------------------------------
REM Phase 2b rollback, level 1 (the fast one): put the collector back on its
REM default 900-second cadence.
REM
REM The faster cadence is opt-in via the CLAUDE_QUOTA_POLL_SECONDS environment
REM variable and the code default is still 900, so removing that one variable
REM is a COMPLETE rollback of the polling rate. No code change, no redeploy,
REM no git operation, nothing to reinstall.
REM
REM Reach for this the moment collector.log shows 429s or the widget starts
REM going blank. Everything else (idle pausing, the reset-aware scheduling,
REM the decoupled backoff) keeps working -- they are all cadence-independent.
REM
REM Deeper rollbacks, if this is not enough:
REM   level 2  code   : git checkout phase-2a-stable
REM   level 3  widget : copy %LOCALAPPDATA%\ClaudeQuotaBackups\widget-1.0.3-<guid>\*
REM                     over %APPDATA%\Corsair\CUE5\html_widgets\<guid>\, then
REM                     fully quit iCUE from the SYSTEM TRAY and relaunch.
REM See docs\phase-2b-plan.md for the full procedure.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo.
echo Removing the CLAUDE_QUOTA_POLL_SECONDS override (persistent user setting)...
reg delete "HKCU\Environment" /v CLAUDE_QUOTA_POLL_SECONDS /f >nul 2>nul
if errorlevel 1 (
    echo   no persistent override was set - nothing to remove.
) else (
    echo   removed.
)

REM Clear it for this process too, so the collector we launch below does not
REM simply inherit the value we just deleted from the registry.
set "CLAUDE_QUOTA_POLL_SECONDS="

echo.
echo Restarting the collector at the default cadence...
call "%~dp0restart_collector.bat"

echo.
echo Done. Confirm with the "starting: ... poll_seconds=900" line in collector.log.
echo Other shells and the next logon will pick up the removal automatically.
echo.
endlocal
