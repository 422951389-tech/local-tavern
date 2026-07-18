"""跨模块共享的窗口、上限与路径配置单一事实源。

所有"历史窗口"相关数值集中在此，消除 trim 与 prompt_builder 各自硬编码、
语义靠注释硬撑导致的隐性裂缝。

语义约定：
- MAX_TURNS_IN_PROMPT : 喂给模型的对话轮数；messages 序列注入条数 = 该值 * 2（每轮 user+assistant）。
- MAX_MESSAGES_IN_SAVE : 存档保留的非 pinned 消息条数；≥ prompt 窗口，差额给快照/导出/重生成留原文回溯余量。
- HARD_LIMIT          : 单存档 message_history 异常增长（批量导入等）的强制 trim 硬上限。
"""
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

MAX_TURNS_IN_PROMPT = 10
MAX_MESSAGES_IN_SAVE = 40
HARD_LIMIT = 200

def _configured_path(env_name: str, default: Path) -> Path:
    """读取可选绝对路径配置；未配置时保留本地酒馆的既有目录布局。"""
    raw = os.environ.get(env_name)
    if raw is None:
        return default.resolve(strict=False)
    text = raw.strip()
    if not text:
        raise ValueError(f"{env_name} 不能为空")
    configured = Path(text).expanduser()
    if not configured.is_absolute():
        raise ValueError(f"{env_name} 必须是绝对路径")
    return configured.resolve(strict=False)


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


_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _configured_host(env_name: str, default: str, *, allow_remote: bool) -> str:
    """读取 uvicorn 监听地址；远程监听必须由显式开关授权。"""
    raw = os.environ.get(env_name, default)
    host = raw.strip()
    if not host:
        raise ValueError(f"{env_name} 不能为空")
    if any(character.isspace() for character in host) or any(
        token in host for token in ("/", "\\", "://", "[", "]")
    ):
        raise ValueError(f"{env_name} 必须是主机名或 IP 地址")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or any(
            not _HOST_LABEL.fullmatch(label) for label in host.split(".")
        ):
            raise ValueError(f"{env_name} 必须是主机名或 IP 地址") from None
        is_loopback = host.casefold() == "localhost"
    else:
        is_loopback = address.is_loopback

    if not is_loopback and not allow_remote:
        raise ValueError(
            f"{env_name} 非本机回环地址时必须显式设置 TAVERN_ALLOW_REMOTE=true"
        )
    return host


def _configured_http_url(env_name: str, default: str) -> str:
    """读取无凭据、无路径的 HTTP(S) 服务根地址。"""
    raw = os.environ.get(env_name, default)
    value = raw.strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"{env_name} 必须是有效的 HTTP(S) 根地址")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(f"{env_name} 必须是有效的 HTTP(S) 根地址") from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(f"{env_name} 必须是有效的 HTTP(S) 根地址")
    return value.rstrip("/")


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

# 服务、进程守卫、健康探测与日志共同使用的运行参数。
ALLOW_REMOTE = _configured_bool("TAVERN_ALLOW_REMOTE", False)
HOST = _configured_host("TAVERN_HOST", "127.0.0.1", allow_remote=ALLOW_REMOTE)
PORT = _configured_int("TAVERN_PORT", 8765, minimum=1, maximum=65535)
PID_PATH = _configured_path("TAVERN_PID_PATH", BASE_DIR / "tavern.pid")
STOP_REQUEST_PATH = _configured_path(
    "TAVERN_STOP_REQUEST_PATH",
    BASE_DIR / "tavern.stop.pid",
)
LOG_FILE = _configured_path("TAVERN_LOG_FILE", LOG_DIR / "tavern.log")
LOG_MAX_BYTES = _configured_int(
    "TAVERN_LOG_MAX_BYTES",
    5 * 1024 * 1024,
    minimum=64 * 1024,
    maximum=1024 * 1024 * 1024,
)
LOG_BACKUP_COUNT = _configured_int(
    "TAVERN_LOG_BACKUP_COUNT",
    5,
    minimum=1,
    maximum=100,
)
OLLAMA_HEALTH_TIMEOUT_MS = _configured_int(
    "TAVERN_OLLAMA_HEALTH_TIMEOUT_MS",
    2000,
    minimum=100,
    maximum=60_000,
)

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

OLLAMA_HOST = _configured_http_url(
    "TAVERN_OLLAMA_HOST",
    "http://localhost:11434",
)

# 默认存档名（单一事实源）
DEFAULT_SAVE = "默认存档"
