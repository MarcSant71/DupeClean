@echo off
title DupeClean
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERRO] Ambiente virtual .venv nao encontrado em %~dp0
    pause
    exit /b 1
)

:: Inicia a aplicacao com pythonw (sem janela preta de fundo)
start "" ".venv\Scripts\pythonw.exe" main.py
