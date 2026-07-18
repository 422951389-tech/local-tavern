@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"

where py >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Windows Python Launcher（py.exe）。
    exit /b 1
)

py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python 3.12。请先安装 Python 3.12。
    exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [环境] 正在创建独立 Python 3.12 虚拟环境……
    py -3.12 -m venv "%~dp0.venv"
    if errorlevel 1 exit /b 1
)

"%~dp0.venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [错误] 现有 .venv 不是 Python 3.12；未修改该环境。
    exit /b 1
)

echo [依赖] 按精确运行时锁安装……
"%~dp0.venv\Scripts\python.exe" -m pip install --no-deps --requirement "%~dp0requirements.lock.txt"
if errorlevel 1 exit /b 1

"%~dp0.venv\Scripts\python.exe" -m pip check
if errorlevel 1 exit /b 1

"%~dp0.venv\Scripts\python.exe" -c "from core.runtime_validation import assert_runtime; assert_runtime()"
if errorlevel 1 exit /b 1

echo [完成] Python 3.12 运行环境与精确依赖锁校验通过。
exit /b 0
