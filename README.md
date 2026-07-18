# 本地酒馆

本地运行的 AI 多角色扮演应用，通过 Ollama 调用本机模型。聊天、角色卡、世界书、存档和备份默认都保存在项目目录，不依赖云端服务。

## 首次安装

1. 安装 Python 3.12 与 Windows Python Launcher（`py.exe`）。
2. 双击 `setup.bat`。脚本会在项目目录创建独立 `.venv`，按 `requirements.lock.txt` 的精确版本安装并校验运行依赖。
3. 启动 Ollama，并确保至少有一个可用模型。
4. 双击 `start.bat`。

正常启动不会自动安装或升级依赖。缺少 `.venv`、Python 版本不符或依赖漂移时，启动器会停止并提示重新运行 `setup.bat`。

## 启动与停止

- `start.bat`：前台模式，显示日志；`Ctrl+C` 关闭。
- `run_hidden.vbs`：隐藏模式；浏览器会在服务返回本项目的存活标记后打开。
- `start_hidden.vbs`：兼容入口，转到 `run_hidden.vbs`。
- `stop_tavern.bat`：优先请求优雅关闭；超时后也只会结束 PID、命令、项目根和监听端口全部匹配的本项目进程。

默认地址为 `http://127.0.0.1:8765`。端口被其他程序占用时，启动器不会打开错误页面，也不会结束占用端口的外部进程。

隐藏模式日志默认写入 `logs/tavern.log`，UTF-8 编码，按 5 MiB × 5 个历史文件轮转。访问日志关闭，应用日志不记录聊天正文或流式响应正文。

## 健康检查

- `GET /health/live`：只证明本地酒馆进程存活，固定返回 200。
- `GET /health/ready`：检查数据目录读写、运行依赖、Ollama 和维护状态；任一未就绪时返回 503。

Ollama 未启动不会阻止进入界面，因此浏览器门禁使用 `/health/live`，不使用 readiness。

## 数据结构

```text
C:\local-tavern\
├── server.py
├── setup.bat
├── start.bat
├── run_hidden.vbs
├── stop_tavern.bat
├── requirements.txt           直接运行依赖
├── requirements.lock.txt      完整运行依赖闭包
├── core\                       领域与基础设施代码
├── routes\                     HTTP API
├── web\                        前端
├── prompts\                    Prompt 模板
├── data\projects\<项目>\
│   ├── characters\             项目共享角色卡
│   ├── worldbook\              项目共享世界书
│   ├── user.yaml               用户档案
│   └── saves\                  独立剧情存档
├── backups\                    全量备份与恢复演练记录
└── logs\                       隐藏模式日志
```

应用支持在界面中创建项目、角色、世界书和存档，不需要手工创建旧版 `data/characters`、`data/user` 或 `data/saves` 目录。

## 配置覆盖

配置通过环境变量在进程启动时读取；修改后必须重启服务。

| 变量 | 默认值或作用 |
|---|---|
| `TAVERN_HOST` | `127.0.0.1` |
| `TAVERN_PORT` | `8765` |
| `TAVERN_OLLAMA_HOST` | `http://localhost:11434` |
| `TAVERN_BASE_DIR` | 项目根目录 |
| `TAVERN_DATA_DIR` | `data` 目录 |
| `TAVERN_PROJECTS_DIR` | 项目数据目录 |
| `TAVERN_SETTINGS_PATH` | 设置文件 |
| `TAVERN_PROMPTS_DIR` | Prompt 目录 |
| `TAVERN_BACKUP_DIR` | 备份目录 |
| `TAVERN_LOG_DIR` / `TAVERN_LOG_FILE` | 日志目录与文件 |
| `TAVERN_PID_PATH` / `TAVERN_STOP_REQUEST_PATH` | 进程登记与停止请求文件 |
| `TAVERN_LOG_MAX_BYTES` / `TAVERN_LOG_BACKUP_COUNT` | 日志轮转上限 |
| `TAVERN_OLLAMA_HEALTH_TIMEOUT_MS` | Ollama 健康探测短超时 |

所有路径覆盖必须是绝对路径。Ollama 地址只接受无凭据、无查询参数的 HTTP(S) 根地址。

应用没有网络认证。绑定非回环地址时，除设置 `TAVERN_HOST` 外还必须显式设置 `TAVERN_ALLOW_REMOTE=true`；仅应在可信网络和明确理解数据暴露风险时使用。

## 运行依赖

项目要求 Python 3.12，直接依赖固定为：

- FastAPI 0.115.0
- Uvicorn 0.32.0
- HTTPX 0.27.0
- PyYAML 6.0.1
- Pydantic 2.9.0

完整传递依赖以 `requirements.lock.txt` 为准。开发锁目前覆盖 pytest 与 pytest-asyncio；coverage、Ruff 和 Playwright 尚未进入已验证的开发锁，不能据此声明完整浏览器与覆盖率门禁已建立。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q core routes tests server.py
.\.venv\Scripts\python.exe -m pip check
```

前端模块测试使用 Node.js 自带测试运行器，具体命令以仓库测试文件和项目上下文文档为准。

## 文档

- 项目内事实与交接文档：`AI_CONTEXT.md`
- 桌面项目事实文档：`本地酒馆搭建-AI上下文.md`
- 桌面优化进度文档：`本地酒馆-功能优化规划.md`

每个优化包完成后，同步更新上述事实与进度文档。

## 常见问题

- 端口被占用：在启动前设置新的 `TAVERN_PORT`，不要编辑 `server.py`。
- Ollama 未就绪：启动 Ollama，确认 `/api/tags` 能返回至少一个模型；界面仍可打开查看状态。
- 隐藏启动失败：先查看 `logs/tavern.log`；主日志路径不可用时，启动器依次回退到项目日志目录和系统临时目录下的 `local-tavern/tavern-bootstrap.log`。
- 角色声线混淆：检查角色卡的 `speaking_style`、`catchphrases` 与确定性声线策略。
- 输出结构不稳定：通过界面中的提示词编辑器检查 `system.md`、`group_chat.md` 和 `summary.md`。
