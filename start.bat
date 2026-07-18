@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not defined TAVERN_BASE_DIR set "TAVERN_BASE_DIR=%~dp0"

echo ============================================
echo   Local Tavern - 本地酒馆（前台模式·可见日志）
echo ============================================
echo.
echo [说明] 此为前台模式，保留窗口以便查看日志。
echo        无窗口静默启动：双击桌面快捷方式 或 run_hidden.vbs
echo        停止全屏模式：按 Ctrl+C；静默模式用 stop_tavern.bat
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 项目独立 .venv 不存在。
    echo [操作] 请先双击 setup.bat 安装 Python 3.12 运行环境。
    pause
    exit /b 1
)

echo [启动] 正在启动服务…
echo [访问] 浏览器只会在 /health/live 返回本项目标记后打开
echo [停止] 按 Ctrl+C 关闭
echo.

"%~dp0.venv\Scripts\python.exe" -X utf8 -m core.launcher serve --open-browser --workers 1
set "EXIT_CODE=%ERRORLEVEL%"

pause
exit /b %EXIT_CODE%
