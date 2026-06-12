@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  LVS Selection Assist  --  Launcher  v1.0.8
::  Codename: "Aesthetic-Darwinism"
::  License : AGPL-3.0-or-later
::  Author  : ArcticWinter
::
::  Launches lvs_main.py -- which decides between SETUP-ONLY mode
::  (no select1..5 folders exist) and FULL RUN mode (cull is
::  underway).  In FULL RUN mode the Qt6 overlay always starts;
::  the Tkinter tasker auto-opens iff FastStone is already running
::  AND at least one picture is sorted into any select bucket.
::
::  Architecture in this version:
::    lvs_main.py          -- entry point + launch gate
::    lvs_backend.py       -- DB, AHK mgr, IPC pipe, raws, FSDB patcher
::    lvs_faststone.py     -- FastStone-specific viewer adapter
::    lvs_overlay_gui.py   -- Qt6 HUD overlay (viewer-invariant)
::    lvs_tasker_gui.py    -- Tkinter tasker (testbed v1)
::    lvs_setup_shortcuts.ahk -- culling hotkeys 1..5 + setup wizard
::
::  NOTE: This file is intentionally pure ASCII.  cmd.exe parses .bat
::  files in the OEM codepage (cp437/cp850), and any UTF-8 multi-byte
::  character inside a parenthesised IF block silently corrupts the
::  cached block, causing false "MISSING dependency" reports.
::  v1.0.3 hotfix: every em-dash, curly quote, and Unicode arrow has
::  been replaced with plain ASCII equivalents.
:: ============================================================

:: --- 1. Elevate to Administrator if not already ---
NET SESSION >nul 2>&1
if %errorLevel% NEQ 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process cmd -ArgumentList '/c """"%~dpnx0""""' -Verb RunAs"
    exit /b
)

echo.
echo ============================================================
echo   LVS Selection Assist  --  Launcher  v1.0.8
echo   (C) 2026 ArcticWinter  --  AGPL-3.0-or-later
echo ============================================================
echo.

:: --- 2. Check Python ---
python --version >nul 2>&1
if %errorLevel% NEQ 0 (
    echo [ERROR] Python not found in PATH.
    echo         https://www.python.org/downloads/  -- check "Add to PATH"
    echo.
    pause
    exit /b 1
)

:: --- 3. Check Python dependencies ---
::
:: IMPORTANT: each dep check writes to its OWN flag variable instead of a
:: shared MISSING counter, then we OR them together AFTER the parenthesised
:: blocks have completed.  This avoids the delayed-expansion edge case where
:: a parse error inside a () block leaves !MISSING! unbound when tested.
::
set MISS_PYQT=0
set MISS_PYWIN=0
set MISS_TK=0

python -c "import PyQt6" >nul 2>&1
if errorlevel 1 set MISS_PYQT=1

python -c "import win32gui, win32pipe, win32file" >nul 2>&1
if errorlevel 1 set MISS_PYWIN=1

python -c "import tkinter" >nul 2>&1
if errorlevel 1 set MISS_TK=1

:: Now aggregate.  Use plain percent-expansion (these have all been set in
:: the top-level scope, no delayed-expansion gymnastics needed).
set ANY_MISSING=0
if "%MISS_PYQT%"=="1"  set ANY_MISSING=1
if "%MISS_PYWIN%"=="1" set ANY_MISSING=1
if "%MISS_TK%"=="1"    set ANY_MISSING=1

if "%ANY_MISSING%"=="1" (
    echo.
    echo [DEPENDENCY CHECK FAILED]
    if "%MISS_PYQT%"=="1"  echo   [MISSING] PyQt6     -- pip install PyQt6
    if "%MISS_PYWIN%"=="1" echo   [MISSING] pywin32   -- pip install pywin32
    if "%MISS_TK%"=="1"    echo   [MISSING] tkinter   -- re-install Python with the tcl/tk option
    echo.
    echo [ACTION] Install missing packages then re-run this launcher:
    echo              pip install PyQt6 pywin32
    echo.
    echo [DIAGNOSTIC] If you believe these packages ARE installed, run:
    echo              python -c "import PyQt6, win32gui, win32pipe, win32file, tkinter; print('all OK')"
    echo          and confirm the same 'python' on PATH is the one with them.
    echo.
    pause
    exit /b 1
)

echo [OK] Python dependencies satisfied.
echo.

:: --- 4. Locate AutoHotkey v2 (informational only) ---
set AHK_EXE=

for %%P in (
    "%ProgramFiles%\AutoHotkey\v2\AutoHotkey64.exe"
    "%ProgramFiles%\AutoHotkey\v2\AutoHotkey32.exe"
    "%ProgramFiles%\AutoHotkey\AutoHotkey64.exe"
    "%ProgramFiles%\AutoHotkey\AutoHotkey.exe"
    "%ProgramFiles(x86)%\AutoHotkey\v2\AutoHotkey64.exe"
    "%ProgramFiles(x86)%\AutoHotkey\AutoHotkey.exe"
    "%LocalAppData%\Programs\AutoHotkey\v2\AutoHotkey64.exe"
    "%LocalAppData%\Programs\AutoHotkey\AutoHotkey.exe"
) do (
    if exist %%P (
        set AHK_EXE=%%P
        goto :AHK_FOUND
    )
)

where AutoHotkey64.exe >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%i in ('where AutoHotkey64.exe') do set AHK_EXE=%%i
    goto :AHK_FOUND
)
where AutoHotkey.exe >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%i in ('where AutoHotkey.exe') do set AHK_EXE=%%i
    goto :AHK_FOUND
)

echo [WARNING] AutoHotkey v2 not found.
echo           Hotkeys 1-5 will be DISABLED until you install it from:
echo           https://www.autohotkey.com/v2/
echo.
goto :START

:AHK_FOUND
echo [OK] AutoHotkey found: %AHK_EXE%
echo      (lvs_main.py launches the macro itself so it dies with LVS.)
echo.

:START
echo [RUN] Starting LVS Selection Assist (Ctrl+C in this window to stop)...
echo.
python "%~dp0lvs_main.py"

echo.
echo [LVS] Exited.  AHK macro has been terminated.
pause
