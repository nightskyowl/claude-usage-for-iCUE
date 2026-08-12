@echo off
REM Pushes this repository to GitHub. Uses Git for Windows + Git Credential
REM Manager: if you're not signed in yet, a browser window will pop up asking
REM you to authorize as your GitHub account. No token ever touches this repo.
setlocal
cd /d "%~dp0"

git --version >nul 2>nul
if errorlevel 1 (
    echo Git is not installed or not on PATH. Install it from https://git-scm.com/download/win
    pause
    exit /b 1
)

git remote get-url origin >nul 2>nul
if errorlevel 1 (
    git remote add origin https://github.com/nightskyowl/claude-usage-for-iCUE.git
) else (
    git remote set-url origin https://github.com/nightskyowl/claude-usage-for-iCUE.git
)

echo Pushing main to https://github.com/nightskyowl/claude-usage-for-iCUE ...
git push -u origin main
if errorlevel 1 (
    echo.
    echo Push failed - see the message above. If a browser auth window opened,
    echo complete it as nightskyowl and run this script again.
) else (
    echo.
    echo Push complete: https://github.com/nightskyowl/claude-usage-for-iCUE
)
pause
endlocal
