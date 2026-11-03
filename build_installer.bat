@echo off
title DupeClean - Automated Build and Packaging
cd /d "%~dp0"

echo =========================================================
echo       DupeClean - Automated Installer Builder
echo =========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment .venv not found.
    echo Please set up your environment before building.
    pause
    exit /b 1
)

:: 1. Check or install PyInstaller
echo [1/3] Checking PyInstaller...
".venv\Scripts\python.exe" -m pip show pyinstaller >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Installing PyInstaller in .venv...
    ".venv\Scripts\python.exe" -m pip install pyinstaller
)

:: 2. Clean previous build directories
echo [2/3] Cleaning previous build artifacts...
if exist "build" rd /s /q "build"
if exist "dist\DupeClean" rd /s /q "dist\DupeClean"

:: 3. Freeze Python into standalone distribution via PyInstaller
echo.
echo [3/3] Freezing Python runtime and bundling CUDA dependencies...
".venv\Scripts\pyinstaller.exe" --noconfirm dupeclean.spec

if not exist "dist\DupeClean\DupeClean.exe" (
    echo.
    echo [ERROR] Build failed! DupeClean.exe was not generated.
    pause
    exit /b 1
)

echo.
echo =========================================================
echo [SUCCESS] Standalone binary distribution created at:
echo           %CD%\dist\DupeClean
echo =========================================================
echo.

:: 4. Inno Setup Compilation (if installed)
set "ISCC_PATH="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC_PATH=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set "ISCC_PATH=C:\Program Files\Inno Setup 6\ISCC.exe"

if defined ISCC_PATH (
    echo Compiling final Windows Installer with Inno Setup...
    "%ISCC_PATH%" installer.iss
    echo.
    echo =========================================================
    echo [INSTALLER READY] Setup executable created at:
    echo                   %CD%\dist\installer
    echo =========================================================
) else (
    echo [NOTICE] Inno Setup compiler (ISCC.exe) was not found in Program Files.
    echo To create a single Setup.exe installer wizard:
    echo 1. Download and install Inno Setup 6 (free): https://jrsoftware.org/isdl.php
    echo 2. Run this script again, or right-click 'installer.iss' and select 'Compile'.
    echo.
    echo In the meantime, you can distribute the standalone folder:
    echo 'dist\DupeClean' (or zip it as DupeClean-Portable.zip).
)

echo.
pause
