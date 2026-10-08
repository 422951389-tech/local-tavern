# LocalTavern 本地酒馆

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-blue)](https://github.com)
[![Release](https://img.shields.io/badge/release-v1.0.1-green)](https://github.com)

**English below ↓**

LocalTavern 是一个 Windows 桌面端的多角色 AI 叙事应用。在一个场景里，多个 AI 角色同时在场、互相回应，剧情持续推进；你以固定身份参与其中。角色卡、世界书、存档全部保存在本机，模型来源可以是本机 Ollama，也可以是 DeepSeek、Anthropic 等云端 API。

## 功能

- 多角色同场：剧情推进、角色回应、状态细节与行动建议按统一结构输出
- 桌面应用：PySide6 + QtWebEngine，无需安装 Python，不占用端口，不依赖浏览器
- 数据本地存储：项目、角色卡、世界书、提示词、存档与备份均为本机文件
- 模型来源可选：本机 Ollama / OpenAI 兼容 API（DeepSeek、SiliconFlow 等）/ Anthropic Messages
- 安全设计：API Key 经 Windows DPAPI 加密存储；云端根地址强制公网 HTTPS；浏览器内核不保留 Cookie；进程内通信（QWebChannel + ASGI），无监听端口
- 长线剧情支持：结构化好感度追踪、剧情摘要、消息钉选、快照与恢复

## 安装与使用

从 [Releases](../../releases) 下载 `LocalTavern-win64.zip`，解压后运行 `LocalTavern.exe`。

注意：

- 请保留整个 `LocalTavern` 目录（EXE 依赖 `_internal`，不能单独拷贝）
- 首次启动会在 `%LOCALAPPDATA%\LocalTavern` 建立数据目录
- 在设置中选择模型来源并填入 API Key 即可开始使用

## 从源码构建

```powershell
git clone <this-repo>
cd local-tavern
setup_desktop.bat        # 准备构建环境（Python 3.12 + Node 24）
build_desktop.bat        # 产出 release\LocalTavern\LocalTavern.exe
```

开发模式（浏览器版，端口 8765）：`setup.bat` + `start.bat`

## 数据位置

```text
%LOCALAPPDATA%\LocalTavern\
├── data\projects\<项目>\    项目、角色卡、世界书与存档
├── prompts\                 用户可编辑的提示词
├── backups\                 全量备份
└── provider-secrets.json    DPAPI 加密的 API Key
```

## 文档

架构、数据格式、Provider 接入、安全模型与验收报告的完整记录见 [docs/PROJECT.md](docs/PROJECT.md)。

## 许可

GPL-3.0。

---

# LocalTavern (English)

LocalTavern is a Windows desktop application for multi-character AI storytelling. Several AI characters share one scene and respond to each other while the narrative advances; you take part with a fixed identity. Character cards, worldbooks and saves live entirely on your machine, and the model backend can be a local Ollama instance or a cloud API such as DeepSeek or Anthropic.

## Features

- Multi-character scenes: story beats, character responses, status details and suggested actions in one structured output
- Desktop app: PySide6 + QtWebEngine; no Python install, no open ports, no browser required
- Local-first storage: projects, cards, worldbooks, prompts, saves and backups are plain local files
- Pluggable backends: local Ollama, OpenAI-compatible APIs (DeepSeek, SiliconFlow, ...), or native Anthropic Messages
- Security: DPAPI-encrypted API keys, HTTPS-only cloud endpoints, no retained cookies, in-process communication (QWebChannel + ASGI) with zero listening ports
- Long campaigns: structured affinity tracking, arc summaries, pinned messages, snapshots and restore

## Getting started

Download `LocalTavern-win64.zip` from [Releases](../../releases), extract it and run `LocalTavern.exe`. Keep the whole directory together — the EXE depends on `_internal`. On first launch a data root is created at `%LOCALAPPDATA%\LocalTavern`; pick a model backend in settings and you're ready.

## License

GPL-3.0.
