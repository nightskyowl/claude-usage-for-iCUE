@echo off
REM Runs the Claude Quota collector in diagnostic mode: does NOT start the
REM server and does NOT call the (rate-limited) usage endpoint. Writes a
REM sanitized data\diag.json describing local credential state, with no
REM token text ever included.
REM
REM Plain "python" is often the Microsoft Store's "App execution alias"
REM stub on Windows: it prints "Python was not found" and exits with code
REM 9009 even when a real Python is installed. To dodge that, this script
REM prefers a console-capable interpreter that actually works -- "py -3",
REM then "python" -- probing each with a harmless exit-code test before
REM use, so any real error is still visible in this console window. If
REM neither works, it falls back to pythonw (no console output, but
REM data\diag.json still gets written) so diagnostics can still run.
setlocal
cd /d "%~dp0"

py -3 -c "raise SystemExit(0)" >nul 2>nul
if not errorlevel 1 (
    set "PYCMD=py -3"
    goto :run_console
)

python -c "raise SystemExit(0)" >nul 2>nul
if not errorlevel 1 (
    set "PYCMD=python"
    goto :run_console
)

where pythonw >nul 2>nul
if errorlevel 1 goto :no_python

echo No console-capable Python found (py/python missing or shadowed by the
echo Microsoft Store stub). Running diagnostics via pythonw instead --
echo no output will appear in this window, only in data\diag.json.
del "%~dp0..\data\diag.json" >nul 2>nul
pythonw "%~dp0claude_quota.py" --diag
timeout /t 3 /nobreak >nul
if exist "%~dp0..\data\diag.json" (
    echo diag written to data\diag.json
) else (
    echo diag failed -- data\diag.json was not created.
    pause
)
goto :done

:run_console
%PYCMD% "%~dp0claude_quota.py" --diag
if errorlevel 1 (
    echo.
    echo diag failed -- see the error above.
    pause
) else (
    echo diag written to data\diag.json
)
goto :done

:no_python
echo.
echo Could not find a working Python interpreter (py, python, or pythonw).
echo Install Python from https://www.python.org/downloads/ and be sure to
echo tick "Add python.exe to PATH" during setup, then run this script again.
echo.
pause

:done
endlocal
