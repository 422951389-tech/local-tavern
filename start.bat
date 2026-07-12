@echo off
chcp 65001 >nul
cd /d C:\local-tavern

echo ============================================
echo   Local Tavern - 本地酒馆（前台模式·可见日志）
echo ============================================
echo.
echo [说明] 此为前台模式，保留窗口以便查看日志。
echo        无窗口静默启动：双击桌面快捷方式 或 run_hidden.vbs
echo        停止全屏模式：按 Ctrl+C；静默模式用 stop_tavern.bat
echo.

REM 检查 Python
where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.11+
    echo 下载：https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 检查依赖
python -c "import fastapi, uvicorn, httpx, yaml, pydantic" >nul 2>&1
if errorlevel 1 (
    echo [提示] 首次启动，正在安装依赖…
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
)

REM 检查 Ollama
curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo [警告] Ollama 未运行或不可达
    echo 请确认 Ollama 已启动，监听 http://localhost:11434
    echo.
) else (
    echo [OK] Ollama 可达
)

echo [启动] 正在启动服务…
echo [访问] 服务就绪后浏览器会自动打开 http://localhost:8765
echo [停止] 按 Ctrl+C 关闭
echo.

REM 后台异步开浏览器：等 3 秒让 uvicorn 起来再开
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8765"

python -X utf8 -m uvicorn server:app --host 127.0.0.1 --port 8765 --workers 1

pause
