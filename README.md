# 本地酒馆

本地酒馆是一款 Windows AI 多角色叙事工作台。正式使用入口是独立桌面程序：双击 `release\LocalTavern\LocalTavern.exe` 即可打开，不需要安装 Python、执行 `setup.bat`、手工启动本项目服务、占用本机 TCP 端口或打开外部浏览器。

角色卡、世界书、存档、Prompt、备份和日志保存在本机。推理来源可选本机 Ollama、OpenAI-compatible 云端 API 或 Anthropic Messages API；只有明确选择云端来源后，本轮所需剧情上下文才会发往对应服务商。

## 直接使用

1. 保留 `release\LocalTavern` 整个目录，不能只移动其中的 EXE。
2. 双击 `release\LocalTavern\LocalTavern.exe`。
3. 首次运行会把旧版 `C:\local-tavern` 中的 `data`、`backups` 和用户 Prompt 安全复制到 `%LOCALAPPDATA%\LocalTavern`；源目录不删除、不覆盖。
4. 在顶部“模型来源”中选择本机 Ollama，或添加 OpenAI、DeepSeek、SiliconFlow、Anthropic、自定义兼容来源。
5. 直接关闭窗口即可完成聊天任务、Provider、摘要和备份调度器的收口。

桌面版使用 `tavern://app` 页面、QWebChannel 和进程内 ASGI 调用。后端核心与窗口在同一主程序内运行，不启动 Uvicorn，也不创建 HTTP 监听端口。QtWebEngine 会创建 Chromium renderer/helper 子进程；它们随主程序退出，不是独立后端服务。

## 推理来源边界

- 云端 Provider：桌面程序无需 Ollama；需要网络、API Key 和对应服务商账户。
- 本机 Ollama：剧情数据不离开本机，但 Ollama 自身仍是独立的本地模型运行时，需要安装、启动并准备模型。
- 本地酒馆不会在来源失效时静默切换模型，也不会自动重试云端生成，避免重复计费和重复写入剧情。

## 数据位置

```text
%LOCALAPPDATA%\LocalTavern\
├── data\projects\<项目>\    项目、角色、世界书与存档
├── prompts\                 用户可编辑 Prompt
├── backups\                 全量备份与恢复演练记录
├── logs\tavern-desktop.log  桌面日志
├── runtime\                 单实例锁与本地 IPC 运行文件
├── providers.json           云端来源配置，不含明文密钥
└── provider-secrets.json    当前 Windows 用户 DPAPI 加密的 API Key
```

旧目录 `C:\local-tavern\data`、`backups`、`prompts` 在首次迁移后继续保留作为原始副本。桌面数据建立后，以 `%LOCALAPPDATA%\LocalTavern` 为正式运行数据根。

## 安全与隐私

- 桌面页面采用 off-the-record Qt profile，不保存持久 Cookie。
- `tavern://app` 只提供打包内的 GET/HEAD 静态资源，阻断目录穿越、符号链接越界、外部导航和新窗口。
- 页面只允许 `/api/**` 与指定健康检查经 QWebChannel 进入进程内 ASGI；桥失败不会回退到 HTTP。
- API Key 不通过读取接口回显，Windows 上使用 DPAPI 保护。
- 云端根地址只接受公网 HTTPS，拒绝内嵌凭据、查询参数、片段、私网解析、系统代理和重定向。
- 选择云端来源意味着本轮 Prompt 上下文会发送给服务商；计费、留存、训练使用和合规条件由用户与服务商约定。

## 旧浏览器模式

`start.bat`、`run_hidden.vbs`、`stop_tavern.bat` 和 `http://127.0.0.1:8765` 只保留给开发调试与兼容排错，不是用户默认入口。开发模式仍使用 Uvicorn；桌面正式版不读取这些启动脚本，也不依赖 8765 端口。

## 开发与构建

开发环境要求 Python 3.12。浏览器开发模式使用 `requirements*.txt`；桌面构建额外使用以下精确锁：

- `requirements-desktop.txt`
- `requirements-desktop.lock.txt`
- `requirements-desktop.hashes.txt`
- PySide6 6.11.1
- PyInstaller 6.21.0

准备和构建：

```powershell
cd C:\local-tavern
setup_desktop.bat
build_desktop.bat
```

构建产物：

- 程序：`release\LocalTavern\LocalTavern.exe`
- 完整性清单：`release\release-manifest.json`
- 当前目录：3,604 个文件，589,021,235 bytes（561.73 MiB）
- EXE SHA-256：`FF4EE956A13EDFB1CDDA92C4D2BCE156DFD39CA561D2F94F5B197C2F3FD94BEA`

当前本地构建未做商业代码签名，Windows SmartScreen 可显示“未知发布者”。程序依赖 `_internal` 目录，不能只复制 EXE。

## 验证

```powershell
# 日常预检
.\.venv-dev\Scripts\python.exe tools\quality_gate.py --preflight

# 正式发布门：包含桌面重建与 EXE 无端口 smoke
.\.venv-dev\Scripts\python.exe tools\quality_gate.py --release `
  --node "C:\Program Files\nodejs\node.exe" `
  --npm "C:\Program Files\nodejs\npm.cmd" `
  --desktop-python ".\.venv-desktop\Scripts\python.exe"

# 单独验收现有 EXE
.\.venv-dev\Scripts\python.exe tools\desktop_smoke.py `
  --executable .\release\LocalTavern\LocalTavern.exe `
  --timeout 120 --hold-ms 5000
```

2026-07-28 当前发布基线：

- release 质量门 43/43，通过并输出 `release_ready:true`
- Python 702/702；Node 114/114
- branch coverage 85%
- Ruff、compileall、依赖哈希、`pip check`、固定 Chromium、axe 与浏览器 E2E 全部通过
- EXE 进程树连续三次 TCP LISTENING 采样均为空，退出后无残留进程
- 真实首次迁移中，34 个数据文件、102 个既有备份、15 个 Prompt 均逐文件哈希一致；源目录指纹未改变

## 常见问题

- 双击无反应：查看 `%LOCALAPPDATA%\LocalTavern\logs\tavern-desktop.log`；启动前异常会写 bootstrap 日志或显示脱敏错误框。
- 提示旧服务仍运行：先关闭旧浏览器版；桌面版拒绝在活动写入期间复制存档。
- 本机模型不可用：启动 Ollama，并确认已安装模型；也可以明确配置云端 Provider。
- 云端 401/403/429：检查 API Key、模型权限、额度和服务商限流策略。
- 首次启动较慢：需要复制旧数据并完成 Windows 对新程序的首次扫描；程序不会删除旧数据。
- 想移动应用：移动整个 `release\LocalTavern` 文件夹；用户数据仍位于 `%LOCALAPPDATA%\LocalTavern`。
- 想回到浏览器开发模式：使用旧启动脚本；它与桌面正式版的数据根不同，不要同时对同一数据根运行两个写入实例。

## 文档

项目唯一权威介绍与交接文档：`C:\Users\zcw\Desktop\本地酒馆-完整项目文档.md`。

功能、架构、数据、Provider、安全、迁移、构建、验收和限制发生变化时，只更新该文件，不再新建重复的桌面规划或上下文文档。
