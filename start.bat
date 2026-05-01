@echo off
REM Launches the head-to-head overlay without a console window.
REM Place a shortcut to this file in shell:startup to auto-run with Windows.
cd /d "%~dp0"

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw rl_h2h.py
    exit /b
)

where py >nul 2>&1
if %errorlevel%==0 (
    start "" py -w rl_h2h.py
    exit /b
)

echo Python is not on PATH.
echo Install Python 3.10+ from https://www.python.org/downloads/
echo and tick "Add Python to PATH" during install.
pause
