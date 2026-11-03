@echo off
title DupeClean (Debug / Console Mode)
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment .venv not found in %~dp0
    echo Please create the virtual environment and install dependencies.
    pause
    exit /b 1
)

echo Starting DupeClean in console mode...
".venv\Scripts\python.exe" main.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo Application exited with error code %ERRORLEVEL%.
    pause
)
