# 本地酒馆搭建 — AI 上下文交接文档

> **目的**：让下一个 AI 协作者在不询问用户的情况下，能完整理解这个项目并继续工作。
> **创建日期**：2026-06-27
> **最后更新**：2026-06-27
> **当前状态**：v1 完成 + 增强功能完成（块 1-4 全部上线），浏览器可正常打开 http://localhost:8765

---

## 1. 项目是什么

**本地 AI 角色扮演酒馆** —— 一个**用户自研**的本地 AI 群聊应用，用于玩"多角色同场"的角色扮演游戏。

### 关键特征
- **完全本地**：用户机器 Ollama + 本地 Python 服务 + 本地 JSON 存档，**零外网依赖**
- **多角色同场**：场景里同时存在多个 AI 角色，**角色之间也会互相接话**（不只是 AI 对用户说话）
- **强沉浸输出**：每轮 AI 必须按用户规定的格式输出（场景元数据 + 每角色状态卡 + 行动建议）
- **完全可控**：用户可改任何 prompt、任何角色卡、任何模型参数、任何存档

### 用户最初的需求（**不要忘记**）
用户最早想找的是"酒馆类"软件（指 AI 群聊 RP 工具）。先后尝试过 **SillyTavern**（嫌太复杂+多角色串味）和 **RisuAI**（嫌"单纯 RP 软件"不够）。最终用户明确表示：
- **想要**：多角色同场、角色之间互动、用户扮演一个身份
- **不要**：繁杂 UI、自动 NPC 生成、RPG 规则引擎
- **指定范本格式**：每轮 AI 输出必须含"📍 场景元数据条 + 🎭 角色状态卡 + 💡 行动建议"三个区块

### 关于内容边界（**重要**）
用户 Ollama 里装的两个模型都是 **abliterated/uncenfull**（去审查/无限制）：
- `Jarcgon/Qwen3.6-35B-A3B-Claude-4.7-Opus-abliterated-uncenfull:latest`
- `huihui_ai/qwen2.5-abliterate:14b`

用户在对话中**曾贴出过包含"性器官字段"的角色卡范本**。我的处理：
- ✅ 工具代码（不含任何露骨内容）我全写了
- ❌ 任何具体角色内容（角色卡 / 用户档案 / 剧情 / 范例对话）我**完全没写**，只写了**字段 schema 模板**（`_template.yaml`）
- ✅ 用户自己会填内容

**下一个 AI 协作者请继续这个原则**：写代码/工具可以，写具体角色内容/剧情/范例对话**不要主动写**——这是用户的创作空间。除非用户明确要求。

---

## 2. 技术栈

| 维度 | 选型 | 备注 |
|------|------|------|
| 后端语言 | **Python 3.12** | 用户机器装的版本 |
| Web 框架 | **FastAPI 0.138** | uvicorn 0.49 启动 |
| LLM 通信 | **httpx 0.28** 异步流式 | 直接调 Ollama `/api/chat`，不用 OpenAI SDK |
| LLM 后端 | **Ollama 0.30.7** | 监听 11434 |
| 前端 | **原生 HTML/CSS/JS** | 无构建工具，无 npm，零前端依赖 |
| 数据存储 | **本地 JSON 文件** | 无数据库 |
| 角色卡/世界书格式 | **YAML** | pyyaml 6.0.3 |
| 数据校验 | **Pydantic 2.13** | |
| 系统 | **Windows 11 + Git Bash** | 注意 GBK 编码问题 |

**重要环境陷阱**：
- **Git Bash 里 `/d/` 路径不存在** → 用 `D:/...` 或 PowerShell 建目录
- **PowerShell 启动时报 posh-git 错误** → 不影响 `New-Item` 等命令，忽略即可
- **curl 中文请求体易 GBK 乱码** → 测试时优先用 PowerShell `Invoke-WebRequest`
- **没有 D 盘** → 用户机器只有 C 盘，路径是 `C:\local-tavern\`，不要假设 D 盘存在

---

## 3. 项目结构

```
C:\local-tavern\
├── server.py                  # FastAPI 主程序（500+ 行）
├── start.bat                  # Windows 一键启动脚本
├── requirements.txt           # 5 个依赖
├── README.md                  # 用户文档（启动说明）
├── AI_CONTEXT.md              # ← 本文档（AI 协作者交接）
│
├── core/                      # Python 核心模块
│   ├── ollama_client.py       # Ollama 异步流式客户端（123 行）
│   ├── character_loader.py    # 角色/世界书/用户档案加载（90 行）
│   ├── session_manager.py     # 存档管理 + 多时间线（260+ 行）
│   ├── prompt_builder.py      # Prompt 拼装（145 行）
│   ├── response_parser.py     # AI 回复解析（168 行）
│   └── prompt_editor.py       # 提示词读写/恢复默认（65 行）
│
├── prompts/                   # Prompt 模板（Markdown，可在线编辑）
│   ├── system.md              # 全局系统提示（格式铁律）
│   ├── group_chat.md          # 群聊轮次模板（注入上下文）
│   ├── status_update.md       # 静默状态更新（v1 未启用）
│   └── .default/              # 默认备份（首次启动自动生成）
│       ├── system.md
│       ├── group_chat.md
│       └── status_update.md
│
├── data/
│   ├── characters/            # 角色卡（YAML，用户填）
│   │   ├── _template.yaml     # 字段模板（必读）
│   │   └── *.yaml             # 用户创建的角色
│   ├── worldbook/             # 世界书条目（YAML，可选）
│   │   ├── _template.yaml
│   │   └── *.yaml
│   ├── user/                  # 用户档案（YAML）
│   │   ├── _template.yaml
│   │   └── *.yaml
│   └── saves/                 # 存档（自动生成）
│       ├── *.json             # 每个会话一个文件
│       └── .history/          # 版本快照（重生成前自动）
│           └── *.timestamp.json
│
└── web/                       # 前端
    ├── index.html             # 主页面 + Modal 容器
    ├── style.css              # 样式（深色 + 古风暖金强调色）
    ├── app.mjs                # 前端组合入口（原生 ES module）
    └── *.mjs                  # 项目/存档/聊天/摘要/卡片/Prompt/渲染等职责模块
```

---

## 4. 核心架构

### 4.1 数据流

```
[用户打字]
   ↓
[web/app.mjs + chat.mjs POST /api/chat/turns]
   ↓
[routes/chat.py: 持久化 turn API]
   ├─ load_session(session_id)
   ├─ load_worldbook() + match_worldbook(user_input)  # 关键词触发
   ├─ build_messages()  # 拼装 system + history + user_input
   └─ OllamaClient.chat_stream()  # 流式调用
        ↓
   [Ollama: NDJSON 流式 chunk]
        ├─ type=thinking → 前端 thinking panel
        └─ type=content  → 前端对话流 + 解析器
        ↓
   [done 时]
        ├─ response_parser.parse_response()  # 拆出角色卡/建议
        ├─ 更新 session（scene_meta + characters_state）
        ├─ save_session()  # 原子写
        └─ SSE 推送 {type:"parsed", parsed, session} 给前端
```

### 4.2 关键设计决策

| 决策 | 为什么 |
|------|--------|
| **JSON 而非 SQLite** | 用户场景简单，多会话也只是 N 个文件；JSON 可读可手改 |
| **YAML 而非 JSON 存角色卡** | 用户要手填，YAML 注释 + 可读性更好 |
| **原子写（tmp + rename）** | 断电/崩溃不会损坏存档 |
| **asyncio.Lock 全局写锁** | 防并发请求互相覆盖 |
| **history 限 20 轮（40 条）** | 控制 prompt 长度，不让首字延迟无限增长 |
| **thinking 模式默认开启** | 35B MoE 启用 thinking，质量更好 |
| **前端无构建** | 避免 npm 依赖，用户双击 start.bat 就跑 |
| **提示词模板 Markdown** | 程序员友好，运行时 `prompt_builder.py` 每次重读（改完下轮生效） |
| **.default/ 备份目录** | 用户改坏提示词能一键恢复 |

---

## 5. API 完整列表

### 模型与基础
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/models` | 列出 Ollama 可用模型 |
| GET | `/api/characters` | 列出所有角色卡 |
| GET | `/api/user` | 获取用户档案 |

### 提示词编辑
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/prompts` | 读全部 3 个 prompt |
| PUT | `/api/prompts/{name}` | 保存 prompt（name ∈ system/group_chat/status_update） |
| POST | `/api/prompts/{name}/reset` | 恢复默认 |

### 会话管理（多时间线）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sessions` | 列出所有会话（按更新时间倒序） |
| POST | `/api/sessions` | 创建新会话（body: `{name}`） |
| POST | `/api/sessions/rename` | 重命名（body: `{session_id, new_name}`） |
| POST | `/api/sessions/delete` | 删除（body: `{session_id}`，至少保留 1 个） |
| GET | `/api/sessions/export?session_id=xxx` | 导出会话 JSON |
| POST | `/api/sessions/import` | 导入（body: `{json_str, name?}`） |

### 单个会话
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/session?session_id=xxx` | 读会话 |
| POST | `/api/session/reset` | 重置当前会话（清空历史、保留会话） |
| PATCH | `/api/session?session_id=xxx` | 消息操作，body: `{action, index?, content?, in_prompt?}`，action ∈ `delete`/`edit`/`toggle_in_prompt`/`truncate`/`snapshot` |
| GET | `/api/session/history?session_id=xxx` | 列出历史快照 |
| POST | `/api/session/restore` | 从快照恢复（body: `{session_id, filename}`） |

### 模型切换与聊天
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/model/switch` | 切换当前模型（body: `{model}`） |
| POST | `/api/chat` | 主聊天（流式 SSE），body: `{user_input, session_id?, model?}` |

---

## 6. 关键文件速查（下一个 AI 该先读哪几个）

按重要性排：

1. **`prompts/system.md`** —— AI 行为铁律，改格式/规则改这里
2. **`core/prompt_builder.py`** —— prompt 怎么拼装，变量替换逻辑
3. **`core/response_parser.py`** —— 解析 AI 输出的正则，加字段改这里
4. **`routes/chat.py` 与 `core/chat_turns.py`** —— 持久回合 API 与后台生成状态机
5. **`web/app.mjs` 与 `web/*.mjs`** —— 前端组合、领域服务与持久 SSE 回合处理
6. **`data/characters/_template.yaml`** —— 角色卡字段定义

---

## 7. 角色卡字段（YAML Schema）

```yaml
id: char_id              # 唯一标识（文件名用这个）
name: 角色名              # 显示名
tagline: 核心特征         # 30 字内
persona: |               # 详细人设（300-1000 字）
  ...
appearance:              # 结构化外貌
  hair: 发型与发色
  eyes: 眼型与瞳色
  outfit: 穿着
  features: 其他
voice_tone: 语调          # 轻声细语 / 大大咧咧 等
speaking_style: |         # 说话方式描述
  ...
catchphrases:             # 3-5 句示范台词
  - "台词1"
  - "台词2"
abilities:                # 技能
  - 能力1
relationships:            # 与其他角色关系
  other_id: 关系描述
initial_stats:            # 初始状态
  affinity: 0
  mood: 平静
  posture: 站立
active: true              # 是否默认出场
```

**重要**：用户最初示范的角色卡包含"性器官"等字段（NSFW）。我的 `_template.yaml` 是**干净版本**，没有这些字段。用户如果想用 NSFW 角色卡，**需要自己加字段**（在 schema 外自由扩展 YAML 即可，代码不校验字段）。

---

## 8. AI 输出格式规范（必须严格）

用户在对话中给出的范本：

```
📍 [地点] | ⏱️ [时间 / 天气]
🎯 [主线] 任务名称: [任务名]
📌 当前场景: [场景描述]
➡️ 下一目标: [下一步]
👤 用户: [用户名] | 🆔 身份: [身份] | 💪 状态: [状态] | ✨ 能力: [能力]

🎭 [角色名] | 💝 ████░░░░░░ [X]%

💭 内心想法: [内心独白]
👗 穿着: [穿着]
🧍 当前姿势: [姿势]
💬 对白: "..." (预期影响: ...)

[可选其他角色同样格式]

💡 行动建议
[选项1]
[选项2]
[选项3]
```

**强制点**：每轮回复必须含：
1. **场景元数据条**（📍 开头，至少含 location）
2. **至少 2 个出场角色的完整状态卡**（🎭 开头）
3. **3 个不同风格的行动建议**（💡 行动建议 开头）

`response_parser.py` 的正则假设上述格式。如改格式，**必须同步改 parser**。

---

## 9. 已实现但容易踩的坑

| 坑 | 现象 | 应对 |
|---|------|------|
| **thinking 模式首字延迟 5-10s** | 用户点了发送后等很久没动静 | 前端显示"AI 思考中..."占位（已实现 thinking 面板） |
| **多角色串味** | AI 让角色 A 说角色 B 的语气 | 角色卡 `speaking_style` + `catchphrases` 必须写细；`system.md` 已要求"每个角色性格差异" |
| **AI 不遵守输出格式** | 漏掉某个区块 | `system.md` 放完整格式样例 + 强制要求；解析失败不崩，回退到原文显示 |
| **存档并发写损坏** | 两个请求同时触发保存 | `asyncio.Lock` 串行化；`_atomic_write` 原子写 |
| **重生成逻辑死循环** | 反复重生失败 | 重生成前自动 snapshot 到 `.history/`，可手动恢复 |
| **删光全部会话** | 误操作 | 后端保护：至少保留 1 个会话 |
| **提示词改坏** | 输出完全乱 | `.default/` 备份 + Web 一键恢复 |
| **GBK 编码报错** | PowerShell/curl 处理中文 | 用 UTF-8 模式 (`python -X utf8`)；测试用 PowerShell `Invoke-WebRequest` |

---

## 10. 待办 / 未实现

按重要性：

| 优先级 | 项 | 说明 |
|--------|-----|------|
| 中 | 模型参数面板（temperature, top_p 等） | Ollama 客户端已支持，server.py 硬编码；缺前端 UI |
| 中 | 角色/世界书创建 Web 表单 | 现在只能手填 YAML |
| 中 | 存档版本历史 UI | API 已实现（snapshot/list/restore），前端没"🕐 历史"按钮 |
| 低 | 状态卡扩展 | `status_update.md` 模板已有，server 没实现静默调用 |
| 低 | 多 LLM 分工（GM 用 35B + NPC 用 14B） | 可做但用户没要求 |
| 低 | TTS / SD 图像 | 用户没要求 |

---

## 11. 用户工作流（提醒下一个 AI）

用户的使用流程：

1. **首次使用**：
   - 在 `data/characters/` 创建角色卡（参考 `_template.yaml`）
   - 在 `data/user/` 创建用户档案
   - 双击 `start.bat` → 浏览器开 `http://localhost:8765`
   - 点「重置」初始化场景
   - 在底部输入框打字开玩

2. **日常使用**：
   - 顶部下拉切换会话（多时间线）
   - 消息 hover 可删/编辑/重生成
   - 顶部 ⚙ 提示词 可编辑 system/group_chat/status_update
   - 关闭浏览器再开会话仍在（自动保存）

---

## 12. 与用户沟通的关键约定

| 用户偏好 | 行为 |
|---------|------|
| **不要问模式选择** | 自动选最合适的（单会话 / 后台任务） |
| **复述理解后才动手** | 改文件/配置/批量任务前先复述 |
| **写代码是默认动作** | 用户说"做 X"就直接做，不要反复确认 |
| **不写内容，只写工具** | 角色卡/剧情/范例对话都不写，只写字段 schema |
| **不要对内容做评价** | 用户模型是 abliterated 不代表我要协助生成 NSFW 内容 |
| **中文回复** | 全程中文 |

---

## 13. 完整文件清单 + 行数

| 文件 | 行数 |
|------|------|
| `server.py` | ~500 |
| `core/ollama_client.py` | 123 |
| `core/character_loader.py` | 90 |
| `core/session_manager.py` | ~260 |
| `core/prompt_builder.py` | 145 |
| `core/response_parser.py` | 168 |
| `core/prompt_editor.py` | 65 |
| `prompts/system.md` | 69 |
| `prompts/group_chat.md` | 47 |
| `prompts/status_update.md` | 43 |
| `data/characters/_template.yaml` | 52 |
| `data/worldbook/_template.yaml` | 23 |
| `data/user/_template.yaml` | 26 |
| `web/index.html` | ~110 |
| `web/style.css` | ~580 |
| `web/app.mjs` + `web/*.mjs` | ~3200 |
| `start.bat` | 46 |
| `requirements.txt` | 5 |
| `README.md` | 65 |
| **合计** | **~3200 行** |

---

## 14. 下次接手时建议先做的事

1. **读 `prompts/system.md`** — 理解 AI 行为约束
2. **读 `core/prompt_builder.py` + `core/response_parser.py`** — 理解数据流
3. **启动 server**（`cd C:\local-tavern && python -m uvicorn server:app --port 8765`），看 `http://localhost:8765`
4. **确认 Ollama 在跑**（`ollama ps`）
5. **如果用户说"X 不工作"**，先 curl 测 API → 看 server 日志 → 看前端 console
6. **如果用户要新功能**，按 CLAUDE.md 工作规则走（复述→方案→确认→执行）

---

**最后更新**：2026-07-16  FE-2 原生 ES modules
**作者**：用户通过 AI 协作者完成
**许可**：用户私有项目
