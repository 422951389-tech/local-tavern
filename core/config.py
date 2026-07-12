"""跨模块共享的窗口、上限与路径配置单一事实源。

所有"历史窗口"相关数值集中在此，消除 trim 与 prompt_builder 各自硬编码、
语义靠注释硬撑导致的隐性裂缝。

语义约定：
- MAX_TURNS_IN_PROMPT : 喂给模型的对话轮数；messages 序列注入条数 = 该值 * 2（每轮 user+assistant）。
- MAX_MESSAGES_IN_SAVE : 存档保留的非 pinned 消息条数；≥ prompt 窗口，差额给快照/导出/重生成留原文回溯余量。
- HARD_LIMIT          : 单存档 message_history 异常增长（批量导入等）的强制 trim 硬上限。
"""
import os
from pathlib import Path

MAX_TURNS_IN_PROMPT = 10
MAX_MESSAGES_IN_SAVE = 40
HARD_LIMIT = 200

def _configured_path(env_name: str, default: Path) -> Path:
    """读取可选绝对路径配置；未配置时保留本地酒馆的既有目录布局。"""
    raw = os.environ.get(env_name)
    if raw is None:
        return default.resolve(strict=False)
    if not raw.strip():
        raise ValueError(f"{env_name} 不能为空")
    return Path(raw).expanduser().resolve(strict=False)


# 项目根目录与关键子路径。
# 测试进程在导入应用前注入 TAVERN_* 路径，生产环境不设变量时行为不变。
BASE_DIR = _configured_path("TAVERN_BASE_DIR", Path("C:/local-tavern"))
DATA_DIR = _configured_path("TAVERN_DATA_DIR", BASE_DIR / "data")
SETTINGS_PATH = _configured_path("TAVERN_SETTINGS_PATH", DATA_DIR / "settings.json")
PROJECTS_DIR = _configured_path("TAVERN_PROJECTS_DIR", DATA_DIR / "projects")
WEB_DIR = _configured_path("TAVERN_WEB_DIR", BASE_DIR / "web")
PROMPTS_DIR = _configured_path("TAVERN_PROMPTS_DIR", BASE_DIR / "prompts")

OLLAMA_HOST = os.environ.get("TAVERN_OLLAMA_HOST", "http://localhost:11434").strip().rstrip("/")
if not OLLAMA_HOST:
    raise ValueError("TAVERN_OLLAMA_HOST 不能为空")

# 默认存档名（单一事实源）
DEFAULT_SAVE = "默认存档"
