@echo off
REM Launches the head-to-head overlay without a console window.
REM Place a shortcut to this file in shell:startup to auto-run with Windows.
cd /d "%~dp0"

REM Self-update: silent, ~1s, never blocks the launch on failure.
where python >nul 2>&1 && python "%~dp0updater.py" 2>nul

set "PYTHONPATH=%~dp0src"

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw -m rl_h2h
    exit /b
)

where py >nul 2>&1
if %errorlevel%==0 (
    start "" py -w -m rl_h2h
    exit /b
)

echo Python is not on PATH.
echo Install Python 3.10+ from https://www.python.org/downloads/
echo and tick "Add Python to PATH" during install.
pause
