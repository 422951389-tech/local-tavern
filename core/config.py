"""跨模块共享的窗口/上限单一事实源。

所有"历史窗口"相关数值集中在此，消除 trim 与 prompt_builder 各自硬编码、
语义靠注释硬撑导致的隐性裂缝。

语义约定：
- MAX_TURNS_IN_PROMPT : 喂给模型的对话轮数；messages 序列注入条数 = 该值 * 2（每轮 user+assistant）。
- MAX_MESSAGES_IN_SAVE : 存档保留的非 pinned 消息条数；≥ prompt 窗口，差额给快照/导出/重生成留原文回溯余量。
- HARD_LIMIT          : 单存档 message_history 异常增长（批量导入等）的强制 trim 硬上限。
"""
from pathlib import Path

MAX_TURNS_IN_PROMPT = 10
MAX_MESSAGES_IN_SAVE = 40
HARD_LIMIT = 200

# 项目根目录与关键子路径（单一事实源，避免各模块硬编码 C:/local-tavern）
BASE_DIR = Path("C:/local-tavern")
DATA_DIR = BASE_DIR / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
PROJECTS_DIR = DATA_DIR / "projects"
WEB_DIR = BASE_DIR / "web"
PROMPTS_DIR = BASE_DIR / "prompts"

# 默认存档名（单一事实源）
DEFAULT_SAVE = "默认存档"