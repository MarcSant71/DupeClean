@echo off
title DupeClean - Build Portable ZIP
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

:: 1. Freeze Python runtime into dist\DupeClean
echo [1/3] Building standalone distribution with PyInstaller...
call ".venv\Scripts\pyinstaller.exe" --noconfirm dupeclean.spec
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

:: 2. Check compression utility (7-Zip or PowerShell)
echo.
echo [2/3] Compressing portable archive...
set "OUTPUT_ZIP=dist\DupeClean-v1.0.0-windows-cuda.zip"
if exist "%OUTPUT_ZIP%" del /f /q "%OUTPUT_ZIP%"

set "SEVEN_ZIP="
if exist "C:\Program Files\7-Zip\7z.exe" set "SEVEN_ZIP=C:\Program Files\7-Zip\7z.exe"
if exist "C:\Program Files (x86)\7-Zip\7z.exe" set "SEVEN_ZIP=C:\Program Files (x86)\7-Zip\7z.exe"

if defined SEVEN_ZIP (
    echo Using 7-Zip for high-ratio LZMA compression...
    cd dist
    "%SEVEN_ZIP%" a -tzip -mx9 "DupeClean-v1.0.0-windows-cuda.zip" "DupeClean"
    cd ..
) else (
    echo 7-Zip not found, using PowerShell Compress-Archive...
    powershell -NoProfile -Command "Compress-Archive -Path 'dist\DupeClean' -DestinationPath 'dist\DupeClean-v1.0.0-windows-cuda.zip' -Force"
)

if not exist "%OUTPUT_ZIP%" (
    echo [ERROR] Failed to create portable archive.
    pause
    exit /b 1
)

:: 3. Success telemetry
echo.
echo =========================================================
echo [SUCCESS] Portable package created!
echo Location: %CD%\%OUTPUT_ZIP%
echo =========================================================
echo.
echo NEXT STEP - Upload to GitHub Releases:
echo 1. Open: https://github.com/MarcSant71/DupeClean/releases/new
echo 2. Tag: v1.0.0
echo 3. Title: DupeClean v1.0.0 - Fast GPU File Deduplication
echo 4. Drag and drop '%OUTPUT_ZIP%' into the 'Attach binaries' box.
echo 5. Click 'Publish release'.
echo.
pause
