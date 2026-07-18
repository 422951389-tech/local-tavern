@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not defined TAVERN_BASE_DIR set "TAVERN_BASE_DIR=%~dp0"
echo ============================================
echo   Stop Local Tavern
echo ============================================

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [ERROR] Project .venv was not found. No process was stopped.
    set "EXIT_CODE=1"
    goto :finish
)

"%~dp0.venv\Scripts\python.exe" -X utf8 -m core.process_guard stop
set "EXIT_CODE=%ERRORLEVEL%"

:finish
echo.
if not "%~1"=="--no-pause" pause
exit /b %EXIT_CODE%
