@echo off
REM Launches the head-to-head overlay without a console window.
REM Place a shortcut to this file in shell:startup to auto-run with Windows.
cd /d "%~dp0"
start "" pythonw rl_h2h.py
