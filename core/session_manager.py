"""存档管理 — 支持多项目

存档结构（JSON）：
{
  "session_id": "default",
  "name": "显示名",
  "project": "所属项目",
  "created_at": "...",
  "updated_at": "...",
  "current_model": "...",
  "scene_meta": { ... },
  "user_status": { ... },
  "characters_state": { ... },
  "message_history": [ ... ]
}

路径：data/projects/<项目>/saves/<存档id>.json
"""
import json
import logging
import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.config import MAX_MESSAGES_IN_SAVE, HARD_LIMIT, PROJECTS_DIR, DEFAULT_SAVE

logger = logging.getLogger(__name__)

ROOT_DIR = PROJECTS_DIR

_save_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _get_save_lock(project: str, save_id: str) -> asyncio.Lock:
    """获取（或惰性创建）某项目+存档对应的写锁。

    按存档隔离并发：不同项目、不同存档互不阻塞，同一存档串行写。
    _locks_guard 负责安全地创建新锁。
    """
    key = f"{project}/{save_id}"
    lock = _save_locks.get(key)
    if lock is not None:
        return lock
    async with _locks_guard:
        lock = _save_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _save_locks[key] = lock
        return lock


def _saves_dir(project: str) -> Path:
    """获取项目的存档目录"""
    d = ROOT_DIR / project / "saves"
    d.mkdir(parents=True, exist_ok=True)
    return d


def atomic_write(path: Path, data: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def _empty_session(save_id: str = DEFAULT_SAVE, project: str = "默认项目") -> dict:
    return {
        "session_id": save_id,
        "name": save_id,
        "project": project,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "current_model": "",
        "scene_meta": {
            "location": "", "time": "", "weather": "",
            "main_quest": "", "current_scene": "", "next_goal": "",
        },
        "user_status": {
            "name": "", "identity": "", "condition": "", "abilities": [],
        },
        "characters_state": {},
        "message_history": [],
        "summaries": [],
        "summary_error": "",
    }


def load_session(project: str, save_id: str = DEFAULT_SAVE) -> dict:
    path = _saves_dir(project) / f"{save_id}.json"
    if not path.exists():
        return _empty_session(save_id, project)
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("project", project)
        s.setdefault("summaries", [])
        s.setdefault("summary_error", "")
        for m in s.get("message_history", []):
            m.setdefault("pinned", False)
        # 惰性迁移：旧存档非 pinned 条数超过存档窗口时，截断并把被截原文写 trim 快照留底。
        # 避免导入/历史遗留的超长存档在第一次 chat 前就已超标却无人收敛。
        _migrate_trim_if_needed(s, project)
        return s
    except (json.JSONDecodeError, OSError) as e:
        logger.error("存档加载失败，使用空存档: %s", e)
        return _empty_session(save_id, project)


async def aload_session(project: str, save_id: str = DEFAULT_SAVE) -> dict:
    """异步版本 load_session：用 asyncio.to_thread 把阻塞 IO 放到线程池。

    避免 event loop 在大存档读取时被卡住。语义与同步版一致。
    """
    return await asyncio.to_thread(load_session, project, save_id)


async def save_session(session: dict, project: str, save_id: str):
    lock = await _get_save_lock(project, save_id)
    async with lock:
        # ponytail: 轻量 CAS。记录 session 加载时的版本号，与磁盘当前版本比较；
        # 若被并发修改过则合并 message_history 而非简单覆盖。单用户桌面应用极少触发。
        old_version = session.get("updated_at", "")
        session["updated_at"] = datetime.now().isoformat()
        session["session_id"] = save_id
        session["project"] = project
        path = _saves_dir(project) / f"{save_id}.json"
        if path.exists() and old_version:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                current_ver = current.get("updated_at", "")
                if current_ver and current_ver > old_version:
                    # 文件已被并发修改：合并 message_history（保留新条目的同时不丢当前 session 的修改）
                    existing_ids = {id(m) for m in current.get("message_history", [])}
                    for m in session.get("message_history", []):
                        if id(m) not in existing_ids:
                            current.setdefault("message_history", []).append(m)
                    # 场景元数据非空字段合并
                    for k, v in session.get("scene_meta", {}).items():
                        if v and not current.get("scene_meta", {}).get(k):
                            current.setdefault("scene_meta", {})[k] = v
                    # characters_state 合并
                    for cid, state in session.get("characters_state", {}).items():
                        if cid not in current.get("characters_state", {}):
                            current.setdefault("characters_state", {})[cid] = state
                    current["updated_at"] = datetime.now().isoformat()
                    session = current
                    logger.info("save_session: 检测到并发修改，已合并（CAS）")
            except (json.JSONDecodeError, OSError):
                pass
        atomic_write(path, json.dumps(session, ensure_ascii=False, indent=2))


def append_history(session: dict, role: str, content: str, thinking: str = ""):
    msg = {"role": role, "content": content}
    if thinking:
        msg["thinking"] = thinking
    session.setdefault("message_history", []).append(msg)


def trim_history(session: dict, max_messages: int = MAX_MESSAGES_IN_SAVE, project: Optional[str] = None):
    """截断消息历史到最近 max_messages 条非 pinned 消息，保留全部 pinned，被截部分自动写 trim 快照。

    - pinned: true 的消息永不截断、常驻。
    - 当 project 传入时，被截掉的非 pinned 消息整体落盘 .history/{sid}.trim.{ts}.json 留底。
    - 硬上限：单存档 history > HARD_LIMIT 时，即使未达 max_messages 也强制 trim 一次，防止异常情况下 history 无限增长。

    注意：max_messages 是"存档窗口"（非 pinned 条数），不同于 prompt_builder 的"prompt 窗口"。
    存档窗口 ≥ prompt 窗口，差额给快照/导出/重生成留原文回溯余量。两者均由 core/config.py 集中定义。
    """
    max_msgs = max_messages
    history = session.get("message_history", [])
    if len(history) <= max_msgs and len(history) <= HARD_LIMIT:
        return
    if len(history) > HARD_LIMIT:
        max_msgs = HARD_LIMIT

    # 分离 pinned（保留）与非 pinned（参与截断）
    pinned = [m for m in history if m.get("pinned")]
    non_pinned = [m for m in history if not m.get("pinned")]

    # 对非 pinned 部分取最近 max_msgs 条；pinned 全部保留
    keep_non = non_pinned[-max_msgs:]
    dropped = non_pinned[:-max_msgs] if len(non_pinned) > max_msgs else []

    # 被截掉的部分写 trim 快照留底
    if dropped and project:
        _save_trim_snapshot(session, project, dropped)

    # 合并并保持原有相对顺序：保留所有 pinned + keep_non（最近非pinned）
    keep_ids = {id(m) for m in keep_non}
    new_history = [m for m in history if m.get("pinned") or id(m) in keep_ids]
    session["message_history"] = new_history

    return dropped


def _save_trim_snapshot(session: dict, project: str, dropped: list):
    """把截断掉的消息整体存为 trim 快照（区别于 regenerate 的全量快照）。"""
    d = _saves_dir(project) / ".history"
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sid = session.get("session_id", DEFAULT_SAVE) or DEFAULT_SAVE
    path = d / f"{sid}.trim.{ts}.json"
    payload = {
        "_snapshot_at": datetime.now().isoformat(),
        "_snapshot_type": "trim",
        "session_id": sid,
        "project": project,
        "dropped_count": len(dropped),
        "dropped_messages": dropped,
    }
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _migrate_trim_if_needed(session: dict, project: Optional[str]):
    """惰性迁移：若非 pinned 历史条数超过存档窗口，做一次 trim 并写原文留底快照。

    与 trim_history 的差别：这里只在 load 时对"明显超标"的旧存档兜底，
    不触发短期总结（总结由 chat 流程驱动，load 不做副作用 AI 调用）。
    """
    history = session.get("message_history", [])
    non_pinned = [m for m in history if not m.get("pinned")]
    if len(non_pinned) <= MAX_MESSAGES_IN_SAVE:
        return
    if project:
        trim_history(session, max_messages=MAX_MESSAGES_IN_SAVE, project=project)
    else:
        # 无 project 时只做内存截断，不写快照（load 路径总会带 project，这是兜底）
        trim_history(session, max_messages=MAX_MESSAGES_IN_SAVE)
    logger.info("惰性迁移截断超长存档 history→%d 条", len(session.get("message_history", [])))


def list_trim_snapshots(project: str, save_id: str = DEFAULT_SAVE) -> list[dict]:
    """列出某存档的所有 trim 快照（按时间倒序）。

    用于前端调试面板，让用户看到"哪些原文被总结覆盖了"。
    """
    d = _saves_dir(project) / ".history"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob(f"{save_id}.trim.*.json"), reverse=True):
        try:
            ts_str = p.stem.split(".trim.", 1)[1] if ".trim." in p.stem else ""
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            out.append({
                "filename": p.name,
                "timestamp": ts_str,
                "modified_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
                "dropped_count": data.get("dropped_count", 0),
                "snapshot_at": data.get("_snapshot_at", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return out


def reset_session(project: str, save_id: str = DEFAULT_SAVE) -> dict:
    """重置存档：归档（不删除）历史快照后清空主存档。

    - 旧存档文件 → .history/{sid}.reset.{ts}.json（仅在文件存在时归档）
    - .history/{sid}.trim.*.json（trim 快照）保留不动
    - .history/{sid}.{ts}.json（regenerate 全量快照）保留不动
    """
    path = _saves_dir(project) / f"{save_id}.json"
    if path.exists():
        archive_dir = _saves_dir(project) / ".history"
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = archive_dir / f"{save_id}.reset.{ts}.json"
        try:
            atomic_write(archive_path, path.read_text(encoding="utf-8"))
        except OSError as e:
            logger.warning("归档旧存档失败: %s", e)
        path.unlink()
    return _empty_session(save_id, project)


# ============ 多存档管理 ============

def _safe_filename(name: str) -> str:
    name = re.sub(r'\.(json|tmp)$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[\\/:*?"<>|\s]', '_', name)
    name = name.strip('_')
    return name[:80] if name else "save"


def list_sessions(project: str) -> list[dict]:
    d = _saves_dir(project)
    sessions = []
    for p in d.glob("*.json"):
        if p.stem.startswith("."):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            sessions.append({
                "session_id": data.get("session_id", p.stem),
                "name": data.get("name", p.stem),
                "updated_at": data.get("updated_at", ""),
                "created_at": data.get("created_at", ""),
                "message_count": len(data.get("message_history", [])),
                "current_model": data.get("current_model", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue
    sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return sessions


def session_exists(project: str, save_id: str) -> bool:
    """save_id 应是已 safe 的文件名 stem（路由入口 _norm_save 把关）。"""
    return (_saves_dir(project) / f"{save_id}.json").exists()


async def create_session(project: str, name: str) -> dict:
    safe = _safe_filename(name)
    final = safe
    i = 2
    while session_exists(project, final):
        final = f"{safe}_{i}"
        i += 1
    s = _empty_session(final, project)
    s["name"] = name
    await save_session(s, project, final)
    return s


async def rename_session(project: str, old_id: str, new_name: str) -> dict:
    if not session_exists(project, old_id):
        raise FileNotFoundError(f"存档 {old_id} 不存在")
    session = load_session(project, old_id)
    session["name"] = new_name
    new_safe = _safe_filename(new_name)
    if new_safe != old_id:
        if session_exists(project, new_safe):
            raise FileExistsError(f"存档 {new_safe} 已存在")
        # 迁移历史快照 → 先写新文件再删旧文件，防止磁盘故障导致数据丢失
        hist_dir = _saves_dir(project) / ".history"
        if hist_dir.exists():
            for p in hist_dir.iterdir():
                if p.name.startswith(old_id + "."):
                    new_filename = new_safe + p.name[len(old_id):]
                    try:
                        p.rename(hist_dir / new_filename)
                    except OSError:
                        pass
        await save_session(session, project, new_safe)
        (_saves_dir(project) / f"{old_id}.json").unlink()
    else:
        await save_session(session, project, old_id)
    return session


async def delete_session(project: str, save_id: str) -> bool:
    sessions = list_sessions(project)
    if len(sessions) <= 1 and session_exists(project, save_id):
        raise ValueError("至少保留 1 个存档")
    path = _saves_dir(project) / f"{save_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def export_session(project: str, save_id: str) -> str:
    return json.dumps(load_session(project, save_id), ensure_ascii=False, indent=2)


async def import_session(project: str, json_str: str, name: str = None) -> dict:
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")
    if "session_id" not in data:
        raise ValueError("缺少 session_id 字段")
    target_name = name or data.get("name", data["session_id"])
    safe = _safe_filename(target_name)
    if session_exists(project, safe):
        safe = f"{safe}_imported"
    data["session_id"] = safe
    data["name"] = target_name
    data["project"] = project
    # 校验 history 条目 schema，仅保留合法角色
    history = data.get("message_history")
    if isinstance(history, list):
        original_len = len(history)
        data["message_history"] = [m for m in history if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
        if len(data["message_history"]) < original_len:
            logger.warning("导入时剔除了 %d 条非法 role 的 history 条目", original_len - len(data["message_history"]))
    await save_session(data, project, safe)
    return data


def toggle_pinned(project: str, save_id: str, index: int) -> dict:
    """翻转某条消息的 pinned 标记，返回更新后的 session（同步 IO）。"""
    session = load_session(project, save_id)
    history = session.setdefault("message_history", [])
    if index < 0 or index >= len(history):
        raise IndexError("无效的 index")
    msg = history[index]
    msg["pinned"] = not msg.get("pinned", False)
    return session


async def atoggle_pinned(project: str, save_id: str, index: int) -> dict:
    """异步翻转 pinned：先读 → 翻转 → 写盘（原子操作，避免 PATCH 路由漏 save）。"""
    session = await aload_session(project, save_id)
    history = session.setdefault("message_history", [])
    if index < 0 or index >= len(history):
        raise IndexError("无效的 index")
    history[index]["pinned"] = not history[index].get("pinned", False)
    await save_session(session, project, save_id)
    return session


def count_pinned(session: dict) -> int:
    """统计 pinned 消息数。包含 user、assistant、system 等所有角色。"""
    return sum(1 for m in session.get("message_history", []) if m.get("pinned"))
