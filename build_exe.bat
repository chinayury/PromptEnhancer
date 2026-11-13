@echo off
rem ============================================
rem  PromptForge build script (Windows)
rem  Output: dist\PromptForge.exe (PyInstaller onefile)
rem  Reference: docs/sdlc/05-deployment.md section 3
rem
rem  Usage:
rem    build_exe.bat                            (default: C:\Python314\python.exe)
rem    build_exe.bat D:\Python314\python.exe     (pass python path as arg 1)
rem    set PROMPTFORGE_PYTHON=... && build_exe.bat
rem
rem  NOTE: keep this file ASCII + LF. Chinese text in a .bat is decoded
rem        with the system ANSI codepage; a UTF-8 file gets mis-parsed and
rem        lines break. See project rule: batch files stay ASCII.
rem ============================================
setlocal
cd /d "%~dp0."

if not "%~1"=="" (set "PY=%~1") else if not "%PROMPTFORGE_PYTHON%"=="" (set "PY=%PROMPTFORGE_PYTHON%") else (set "PY=C:\Python314\python.exe")
if not exist "%PY%" (
    echo [ERROR] System Python 3.14 not found: %PY%
    echo         Hosted Python 3.13 has no tkinter and cannot build the GUI.
    echo         Edit set PY=... in this script, or pass the path as arg 1.
    exit /b 1
)

rem ---- Clear safe-delete session vars (else PyInstaller gets interrupted) ----
set CODEBUDDY_SESSION_ID=
set CLAUDE_SESSION_ID=

rem ---- Isolated build venv (.build_venv is gitignored, safe to delete) ----
if not exist ".build_venv\Scripts\python.exe" (
    echo [1/3] Creating build venv .build_venv ...
    "%PY%" -m venv .build_venv || exit /b 1
)
echo [2/3] Installing customtkinter + pyinstaller ...
".build_venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q customtkinter pyinstaller || exit /b 1

rem ---- PyInstaller onefile build (takes about 2-5 minutes) ----
echo [3/3] Running PyInstaller ...
".build_venv\Scripts\python.exe" -m PyInstaller --onefile --noconsole --name PromptForge --clean ^
  --paths src ^
  --add-data "src/promptforge/config/providers.json;promptforge/config" ^
  --collect-submodules promptforge ^
  --collect-submodules forge ^
  --collect-data customtkinter ^
  src/promptforge/__main__.py || exit /b 1

if not exist "dist\PromptForge.exe" (
    echo [ERROR] Build finished but dist\PromptForge.exe is missing. Check the log above.
    exit /b 1
)
echo.
echo [OK] Build complete: %cd%\dist\PromptForge.exe
echo      Ship the single exe; the data\ folder is auto-created next to it on first run.
exit /b 0
