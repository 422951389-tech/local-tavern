@echo off
chcp 65001 >nul
echo ============================================
echo   停止 本地酒馆 服务
echo ============================================

REM 找到占用 8765 端口的进程并结束它
echo 正在查找并结束占用 8765 端口的进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    echo 结束 PID %%a
    taskkill /F /PID %%a >nul 2>&1
)

REM 兜底：结束监听 8765 的 python 进程
taskkill /F /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq *" >nul 2>&1

echo.
echo [完成] 本地酒馆服务已停止
echo.
pause