@echo off
setlocal

rem ============================================================================
rem Toolchain Path Manager - launcher
rem
rem This script MUST be used with a virtual environment whose directory name is
rem exactly ".venv" (fixed relative path), for example:
rem     py -3.14 -m venv .venv
rem     .venv\Scripts\python.exe -m pip install -r requirements.txt
rem
rem The GUI is started via pythonw.exe in the background, so this console
rem window closes right after launching (no black window stays open).
rem ============================================================================

cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
    echo [ERROR] Virtual environment interpreter not found: %PYW%
    echo.
    echo This launcher must be used with a virtual environment, and the
    echo environment directory name must be exactly ".venv".
    echo Create it first in the script directory, then install dependencies:
    echo     py -3.14 -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

start "" "%PYW%" "%~dp0tool2path.py" %*
exit /b 0
