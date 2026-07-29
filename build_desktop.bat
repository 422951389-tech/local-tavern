@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"

if not exist ".venv-desktop\Scripts\python.exe" (
    echo [错误] 桌面构建环境不存在，请先执行 setup_desktop.bat。
    pause
    exit /b 1
)

".venv-desktop\Scripts\python.exe" tools\build_desktop.py --clean
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" (
    echo [完成] 双击 release\LocalTavern\LocalTavern.exe 即可独立使用。
) else (
    echo [错误] 桌面发行版构建失败。
)
pause
exit /b %EXIT_CODE%
