# 本地酒馆搭建 — AI 上下文交接文档

> **目的**：让下一个 AI 协作者在不询问用户的情况下，能完整理解这个项目并继续工作。
> **创建日期**：2026-06-27
> **最后更新**：2026-07-22（SEARCH-1）
> **当前状态**：阶段 A、B、C、D completed；OPS-1、UX-1、SEARCH-1 completed，OPS-2 开发锁验收仍 in_progress；当前实施 REL-1
> **权威进度**：以桌面《本地酒馆搭建-AI上下文.md》和《本地酒馆-功能优化规划.md》为准

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
| Web 框架 | **FastAPI 0.115.0** | uvicorn 0.32.0，lifespan 启动 |
| LLM 通信 | **httpx 0.27.0** 异步流式 | 直接调 Ollama `/api/chat`，不用 OpenAI SDK |
| LLM 后端 | **Ollama 0.30.7** | 监听 11434 |
| 前端 | **原生 HTML/CSS/JS** | 无构建工具，无 npm，零前端依赖 |
| 数据存储 | **本地 JSON 文件** | 无数据库 |
| 角色卡/世界书格式 | **YAML** | PyYAML 6.0.1 |
| 数据校验 | **Pydantic 2.9.0** | |
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
├── server.py                  # FastAPI 主程序：lifespan、路由、安全头
├── setup.bat                  # Python 3.12 .venv + 精确 runtime lock 安装/校验
├── start.bat / run_hidden.vbs / stop_tavern.bat  # 统一安全 launcher 与精确停止
├── requirements.txt / requirements.lock.txt       # 直接 runtime 依赖 / 22 包闭包
├── requirements-dev.txt / requirements-dev.lock.txt # pytest 工具链；浏览器工具尚未完整锁定
├── README.md                  # 用户文档（启动说明）
├── AI_CONTEXT.md              # ← 本文档（AI 协作者交接）
│
├── core/                      # Python 核心模块
│   ├── ollama_client.py       # Ollama 流式客户端 + /api/show 上下文缓存
│   ├── launcher.py            # ★ OPS-1：版本校验、预绑定端口、live 浏览器门禁
│   ├── health.py              # ★ OPS-1：data/runtime/Ollama/maintenance 就绪检查
│   ├── logging_config.py      # ★ OPS-1：前台控制台 / hidden UTF-8 轮转日志
│   ├── runtime_validation.py  # ★ OPS-2：Python 3.12 与 runtime lock 精确校验
│   ├── character_loader.py    # 角色/世界书/用户档案加载（90 行）
│   ├── session_manager.py     # 存档管理 + 多时间线（260+ 行）
│   ├── prompt_assembler.py    # ★ PROMPT-1：来源单次注入、总预算、裁剪诊断
│   ├── worldbook_policy.py    # ★ WORLD-1：三态触发、稳定排序与脱敏诊断
│   ├── roleplay_policy.py     # ★ ROLE-1：发言上下文、身份解析、禁言倒计时
│   ├── search_service.py      # ★ SEARCH-1：当前权威 Session 有界搜索、脱敏与稳定快照
│   ├── token_estimator.py     # ★ PROMPT-1：可替换的保守 token 估算协议
│   ├── summary_lifecycle.py   # ★ MEMORY-1：稳定 ID、严格校验、异步生成与代际收口
│   ├── session_store.py       # revision/CAS、旧摘要稳定迁移、来源状态派生
│   ├── prompt_builder.py      # 旧调用兼容层；运行时路由不使用
│   ├── response_parser.py     # AI 回复解析（168 行）
│   └── prompt_editor.py       # 提示词读写/恢复默认（65 行）
│
├── prompts/                   # Prompt 模板（Markdown，可在线编辑）
│   ├── system.md              # 全局系统提示（格式铁律）
│   ├── group_chat.md          # 群聊轮次模板（注入上下文）
│   ├── summary.md             # 短期记忆总结模板，可在线编辑
│   └── .default/              # 默认备份；system/group_chat/summary 均有最新版本
│
├── data/projects/<项目稳定ID>/
│   ├── characters/            # 项目共享角色卡 YAML
│   ├── worldbook/             # 项目共享世界书 YAML
│   ├── user.yaml              # 项目共享用户档案
│   └── saves/                 # 独立 Session JSON + .history/ 快照
│
└── web/                       # 前端
    ├── index.html             # 主页面 + Modal 容器
    ├── style.css              # 样式（深色 + 古风暖金强调色）
    ├── app.mjs                # 前端组合入口（原生 ES module）
    └── *.mjs                  # 项目/存档/聊天/摘要/世界书/角色节奏/搜索/卡片/Prompt/渲染/listbox/帧合并等职责模块
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
   ├─ OllamaClient.get_context_limit()  # /api/show 缓存；故障时 4096 失败闭合
   ├─ PromptAssembler.assemble()        # 单次注入 + 总预算 + 安全诊断
   ├─ 预算通过后才创建 turn / 写 pending user
   └─ OllamaClient.chat_stream(num_ctx=同一预算上限)  # 防止运行时窗口漂移
        ↓
   [Ollama: NDJSON 流式 chunk]
        ├─ type=thinking → 前端 thinking panel
        └─ type=content  → 前端对话流 + 解析器
        ↓
   [done 时]
        ├─ response_parser.parse_response()  # 拆出角色卡/建议
        ├─ 更新 session（scene_meta + characters_state）
        ├─ save_session()  # 原子写
        └─ SSE 推送 {type:"parsed", parsed, revision, session_delta} 给前端
```

### 4.2 关键设计决策

| 决策 | 为什么 |
|------|--------|
| **JSON 而非 SQLite** | 用户场景简单，多会话也只是 N 个文件；JSON 可读可手改 |
| **YAML 而非 JSON 存角色卡** | 用户要手填，YAML 注释 + 可读性更好 |
| **原子写（tmp + rename）** | 断电/崩溃不会损坏存档 |
| **asyncio.Lock 全局写锁** | 防并发请求互相覆盖 |
| **Prompt 10 轮/20 条，存档 40 条** | 数量上限维持不变，给快照与重生成留原文回溯余量 |
| **总预算 = context_limit - num_predict - safety margin** | 所有可选来源逐块重算完整 messages，不能依赖 Ollama 静默截断 |
| **运行 num_ctx 与预算上限同源** | `/api/chat.options.num_ctx` 使用本轮诊断中的 context_limit |
| **thinking 模式默认开启** | 35B MoE 启用 thinking，质量更好 |
| **前端无构建** | 避免 npm 依赖，用户双击 start.bat 就跑 |
| **统一 launcher + 单 worker** | 启动前逐包校验 runtime lock、预绑定最终 socket；浏览器只认固定 live 标记，外部占用者不被结束 |
| **hidden 轮转日志** | UTF-8 5 MiB × 5，关闭 access log，不记录聊天正文；配置导入失败也有两级文件兜底 |
| **提示词模板 Markdown** | `PromptAssembler` 每轮重读；system 只放静态规则，group 中六类运行时数据槽各恰好一次 |
| **.default/ 备份目录** | 用户改坏提示词能一键恢复 |
| **摘要稳定 UUID + trim 双向绑定** | 编辑和重生成按 `summary_id` 精确定位；每段只读取自己的 `source_snapshot_id`，不回退最新快照 |
| **摘要状态与内容有效性分离** | `pending/completed/failed` 描述生成任务，`content_status` 决定正文能否进入 Prompt；失败重试不丢旧有效正文 |
| **生成代际令牌** | 后台结果只写回相同 `generation_id` 的 pending 段，人工编辑、新重试和乱序任务不会被旧结果覆盖 |
| **角色发言快照** | turn 接受时冻结 `may_speak/chattiness/remaining_silent_turns`；Prompt 与解析写回使用同一份上下文 |
| **完成回合计数** | 仅 completed 普通 chat 在唯一 Session 提交内扣减禁言；重生成、取消、失败和中断恢复均不扣减 |

**OPS-1/OPS-2 运行边界（提交 `c16ddb4`）**：官方入口只调用项目 Python 3.12 `.venv` 和 `core.launcher serve --workers 1`；启动前逐包比对 22 包 runtime lock，路径/host/port/PID/log/Ollama 配置来自 `core.config`。launcher 预绑定最终 socket，foreign listener 不打开浏览器；hidden 使用轮转日志。lifespan 启动失败与外部取消仍完整清理 PID/任务/client。pytest 476/476、Node 60/60、17 个模块语法、隔离真实子进程 smoke 与独立 P0/P1 终审通过；真实 data 31 文件 / `F20D671B…A68EDF`、backups 97 文件 / `81F28F77…D2C888`、logs 0 文件 / `E3B0C442…B855` 均未变化，95 个历史 ZIP 未删除。coverage/Ruff/Playwright、hash lock 与全新环境安装验收尚未完成。

**UX-1 边界（提交 `f0dcd87`）**：项目统计使用一次聚合 API；现代 parsed 事件只携带 revision 与场景/角色/策略/模型四字段 delta，旧日志缺 warnings 时仍可重放。旧 Session 的 scene_meta 缺失/null/字符串只在内存补六字段。流式文本按动画帧合并，历史消息单 Fragment 挂载；终态权威 reload 前保持写锁。Modal、listbox、角色卡和消息操作具备键盘、焦点恢复与 44px 触控路径。375/600/768px Edge 隔离验收无横向滚动，下拉保留 16px 边界；pytest 487/487、Node 72/72、19 个模块语法和独立 P0/P1 终审通过。axe/Playwright 未运行，仍属于 OPS-2 外部开发依赖门。

**SEARCH-1 边界（提交 `0865f69`）**：`GET /api/search` 只扫描项目当前 `saves/*.json` 的消息正文和总结结构字段，不扫描 `.history`、不建索引、不迁移数据。同一普通文件句柄执行有界稳定快照，固定 500 存档/50,000 来源/64 MiB；坏 JSON、Unicode surrogate、非普通文件和非 JavaScript 安全整数 revision 安全跳过。匹配前完成凭据脱敏，Unicode casefold 字面搜索不构造正则。前端严格白名单、纯 DOM 高亮、Abort latest request、Modal token、single-flight 和权威 Session 重读；消息/总结仅按 UUID 精确定位。pytest 499/499、Node 82/82、20 个模块语法与两轮独立 P0/P1 终审通过。真实守卫当前为 data 31 / `F20D671B…A68EDF`、backups 98 / `E09A261B…91ED7`、logs 1 / `AB0A840B…D46A`；既有备份和日志未触碰。Playwright/axe 与应用内浏览器插件未执行。

---

## 5. API 完整列表

### 模型与基础
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health/live` | 固定存活标记；200、no-store，不探测磁盘/Ollama |
| GET | `/health/ready` | data/runtime/Ollama/maintenance；全部通过 200，否则 503 |
| GET | `/api/models` | 列出 Ollama 可用模型 |
| GET | `/api/projects/stats` | 一次返回各项目角色、世界书与权威存档数量；单项目错误固定降级 |
| GET | `/api/search?project=&q=&scope=&limit=` | 搜索当前项目权威 Session；scope=all/messages/summaries/pinned，返回安全片段、稳定 ID、revision 与有界扫描计数 |
| GET | `/api/characters` | 列出所有角色卡 |
| GET | `/api/user` | 获取用户档案 |

### 提示词编辑
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/prompts` | 读全部 3 个 prompt |
| PUT | `/api/prompts/{name}` | 保存 prompt（name ∈ system/group_chat/summary） |
| POST | `/api/prompts/{name}/reset` | 恢复默认 |

### 会话管理（多时间线）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sessions` | 列出所有会话（按更新时间倒序） |
| POST | `/api/sessions` | 创建新会话（body: `{name}`） |
| POST | `/api/sessions/rename` | 重命名（body: `{session_id, new_name}`） |
| POST | `/api/sessions/delete` | 删除（body: `{session_id}`，至少保留 1 个） |
| GET | `/api/sessions/export?project=...&save=...` | 导出会话 JSON |
| POST | `/api/sessions/import` | 导入（body: `{json_str, name?}`） |

### 单个会话
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/session?project=...&save=...` | 读会话（含摘要来源与进程内生成状态） |
| POST | `/api/session/reset` | 重置当前会话（清空历史、保留会话） |
| PATCH | `/api/session?project=...&save=...` | 按稳定 `message_id` 执行消息编辑、删除、钉选与 Prompt 开关 |
| GET | `/api/session/history?project=...&save=...` | 列出历史快照 |
| POST | `/api/session/restore` | 从普通/reset 快照恢复（trim 仅供摘要原文查看） |
| PATCH | `/api/session/summary` | 按稳定 `summary_id` 严格编辑任意摘要段 |
| POST | `/api/session/summary/regenerate` | 按绑定 trim 快照异步重生成，返回 202 |
| PATCH | `/api/session/characters/{character_id}/silence` | 按 revision CAS 保存角色剩余禁言成功轮次（0–999） |
| PATCH | `/api/session/roleplay-policy` | 按 revision CAS 保存禁言角色严格写回开关 |

### 模型切换与聊天
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/model/switch` | 切换当前模型（body: `{model}`） |
| POST | `/api/chat/turns` | 接受持久回合并返回 `turn_id`（202） |
| GET | `/api/chat/turns/{turn_id}/events` | 可断线续接的 SSE 事件流 |
| POST | `/api/chat/turns/{turn_id}/cancel` | 幂等取消持久回合 |
| POST | `/api/chat` | 兼容旧调用的同步 SSE 入口 |

---

## 6. 关键文件速查（下一个 AI 该先读哪几个）

按重要性排：

1. **`prompts/system.md`** —— AI 行为铁律，改格式/规则改这里
2. **`core/prompt_assembler.py` + `core/token_estimator.py`** —— Prompt 总预算、裁剪顺序、诊断与估算规则
3. **`core/response_parser.py`** —— 解析 AI 输出的正则，加字段改这里
4. **`routes/chat.py`、`core/chat_turns.py` 与 `core/roleplay_policy.py`** —— 持久回合、角色发言快照、解析写回与终态计数
5. **`core/summary_lifecycle.py` 与 `routes/messages.py`** —— 摘要严格校验、来源绑定、失败重试与并发收口
6. **`web/app.mjs`、`web/search.mjs`、`web/summary-panel.mjs`、`web/roleplay.mjs` 与 `web/*.mjs`** —— 前端组合、搜索定位、摘要、角色节奏与持久 SSE 回合处理
7. **`core/launcher.py`、`core/health.py`、`core/runtime_validation.py`** —— 安全启动、health 与精确依赖校验
8. **`core/character_loader.py` 中的 `CHARACTER_SCHEMA` + 各项目受跟踪 `_template.yaml`** —— 角色卡字段定义

---

## 7. 角色卡字段（YAML Schema）

```yaml
id: char_id              # 唯一标识（文件名用这个）
name: 角色名              # 显示名
aliases: []               # 身份解析别名；仅唯一命中才允许写回
chattiness: 50            # 0–100 提示权重，不是发言人数配额
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
2. **0 到 min(3, 当前活跃角色数) 个真实出场角色状态卡**（🎭 开头）；无活跃角色时严禁创建角色
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
| **提示词改坏或重复数据槽** | 发送前返回 `prompt_template_invalid` 422 | 六类运行时数据槽必须各恰好一次；`.default/` 最新备份可恢复 |
| **Prompt 超预算** | 发送前返回 `prompt_budget_exceeded` 422 | 诊断只含来源、稳定 ID、估算量、保留/裁剪原因，不含正文；不会创建 turn、快照或 pending user |
| **摘要一直显示生成中** | 服务重启后磁盘仍是 pending，但进程内任务不存在 | GET 响应的 `generation_active=false` 会显示“生成已中断”；用户可按原 `summary_id` 与原快照重试 |
| **摘要原文快照缺失/错属** | 重生成返回稳定 `summary_source_*` 错误 | 不扫描、不回退最新 trim，也不会调用模型；前端仍保留现有有效正文 |
| **GBK 编码报错** | PowerShell/curl 处理中文 | 用 UTF-8 模式 (`python -X utf8`)；测试用 PowerShell `Invoke-WebRequest` |

---

## 10. 待办 / 未实现

完整剩余项与验收矩阵只维护在桌面《本地酒馆-功能优化规划.md》。OPS-1、UX-1、SEARCH-1 已完成；当前实施 REL-1。OPS-2 的 coverage/Ruff/Playwright、hash lock 与全新环境安装在取得外部下载确认后收口。

---

## 11. 用户工作流（提醒下一个 AI）

用户的使用流程：

1. **首次使用**：
   - 双击 `setup.bat` 创建并校验 Python 3.12 项目 `.venv`
   - 启动 Ollama，双击 `start.bat`；浏览器只在 `/health/live` 返回本项目标记后打开
   - 在界面创建项目、角色、用户档案、世界书与存档
   - 点「重置」初始化场景
   - 在底部输入框打字开玩

2. **日常使用**：
   - 顶部下拉切换会话（多时间线）
   - 顶部搜索按钮可按消息/总结/钉选范围搜索当前项目全部权威存档，并精确跳转到消息或总结段
   - 消息 hover 可删/编辑/重生成
   - 顶部 ⚙ 提示词可编辑 system/group_chat/summary；摘要面板可按段查看原文、编辑、重生成或重试
   - 世界书页可编辑项目共享条目，并为当前存档单独选择 manual 条目；命中诊断不显示正文
   - 右侧「角色状态与禁言」可保存当前存档的禁言倒计时和严格写回策略；角色卡编辑器保存项目共享 chattiness
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

## 13. 关键文件索引

| 范围 | 文件 |
|------|------|
| 启动/运行 | `core/launcher.py`、`core/runtime_validation.py`、`core/logging_config.py`、`core/health.py`、`core/process_guard.py` |
| 数据事务 | `core/session_store.py`、`core/recovery_store.py`、`core/backup_store.py`、`core/library_lock.py` |
| 聊天/Prompt | `core/chat_turns.py`、`core/prompt_assembler.py`、`core/token_estimator.py`、`core/ollama_client.py` |
| 记忆/世界/角色/搜索 | `core/summary_lifecycle.py`、`core/worldbook_policy.py`、`core/roleplay_policy.py`、`core/search_service.py`、`routes/search.py` |
| 前端 | `web/app.mjs`、`web/api-client.js`、`web/session-ref.js`、`web/turn-client.js`、`web/*.mjs` |
| 安装锁 | `.python-version`、`requirements.lock.txt`、`requirements-dev.lock.txt`、`setup.bat` |

---

## 14. 下次接手时建议先做的事

1. **先读桌面优化规划的 REL-1 段** — 当前任务是结构化关系边、证据生命周期与可访问图谱
2. **读 `core/session_store.py`、`core/session_manager.py`、`routes/messages.py`、`core/destructive_service.py`** — 关系边需接入 trim/delete/truncate/regenerate/角色删除事务
3. **读 `web/search.mjs` 与 `web/app.mjs`** — 证据选择和定位复用 SEARCH-1 的安全组件
4. **检查启动链**：首次运行 `setup.bat`，随后使用 `start.bat` 或 `run_hidden.vbs`；不要绕过 launcher 直接调用 uvicorn
5. **确认 Ollama 在跑**（`ollama ps`）
6. **如果用户说"X 不工作"**，先 curl 测 API → 看 server 日志 → 看前端 console

---

**最后更新**：2026-07-18  UX-1 响应式、可访问性、前端性能与轻量 parsed 事件
**作者**：用户通过 AI 协作者完成
**许可**：用户私有项目
