# 本地酒馆

本地自研的 AI 多角色扮演酒馆，接入你的本地 Ollama。

## 启动

双击 `start.bat`，浏览器打开 http://localhost:8765

## 首次使用

1. 在 `data/characters/` 创建角色卡（参考 `_template.yaml`）
2. 在 `data/user/` 创建用户档案
3. （可选）在 `data/worldbook/` 添加世界书条目
4. 浏览器打开后，点右上角「重置」初始化场景
5. 在底部输入框打字开始

## 功能

- 多角色同场，角色之间也会互相接话
- 每个角色维护好感度/内心/姿势/穿着，跨轮保留
- 顶栏可切换模型（35B 主力 / 14B 备用）
- 顶栏可切换/新建/重命名/删除会话（多时间线）
- 顶栏 ⚙ 提示词 可在线编辑 3 个 prompt 文件
- 消息 hover 可删除 / 编辑 / 重新生成
- 存档自动保存（原子写，断电不损坏）

## 文件结构

```
C:\local-tavern\
├── server.py                  主程序
├── start.bat                  启动脚本
├── requirements.txt           依赖
├── README.md                  本文档
├── AI_CONTEXT.md              AI 协作者交接文档（在桌面也有副本）
├── core/                      Python 模块
├── prompts/                   Prompt 模板（可在线编辑）
├── data/                      数据（角色/世界书/用户档案/存档）
└── web/                       前端
```

## 路径注意

- **项目位置**：`C:\local-tavern\`（不是 D 盘，你机器只有 C 盘）
- **Ollama 数据**：`C:\Users\zcw\.ollama\`（与本项目分开，不冲突）
- **存档位置**：`C:\local-tavern\data\saves\`

## 模型

用户 Ollama 中已装：
- `Jarcgon/Qwen3.6-35B-A3B-Claude-4.7-Opus-abliterated-uncenfull:latest`（35B MoE，推荐）
- `huihui_ai/qwen2.5-abliterate:14b`（轻量备选）

## 依赖

```bash
pip install -r requirements.txt
```

需要：fastapi, uvicorn, httpx, pyyaml, pydantic

## 文档

- 给 AI 协作者看的项目上下文：`桌面\本地酒馆搭建-AI上下文.md`
- 项目内 `AI_CONTEXT.md` 是同一份的副本

## 常见问题

**端口被占用** → 编辑 `server.py` 改端口
**Ollama 没启动** → 启动 Ollama，它监听 11434
**角色串味** → 角色卡里 `speaking_style` 和 `catchphrases` 写细
**AI 不遵守输出格式** → 顶栏 ⚙ 提示词 里改 `system.md`，加完整范本示例