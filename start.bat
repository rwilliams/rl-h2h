@echo off
REM Launches the head-to-head overlay without a console window.
REM Place a shortcut to this file in shell:startup to auto-run with Windows.
cd /d "%~dp0"

REM Resolve python.exe once, then derive pythonw.exe from the SAME install.
REM Otherwise `where pythonw` can pick a different Python on PATH than `where
REM python` does, and `inputs` (gamepad support) may be installed in only one
REM of them — controller bindings then silently no-op under pythonw because
REM sys.stderr is None and the ImportError diagnostic is swallowed.
set "PY_EXE="
for /f "delims=" %%i in ('where python.exe 2^>nul') do if not defined PY_EXE set "PY_EXE=%%i"

set "PY_DIR="
set "PYW_EXE="
if defined PY_EXE for %%i in ("%PY_EXE%") do set "PY_DIR=%%~dpi"
if defined PY_DIR if exist "%PY_DIR%pythonw.exe" set "PYW_EXE=%PY_DIR%pythonw.exe"

REM Self-update: silent, ~1s, never blocks the launch on failure.
if defined PY_EXE "%PY_EXE%" "%~dp0updater.py" 2>nul

set "PYTHONPATH=%~dp0src"

if defined PYW_EXE (
    start "" "%PYW_EXE%" -m rl_h2h
    exit /b
)

REM Fallback: py -w (Python launcher), then pythonw on PATH. These don't
REM guarantee the same install as `python`, so `inputs` may be missing.
where py >nul 2>&1
if %errorlevel%==0 (
    start "" py -w -m rl_h2h
    exit /b
)

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw -m rl_h2h
    exit /b
)

echo Python is not on PATH.
echo Install Python 3.10+ from https://www.python.org/downloads/
echo and tick "Add Python to PATH" during install.
pause
