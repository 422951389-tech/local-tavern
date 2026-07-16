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


def _configured_int(
    env_name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """读取受边界约束的整数配置，拒绝布尔/浮点和越界文本。"""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    text = raw.strip()
    if not text or not text.isdecimal():
        raise ValueError(f"{env_name} 必须是非负整数")
    value = int(text)
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" 到 {maximum}" if maximum is not None else " 以上"
        raise ValueError(f"{env_name} 必须在 {minimum}{upper}")
    return value


def _configured_bool(env_name: str, default: bool) -> bool:
    """读取显式布尔环境变量，拒绝含糊文本。"""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{env_name} 必须是 true/false 或 1/0")


# Prompt 总预算。模型上限优先由 Ollama /api/show 获取；获取失败时使用保守回退。
# 输出预算与安全余量始终先从上下文上限中扣除，不能依赖 Ollama 静默截断输入。
PROMPT_CONTEXT_FALLBACK = _configured_int(
    "TAVERN_PROMPT_CONTEXT_FALLBACK",
    4096,
    minimum=4096,
    maximum=1_048_576,
)
PROMPT_SAFETY_MARGIN = _configured_int(
    "TAVERN_PROMPT_SAFETY_MARGIN",
    1024,
    minimum=128,
    maximum=32768,
)
MODEL_CONTEXT_CACHE_SECONDS = _configured_int(
    "TAVERN_MODEL_CONTEXT_CACHE_SECONDS",
    300,
    minimum=1,
    maximum=86400,
)


# 项目根目录与关键子路径。
# 测试进程在导入应用前注入 TAVERN_* 路径，生产环境不设变量时行为不变。
BASE_DIR = _configured_path("TAVERN_BASE_DIR", Path("C:/local-tavern"))
DATA_DIR = _configured_path("TAVERN_DATA_DIR", BASE_DIR / "data")
SETTINGS_PATH = _configured_path("TAVERN_SETTINGS_PATH", DATA_DIR / "settings.json")
PROJECTS_DIR = _configured_path("TAVERN_PROJECTS_DIR", DATA_DIR / "projects")
WEB_DIR = _configured_path("TAVERN_WEB_DIR", BASE_DIR / "web")
PROMPTS_DIR = _configured_path("TAVERN_PROMPTS_DIR", BASE_DIR / "prompts")
RECOVERY_DIR = _configured_path("TAVERN_RECOVERY_DIR", DATA_DIR / ".recovery")
MIGRATIONS_DIR = _configured_path("TAVERN_MIGRATIONS_DIR", DATA_DIR / ".migrations")
BACKUPS_DIR = _configured_path("TAVERN_BACKUP_DIR", BASE_DIR / "backups")
LOG_DIR = _configured_path("TAVERN_LOG_DIR", BASE_DIR / "logs")

# 恢复记录只写到期时间；永久清理由显式管理命令执行，不能在读取路径自动删除。
TRASH_RETENTION_DAYS = _configured_int(
    "TAVERN_TRASH_RETENTION_DAYS",
    30,
    minimum=1,
    maximum=3650,
)
BACKUP_RETENTION_DAYS = _configured_int(
    "TAVERN_BACKUP_RETENTION_DAYS",
    30,
    minimum=1,
    maximum=3650,
)
BACKUP_RETENTION_COUNT = _configured_int(
    "TAVERN_BACKUP_RETENTION_COUNT",
    10,
    minimum=1,
    maximum=1000,
)
BACKUP_SCHEDULE_ENABLED = _configured_bool(
    "TAVERN_BACKUP_SCHEDULE_ENABLED",
    True,
)
BACKUP_INTERVAL_HOURS = _configured_int(
    "TAVERN_BACKUP_INTERVAL_HOURS",
    24,
    minimum=1,
    maximum=24 * 365,
)
BACKUP_DRILL_INTERVAL_DAYS = _configured_int(
    "TAVERN_BACKUP_DRILL_INTERVAL_DAYS",
    7,
    minimum=1,
    maximum=365,
)
BACKUP_SCHEDULER_POLL_SECONDS = _configured_int(
    "TAVERN_BACKUP_SCHEDULER_POLL_SECONDS",
    900,
    minimum=60,
    maximum=24 * 60 * 60,
)

OLLAMA_HOST = os.environ.get("TAVERN_OLLAMA_HOST", "http://localhost:11434").strip().rstrip("/")
if not OLLAMA_HOST:
    raise ValueError("TAVERN_OLLAMA_HOST 不能为空")

# 默认存档名（单一事实源）
DEFAULT_SAVE = "默认存档"
