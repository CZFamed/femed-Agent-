@echo off
rem ASCII only: Chinese text is produced by Python, not by cmd.
chcp 65001 >nul
cd /d "%~dp0"
title Pulse Vision Model Check
if exist ".venv\Scripts\python.exe" goto run
echo.
echo [ERROR] Python runtime not found: .venv\Scripts\python.exe
echo Please copy the WHOLE folder including the .venv directory, then try again.
echo.
pause
exit /b 1

:run
".venv\Scripts\python.exe" -m pulse.console.launcher --check-vision
pause
