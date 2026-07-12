"""Prompt 编辑器 — 读/写/恢复提示词文件

设计：
- 主文件: PROMPTS_DIR/{name}.md
- 默认备份: PROMPTS_DIR/.default/{name}.md（首次启动时拷贝）
"""
import shutil

from core.config import PROMPTS_DIR


DEFAULT_DIR = PROMPTS_DIR / ".default"

VALID_NAMES = ("system", "group_chat")


def _ensure_defaults():
    """确保默认备份存在（首次启动时把当前 prompt 拷到 .default）"""
    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
    for name in VALID_NAMES:
        default_path = DEFAULT_DIR / f"{name}.md"
        if not default_path.exists():
            src = PROMPTS_DIR / f"{name}.md"
            if src.exists():
                shutil.copy2(src, default_path)


def read_prompt(name: str) -> str:
    """读取指定 prompt"""
    if name not in VALID_NAMES:
        raise ValueError(f"无效 prompt 名: {name}")
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_prompt(name: str, content: str):
    """写入 prompt（原子写）"""
    if name not in VALID_NAMES:
        raise ValueError(f"无效 prompt 名: {name}")
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_defaults()

    path = PROMPTS_DIR / f"{name}.md"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _get_default_prompt(name: str) -> str:
    """获取 prompt 的默认备份。

    优先取最新一份 `.v*-bak`（v3+ 改造时备份的版本），若不存在则用老的 `.md`。
    这样"恢复默认"会回到项目最近一次重大改造时的版本，而不是更老的初始版本。
    """
    bak_files = sorted(DEFAULT_DIR.glob(f"{name}.md.v*-bak"), reverse=True)
    if bak_files:
        return bak_files[0].read_text(encoding="utf-8")
    fallback = DEFAULT_DIR / f"{name}.md"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8")
    return ""


def reset_prompt_to_default(name: str) -> str:
    """恢复默认 prompt（优先用最新的 .v*-bak 备份）"""
    if name not in VALID_NAMES:
        raise ValueError(f"无效 prompt 名: {name}")
    _ensure_defaults()
    content = _get_default_prompt(name)
    if not content:
        raise FileNotFoundError(f"默认备份不存在: {name}")
    write_prompt(name, content)
    return content
