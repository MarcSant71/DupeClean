@echo off
title DupeClean
cd /d "%~dp0.."

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] Virtual environment .venv not found in %CD%
    echo Please create the virtual environment and install dependencies.
    pause
    exit /b 1
)

:: Launch application with pythonw (no background console window)
start "" ".venv\Scripts\pythonw.exe" main.py
