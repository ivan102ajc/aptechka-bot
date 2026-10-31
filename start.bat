@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Бот Аптечка

where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python не найден. Установите его с https://www.python.org/downloads/
  echo и при установке поставьте галочку "Add python.exe to PATH".
  pause
  exit /b
)

%PY% bot.py
pause
