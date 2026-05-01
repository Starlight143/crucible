@echo off
setlocal enabledelayedexpansion
title Crucible WebUI

cd /d "%~dp0"

:: ============================================================
::  Locate Python (check venv first, then system PATH)
:: ============================================================
set "PYTHON="

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON=%~dp0.venv\Scripts\python.exe"
    goto :py_found
)
if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON=%~dp0venv\Scripts\python.exe"
    goto :py_found
)
if exist "%~dp0.venv\Scripts\python3.exe" (
    set "PYTHON=%~dp0.venv\Scripts\python3.exe"
    goto :py_found
)

for %%c in (python python3 py) do (
    where %%c >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON=%%c"
        goto :py_found
    )
)

echo.
echo [ERROR] Python not found.
echo Install Python 3.10+ or activate your virtual environment first.
echo.
pause
exit /b 1

:py_found
echo [OK] Python found: %PYTHON%
echo [INFO] Starting WebUI...
echo.

"%PYTHON%" "%~dp0launch_webui.py"

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] WebUI exited. See output above for details.
    pause
)
