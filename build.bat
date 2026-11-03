@echo off
title DupeClean - Build Standalone Portable Release
cd /d "%~dp0"

echo =========================================================
echo       DupeClean - Standalone Portable Release Builder
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

:: 2. Clean previous build artifacts
echo [2/3] Cleaning previous build artifacts...
if exist "build" rd /s /q "build"
if exist "dist\DupeClean" rd /s /q "dist\DupeClean"

:: 3. Freeze Python runtime into dist\DupeClean via PyInstaller
echo.
echo [3/3] Freezing standalone distribution with PyInstaller...
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

:: 4. Compress portable archive using 7-Zip LZMA2
echo.
echo Compressing portable archive using 7-Zip LZMA2 solid mode...

for /f "tokens=*" %%V in ('"%PYTHON_EXE%" -c "import dupeclean; print(dupeclean.__version__)"') do set "APP_VERSION=%%V"
if not defined APP_VERSION set "APP_VERSION=1.0.0"

set "OUTPUT_7Z=dist\DupeClean-v%APP_VERSION%-windows-cuda.7z"
if exist "%OUTPUT_7Z%" del /f /q "%OUTPUT_7Z%"

set "SEVEN_ZIP="
if exist "C:\Program Files\7-Zip\7z.exe" set "SEVEN_ZIP=C:\Program Files\7-Zip\7z.exe"
if exist "C:\Program Files (x86)\7-Zip\7z.exe" set "SEVEN_ZIP=C:\Program Files (x86)\7-Zip\7z.exe"

if defined SEVEN_ZIP (
    cd dist
    "%SEVEN_ZIP%" a -t7z -m0=lzma2 -mx=9 -ms=on "DupeClean-v%APP_VERSION%-windows-cuda.7z" "DupeClean"
    cd ..
) else (
    echo [WARNING] 7-Zip not found in Program Files.
    echo Standalone folder created at: dist\DupeClean
    echo Install 7-Zip from https://www.7-zip.org/ to generate compressed release archives.
    pause
    exit /b 0
)

if not exist "%OUTPUT_7Z%" (
    echo [ERROR] Failed to create portable .7z archive.
    pause
    exit /b 1
)

echo.
echo =========================================================
echo [SUCCESS] Portable package created successfully!
echo Archive: %CD%\%OUTPUT_7Z%
echo =========================================================
echo.
pause
