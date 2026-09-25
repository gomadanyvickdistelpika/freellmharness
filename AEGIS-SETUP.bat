@echo off
REM ===================================================================
REM  AEGIS - one-click setup
REM
REM  Unpacks, installs, tests, makes a desktop icon, and starts the app.
REM  Everything it does is written to aegis-setup.log next to this file.
REM  Safe to run again: it picks up where it left off.
REM ===================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
REM %~dp0 ends with a backslash; "C:\path\" makes it escape the closing
REM quote, so tar sees  C:\path"  and fails. Strip it.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "LOG=%HERE%\aegis-setup.log"
set "ZIP=%HERE%\aegis-harness.zip"
set "APP=%HERE%\aegis"

echo ============================================
echo   AEGIS setup
echo   This takes about 5 minutes. Leave it running.
echo ============================================
echo.

echo ===== AEGIS setup started %DATE% %TIME% =====> "%LOG%"
echo Folder: %~dp0>> "%LOG%"

REM Files copied onto the machine can be flagged by Windows. Clear that.
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%HERE%' -Filter 'aegis*' -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

REM --- 1. Python ------------------------------------------------------
echo [1/7] Looking for Python...
echo.>> "%LOG%"
echo --- STEP 1: python --->> "%LOG%"
set "PY="
py -3 --version >> "%LOG%" 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python --version >> "%LOG%" 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo RESULT: FAIL - no Python on PATH.>> "%LOG%"
    echo.
    echo   PROBLEM: Python is not installed, or not on your PATH.
    echo.
    echo   FIX: Go to python.org/downloads, get Python 3.12,
    echo        and TICK "Add python.exe to PATH" on the first screen.
    echo        Then run this file again.
    echo.
    goto :stopped
)
echo       Found: !PY!
echo Using: !PY!>> "%LOG%"

REM --- 2. Unpack ------------------------------------------------------
echo [2/7] Unpacking...
echo.>> "%LOG%"
echo --- STEP 2: unpack --->> "%LOG%"
REM Downloaded from GitHub ("Code -> Download ZIP" and extracted): the app is
REM already here, so there is nothing to unpack.
if not exist "%ZIP%" if exist "%HERE%\run.bat" if exist "%HERE%\aegis\__init__.py" (
    echo Running from the downloaded folder - nothing to unpack.>> "%LOG%"
    echo       Nothing to unpack.
    set "APP=%HERE%"
    goto :deps
)
if not exist "%ZIP%" (
    echo RESULT: FAIL - aegis-harness.zip is not in this folder.>> "%LOG%"
    echo.
    echo   PROBLEM: this folder does not look like AEGIS.
    echo   Run AEGIS-SETUP.bat from inside the extracted freellmharness folder.
    goto :stopped
)
tar -xf "%ZIP%" -C "%HERE%" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo tar failed, trying PowerShell...>> "%LOG%"
    powershell -NoProfile -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%HERE%' -Force" >> "%LOG%" 2>&1
)
if not exist "%APP%\run.bat" (
    echo RESULT: FAIL - unpack produced no aegis\run.bat>> "%LOG%"
    echo.
    echo   PROBLEM: could not unpack the zip.
    goto :stopped
)
echo Unpacked to %APP%>> "%LOG%"
dir /b "%APP%" >> "%LOG%" 2>&1

:deps
REM --- 3. Dependencies -------------------------------------------------
echo [3/7] Installing dependencies ^(a few minutes^)...
echo.>> "%LOG%"
echo --- STEP 3: dependencies --->> "%LOG%"
cd /d "%APP%"
if not exist ".venv\Scripts\python.exe" (
    !PY! -m venv .venv >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo RESULT: FAIL - could not create the virtual environment.>> "%LOG%"
        echo.
        echo   PROBLEM: could not create the Python environment.
        goto :stopped
    )
)
set "VPY=%APP%\.venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip >> "%LOG%" 2>&1
"%VPY%" -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
    echo RESULT: FAIL - dependency install failed.>> "%LOG%"
    echo.
    echo   PROBLEM: installing the Python packages failed.
    echo   The reason is at the bottom of aegis-setup.log
    goto :stopped
)
echo       Done.
echo Dependencies installed.>> "%LOG%"
"%VPY%" -m pip list >> "%LOG%" 2>&1

REM --- 4. Chromium -----------------------------------------------------
echo [4/7] Downloading Chromium for browser use...
echo.>> "%LOG%"
echo --- STEP 4: chromium --->> "%LOG%"
"%VPY%" -m playwright install chromium >> "%LOG%" 2>&1
if errorlevel 1 (
    echo NOTE: Chromium download failed. Browser use can still attach to Chrome.>> "%LOG%"
    echo       Skipped ^(browser use will use your own Chrome^).
) else (
    echo Chromium ready.>> "%LOG%"
    echo       Done.
)

REM --- 5. Self-test ----------------------------------------------------
echo [5/7] Running the test suite...
echo.>> "%LOG%"
echo --- STEP 5: test suite --->> "%LOG%"
"%VPY%" -u tests\test_aegis.py > "%HERE%\aegis-tests.log" 2>&1
if errorlevel 1 (
    echo RESULT: tests reported failures - see aegis-tests.log>> "%LOG%"
    echo       Some tests failed - see aegis-tests.log
) else (
    echo Test suite passed.>> "%LOG%"
    echo       Passed.
)
powershell -NoProfile -Command "Get-Content -Tail 8 '%HERE%\aegis-tests.log'" >> "%LOG%" 2>&1

REM --- 6. Probe this machine -------------------------------------------
echo [6/7] Checking your hardware...
echo.>> "%LOG%"
echo --- STEP 6: this machine --->> "%LOG%"
"%VPY%" -c "import sys,json;sys.path.insert(0,'.');from aegis import hardware,skills;from aegis.tools import computer,browser;hw=hardware.probe().to_dict();c=computer.capability();print(json.dumps({'cpu':hw['cpu_name'],'ram_gb':hw['ram_total_gb'],'ram_free_gb':hw['ram_available_gb'],'gpu':hw['accelerator'],'vram_gb':hw['vram_total_gb'],'discrete_gpu':hw['has_discrete_gpu'],'disk_free_gb':hw['disk_free_gb'],'skills':len(skills.loadable()),'computer_use':c.detail,'screen':list(c.screen),'browser_ready':browser.available()[0]},indent=2))" >> "%LOG%" 2>&1

REM --- 7. Desktop icon --------------------------------------------------
echo [7/7] Creating the desktop icon...
echo.>> "%LOG%"
echo --- STEP 7: desktop icon --->> "%LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%APP%\make-shortcut.ps1" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo NOTE: shortcut creation failed - see the log.>> "%LOG%"
    echo       Could not create the icon. You can still start it from run.bat
) else (
    echo       Done - look on your Desktop for AEGIS.
)

echo.>> "%LOG%"
echo ===== SETUP FINISHED %DATE% %TIME% =====>> "%LOG%"

echo.
echo ============================================
echo   Finished. Starting AEGIS now.
echo   The AEGIS icon is on your Desktop.
echo ============================================
echo.
start "" "%APP%\.venv\Scripts\pythonw.exe" -m aegis
timeout /t 4 /nobreak >nul
goto :eof

:stopped
echo.>> "%LOG%"
echo ===== SETUP STOPPED %DATE% %TIME% =====>> "%LOG%"
echo.
echo   Setup stopped. Full detail is in aegis-setup.log
echo.
pause
