# LocalTavern 本地酒馆

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-blue)](https://github.com)
[![Release](https://img.shields.io/badge/release-v1.0.1-green)](https://github.com)

**English below ↓**

Windows 桌面端 AI 多角色叙事工作台。多个 AI 角色同场扮演、互相接话，你以明确身份入场——不是和单个机器人聊天，是进一间酒馆。

## ✨ 特性

- 🎭 **多角色同场** —— 一场景多 AI 角色，剧情推进/角色回应/状态细节/行动建议统一结构输出
- 🖥️ **真桌面应用** —— PySide6 + QtWebEngine，双击即用，无需 Python 环境、不占端口、不开浏览器
- 📖 **世界书/角色卡/存档全本地** —— 项目、角色、世界书、Prompt、备份全部在本机文件，你的故事是你的
- 🧠 **推理来源自选** —— 本机 Ollama / OpenAI 兼容 API（DeepSeek、SiliconFlow…）/ Anthropic Messages
- 🔒 **安全设计** —— API Key 用 Windows DPAPI 加密存储；云端根地址强制公网 HTTPS；off-the-record 浏览器 profile 不留 Cookie；无端口进程内通信（QWebChannel + ASGI）
- 📊 **结构化好感度** —— 角色关系量化追踪，长线剧情摘要/钉选/快照/恢复
- 🎨 **实时视觉验收** —— 55 项质量门、固定 Chromium 九宽度视觉回归、axe 无障碍检查

## 🚀 快速开始

从 [Releases](../../releases) 下载 `LocalTavern-win64.zip`，解压后双击 `LocalTavern.exe`。

- 保留整个 `LocalTavern` 目录（EXE 依赖 `_internal`，不能单拷）
- 首次启动会在 `%LOCALAPPDATA%\LocalTavern` 建立数据根
- 模型来源三选一：本机 Ollama / OpenAI 兼容云端 / Anthropic

## 🔧 从源码构建

```powershell
git clone <this-repo>
cd local-tavern
setup_desktop.bat        # 准备构建环境 (Python 3.12 + Node 24)
build_desktop.bat        # 产出 release\LocalTavern\LocalTavern.exe
```

开发模式（浏览器版，端口 8765）：`setup.bat` + `start.bat`

## 📁 数据位置

```text
%LOCALAPPDATA%\LocalTavern\
├── data\projects\<项目>\    项目、角色、世界书与存档
├── prompts\                 用户可编辑 Prompt
├── backups\                 全量备份
└── provider-secrets.json    DPAPI 加密的 API Key
```

## ⚖️ 许可

GPL-3.0。地图不收费，但按本地图改的图也得开源。

---

# LocalTavern (English)

A Windows desktop AI multi-character narrative workbench. Multiple AI characters share one scene and react to each other — you join with a fixed identity instead of chatting with a lone bot. Think: walking into a tavern.

## Features

- 🎭 **Multi-character scenes** — unified structured output: story beat, character responses, foldable status details, suggested actions
- 🖥️ **Real desktop app** — PySide6 + QtWebEngine shell; no Python install, no TCP port, no browser tab. Double-click and play
- 📖 **Everything local** — projects, character cards, worldbooks, prompts, saves and backups live in local files
- 🧠 **Bring your own brain** — local Ollama, OpenAI-compatible APIs (DeepSeek, SiliconFlow, ...), or native Anthropic Messages
- 🔒 **Security-first** — DPAPI-encrypted API keys, HTTPS-only cloud roots, off-the-record web profile, in-process QWebChannel+ASGI (zero listening ports)
- 📊 **Structured affinity tracking**, long-arc summaries, pinned messages, snapshots & restore
- 🎨 **Verified releases** — 55-check quality gate, fixed-Chromium 9-width visual regression, axe a11y checks

## Build from source

```powershell
setup_desktop.bat
build_desktop.bat
# -> release\LocalTavern\LocalTavern.exe
```

Python 3.12 + Node 24 required for the full quality gate.

## License

GPL-3.0
