@echo off
chcp 65001 >nul
cd /d C:\local-tavern
echo ============================================
echo   Stop Local Tavern
echo ============================================

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found. No process was stopped.
    set "EXIT_CODE=1"
    goto :finish
)

python -X utf8 -m core.process_guard stop
set "EXIT_CODE=%ERRORLEVEL%"

:finish
echo.
if not "%~1"=="--no-pause" pause
exit /b %EXIT_CODE%
