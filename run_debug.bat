@echo off
title DupeClean (Modo Debug / Console)
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Ambiente virtual .venv nao encontrado!
    pause
    exit /b 1
)

echo Iniciando DupeClean em modo de console...
".venv\Scripts\python.exe" main.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo A aplicacao encerrou com erro %ERRORLEVEL%.
    pause
)
