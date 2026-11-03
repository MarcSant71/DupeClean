@echo off
title DupeClean - Build Portable 7z Package
cd /d "%~dp0"

echo =========================================================
echo       DupeClean - Portable Release Packager
echo =========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment .venv not found.
    echo Please set up your environment before building.
    pause
    exit /b 1
)

:: 1. Check or install PyInstaller
echo [1/4] Checking PyInstaller...
".venv\Scripts\python.exe" -m pip show pyinstaller >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo PyInstaller not found. Installing in .venv...
    ".venv\Scripts\python.exe" -m pip install pyinstaller
)

:: 2. Freeze Python runtime into dist\DupeClean
echo.
echo [2/4] Building standalone distribution with PyInstaller...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm dupeclean.spec
if %ERRORLEVEL% neq 0 (
    echo [ERROR] PyInstaller compilation failed!
    pause
    exit /b 1
)

if not exist "dist\DupeClean\DupeClean.exe" (
    echo [ERROR] DupeClean.exe was not found in dist\DupeClean.
    pause
    exit /b 1
)

:: 3. Check compression utility (7-Zip is required for LZMA2 solid compression)
echo.
echo [3/4] Compressing portable archive using 7-Zip LZMA2...
set "OUTPUT_7Z=dist\DupeClean-v1.0.0-windows-cuda.7z"
if exist "%OUTPUT_7Z%" del /f /q "%OUTPUT_7Z%"
if exist "dist\DupeClean-v1.0.0-windows-cuda.zip" del /f /q "dist\DupeClean-v1.0.0-windows-cuda.zip"

set "SEVEN_ZIP="
if exist "C:\Program Files\7-Zip\7z.exe" set "SEVEN_ZIP=C:\Program Files\7-Zip\7z.exe"
if exist "C:\Program Files (x86)\7-Zip\7z.exe" set "SEVEN_ZIP=C:\Program Files (x86)\7-Zip\7z.exe"

if defined SEVEN_ZIP (
    echo Using 7-Zip LZMA2 Ultra Solid compression (fits under GitHub 2GB limit)...
    cd dist
    "%SEVEN_ZIP%" a -t7z -m0=lzma2 -mx=9 -ms=on "DupeClean-v1.0.0-windows-cuda.7z" "DupeClean"
    cd ..
) else (
    echo [ERROR] 7-Zip is required to compress CUDA packages below GitHub's 2GB limit!
    echo Standard ZIP format produces ~2.8 GB, which GitHub Releases rejects.
    echo Please install 7-Zip from https://www.7-zip.org/
    pause
    exit /b 1
)

if not exist "%OUTPUT_7Z%" (
    echo [ERROR] Failed to create portable .7z archive.
    pause
    exit /b 1
)

:: 4. Success telemetry
echo.
echo =========================================================
echo [SUCCESS] Portable .7z package created successfully!
echo File: %CD%\%OUTPUT_7Z%
echo =========================================================
echo.
echo NEXT STEP - Upload to GitHub Releases:
echo 1. Open: https://github.com/MarcSant71/DupeClean/releases/new
echo 2. Tag: v1.0.0
echo 3. Title: DupeClean v1.0.0 - Fast GPU File Deduplication
echo 4. Drag and drop '%OUTPUT_7Z%' into the 'Attach binaries' box.
echo 5. Click 'Publish release'.
echo.
pause
