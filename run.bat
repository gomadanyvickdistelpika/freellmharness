@echo off
REM ---------------------------------------------------------------
REM  AEGIS - launcher
REM  First run creates a venv, installs dependencies and downloads a
REM  Chromium for browser use (~3 minutes total).
REM  Every run after that starts in a couple of seconds.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo [AEGIS] First run - creating virtual environment...
    py -3 -m venv .venv || python -m venv .venv
    if errorlevel 1 (
        echo [AEGIS] Could not create a virtual environment.
        echo [AEGIS] Install Python 3.10+ from python.org and tick "Add to PATH".
        pause
        exit /b 1
    )
    echo [AEGIS] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [AEGIS] Dependency install failed. See the messages above.
        pause
        exit /b 1
    )

    echo [AEGIS] Downloading Chromium for browser use...
    ".venv\Scripts\python.exe" -m playwright install chromium
    if errorlevel 1 (
        echo [AEGIS] Chromium download failed - browser use will still work by
        echo [AEGIS] attaching to your own Chrome. Run this later to retry:
        echo [AEGIS]   .venv\Scripts\python.exe -m playwright install chromium
    )
)

".venv\Scripts\python.exe" -m aegis %*
if errorlevel 1 pause
endlocal
