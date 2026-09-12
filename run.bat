@echo off
setlocal

rem ============================================================================
rem Toolchain Path Manager - launcher
rem
rem This script MUST be used with a virtual environment whose directory name is
rem exactly ".venv" (fixed relative path), for example:
rem     py -3.14 -m venv .venv
rem     .venv\Scripts\python.exe -m pip install -r requirements.txt
rem ============================================================================

cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] Virtual environment interpreter not found: %PY%
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

"%PY%" "%~dp0toolchain_path_manager.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo [ERROR] The program exited with code %CODE%.
    pause
)
exit /b %CODE%
