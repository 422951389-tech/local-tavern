# LocalTavern 本地酒馆

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

在电脑上开一间自己的酒馆：一群 AI 角色同场陪你演戏——他们互相接话、抢话、各怀心思，你推门进场就是戏里的人。不是跟一个机器人一问一答，是进一间酒馆。

**English below ↓**

## 这玩意儿干嘛的

- **一台戏，不是一对一** —— 一场景里多个 AI 角色同时在场：剧情往前走、角色轮流回应、状态细节、行动建议，一条流水线全给你
- **双击就能玩** —— 下载解压双击 `LocalTavern.exe` 就完事。不用装 Python、不开浏览器、不占端口，就是个正经桌面软件
- **你的东西全在你电脑上** —— 角色卡、世界书、存档、备份全是本地文件，不经过任何服务器。你的故事就是你的
- **脑子自己挑** —— 本机跑 Ollama 也行，接 DeepSeek、SiliconFlow 这类云端 API 也行，Anthropic 原生接口也行——有钱云端没钱本地，随你
- **安全感给足** —— API Key 用 Windows 自带的 DPAPI 加密存；断网也能玩本地模型；浏览器内核不留 Cookie
- **长线剧情不断片** —— 好感度量化追踪、剧情摘要、钉选名场面、随时存档读档

## 三分钟上手

从 [Releases](../../releases) 下 `LocalTavern-win64.zip`，解压，双击 `LocalTavern.exe`。

- 整个文件夹一起拷，别只拷 EXE（它要靠旁边的 `_internal` 活）
- 第一次开会自动在 `%LOCALAPPDATA%\LocalTavern` 建数据目录
- 模型三选一：本地 Ollama / 云端 API / Anthropic——设置里填个 Key 就成

## 想自己构建的

```powershell
git clone <this-repo>
cd local-tavern
setup_desktop.bat        # 装环境（要 Python 3.12 + Node 24）
build_desktop.bat        # 产出 release\LocalTavern\LocalTavern.exe
```

开发调试走浏览器版（端口 8765）：`setup.bat` + `start.bat`

## 数据都在哪

```text
%LOCALAPPDATA%\LocalTavern\
├── data\projects\<项目>\    角色卡、世界书、存档
├── prompts\                 提示词，随便改
├── backups\                 全量备份
└── provider-secrets.json    DPAPI 加密的 API Key
```

## 技术细节

架构、安全设计、验收报告全在 [docs/PROJECT.md](docs/PROJECT.md)，硬核玩家自取。

## 许可

GPL-3.0——地图不收费，但你拿它改的图也得开源。

---

# LocalTavern (English)

Run your own tavern on your PC: a whole cast of AI characters performing with you in one shared scene — they talk over each other, scheme, and react. You're not chatting with a bot; you're walking into a tavern.

- **An ensemble, not a chatbot** — multiple AI characters per scene: story beats, character replies, status details, and suggested actions in one flow
- **Double-click and play** — no Python, no browser, no open ports. A proper Windows desktop app
- **Everything stays on your machine** — cards, worldbooks, saves, backups: local files only. Your stories are yours
- **Bring your own brain** — local Ollama, OpenAI-compatible APIs (DeepSeek, SiliconFlow...), or native Anthropic
- **Security baked in** — DPAPI-encrypted API keys, works offline with local models, no cookie trails
- **Long campaigns welcome** — affinity tracking, arc summaries, pinned moments, save/restore anytime

Grab `LocalTavern-win64.zip` from [Releases](../../releases), unzip, double-click. Keep the whole folder together — the EXE needs its `_internal` buddy.

License: GPL-3.0.
