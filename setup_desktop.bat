@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 项目运行环境不存在，请先执行 setup.bat。
    pause
    exit /b 1
)

if not exist ".venv-desktop\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m venv ".venv-desktop"
    if errorlevel 1 goto :failed
)

".venv-desktop\Scripts\python.exe" -m pip install --disable-pip-version-check --require-hashes -r requirements-desktop.hashes.txt
if errorlevel 1 goto :failed

".venv-desktop\Scripts\python.exe" tools\dependency_locks.py --environment desktop
if errorlevel 1 goto :failed

echo [完成] 桌面构建环境已按哈希锁安装。
pause
exit /b 0

:failed
echo [错误] 桌面构建环境安装失败。
pause
exit /b 1
