@echo off
rem ASCII only: cmd mis-parses batch files that contain non-ASCII bytes.
rem All Chinese messages are printed by Python (pulse.console.launcher).
chcp 65001 >nul
cd /d "%~dp0"
title Pulse Media Console
if exist ".venv\Scripts\python.exe" goto run
echo.
echo [ERROR] Python runtime not found: .venv\Scripts\python.exe
echo Please copy the WHOLE folder including the .venv directory, then try again.
echo.
pause
exit /b 1

:run
".venv\Scripts\python.exe" -m pulse.console.launcher
if errorlevel 1 pause
