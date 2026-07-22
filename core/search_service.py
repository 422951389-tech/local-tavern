"""当前项目权威 Session 的有界、只读字面检索。"""
from __future__ import annotations

import json
import os
import re
import stat
from collections import deque
from pathlib import Path
from typing import Iterable, Iterator

from core.library_lock import library_lock
from core.path_policy import (
    PathPolicyError,
    resolve_project_dir,
    validate_file_id,
)
from core.recovery_store import DataCorruptionError
from core.session_store import SessionStore, normalize_session


SEARCH_SCOPES = frozenset({"all", "messages", "summaries", "pinned"})
MAX_SEARCH_QUERY_LENGTH = 128
MAX_SEARCH_LIMIT = 100
MAX_SEARCH_SESSION_FILES = 500
MAX_SEARCH_SOURCES = 50_000
MAX_SEARCH_BYTES = 64 * 1024 * 1024
MAX_SEARCH_SNIPPET_LENGTH = 240
MAX_CREDENTIAL_SPANS = 1024
MAX_SAFE_INTEGER = (1 << 53) - 1

_REDACTION_MARKER = "[凭据已隐藏]"
_SAFE_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?$"
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<pem_type>(?:[A-Z0-9]+ )*PRIVATE KEY)-----.*?"
    r"(?:-----END (?P=pem_type)-----|\Z)",
    re.DOTALL,
)
_BEARER_RE = re.compile(r"\bBearer[ \t]+(?P<value>[^\s,;，；。]+)", re.IGNORECASE)
_URL_USERINFO_RE = re.compile(
    r"\b[a-z][a-z0-9+.-]*://(?P<value>[^/@\s]+)@",
    re.IGNORECASE,
)
_ASSIGNMENT_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?P<key_quote>[\"']?)(?P<name>[A-Za-z][A-Za-z0-9_.-]{0,79})(?P=key_quote)"
    r"[ \t]*(?:=|:)[ \t]*",
    re.IGNORECASE,
)
_SPACED_ASSIGNMENT_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<key_quote>[\"']?)(?P<name>api[ \t]+key|access[ \t]+token|refresh[ \t]+token|"
    r"auth[ \t]+token|client[ \t]+secret|secret[ \t]+key)"
    r"(?P=key_quote)[ \t]*(?:=|:)[ \t]*",
    re.IGNORECASE,
)
_BRACKET_ASSIGNMENT_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?:"
    r"(?:os\.)?environ|process\.env|env"
    r")[ \t]*\[[ \t]*(?P<key_quote>[\"'])"
    r"(?P<name>[A-Za-z][A-Za-z0-9_. -]{0,79})(?P=key_quote)"
    r"[ \t]*\][ \t]*(?:=|:)[ \t]*",
    re.IGNORECASE,
)
_BARE_TOKEN_RES = (
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:hf|npm)_[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bmfa\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\b"
    ),
)


class SearchError(RuntimeError):
    """可映射为不泄露磁盘细节的稳定 API 错误。"""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": str(self)}


class SearchInputError(SearchError):
    pass


class SearchLimitExceeded(SearchError):
    pass


class SearchProjectNotFound(SearchError):
    pass


# 兼容调用方使用更短的异常名。
SearchLimitError = SearchLimitExceeded


def normalize_search_query(q: object) -> str:
    if not isinstance(q, str):
        raise SearchInputError("搜索关键词必须是字符串", code="invalid_search_query")
    query = q.strip()
    if len(query) > MAX_SEARCH_QUERY_LENGTH:
        raise SearchInputError(
            f"搜索关键词不能超过 {MAX_SEARCH_QUERY_LENGTH} 个字符",
            code="invalid_search_query",
        )
    return query


def validate_search_scope(scope: object) -> str:
    if not isinstance(scope, str) or scope not in SEARCH_SCOPES:
        raise SearchInputError("搜索范围无效", code="invalid_search_scope")
    return scope


def validate_search_limit(limit: object) -> int:
    if isinstance(limit, str) and re.fullmatch(r"[0-9]+", limit):
        limit = int(limit)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > MAX_SEARCH_LIMIT
    ):
        raise SearchInputError(
            f"搜索结果上限必须在 1 到 {MAX_SEARCH_LIMIT} 之间",
            code="invalid_search_limit",
        )
    return limit


def empty_search_response(project: str, scope: str) -> dict:
    return {
        "project": project,
        "scope": scope,
        "results": [],
        "total_matches": 0,
        "truncated": False,
        "scanned": {"sessions": 0, "sources": 0, "bytes": 0},
        "skipped": [],
    }


def _is_credential_key(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return normalized == "pwd" or normalized.endswith((
        "apikey",
        "token",
        "password",
        "passwd",
        "passphrase",
        "secret",
        "secretkey",
        "secretaccesskey",
        "accesskey",
        "privatekey",
        "credential",
        "credentials",
    ))


def _assignment_value_span(text: str, start: int) -> tuple[int, int] | None:
    """读取一个赋值值；引号内支持反斜杠转义，裸值允许多词口令。"""
    if start >= len(text):
        return None
    quote = text[start]
    if quote in {'"', "'"}:
        value_start = start + 1
        cursor = value_start
        escaped = False
        while cursor < len(text):
            character = text[cursor]
            if character in "\r\n":
                break
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                break
            cursor += 1
        return (value_start, cursor) if cursor > value_start else None

    cursor = start
    while cursor < len(text) and text[cursor] not in "\r\n,;，；。)}]":
        cursor += 1
    while cursor > start and text[cursor - 1] in " \t":
        cursor -= 1
    return (start, cursor) if cursor > start else None


def _credential_spans(text: str) -> list[tuple[int, int]] | None:
    spans: list[tuple[int, int]] = []

    def collect(matches: Iterable[tuple[int, int]]) -> bool:
        for start, end in matches:
            spans.append((start, end))
            if len(spans) > MAX_CREDENTIAL_SPANS:
                return False
        return True

    if not collect(match.span() for match in _PEM_PRIVATE_KEY_RE.finditer(text)):
        return None
    if not collect(match.span("value") for match in _BEARER_RE.finditer(text)):
        return None
    if not collect(match.span("value") for match in _URL_USERINFO_RE.finditer(text)):
        return None

    for pattern in (
        _ASSIGNMENT_PREFIX_RE,
        _SPACED_ASSIGNMENT_PREFIX_RE,
        _BRACKET_ASSIGNMENT_PREFIX_RE,
    ):
        for match in pattern.finditer(text):
            if not _is_credential_key(match.group("name")):
                continue
            value_span = _assignment_value_span(text, match.end())
            if value_span is None:
                continue
            spans.append(value_span)
            if len(spans) > MAX_CREDENTIAL_SPANS:
                return None

    for pattern in _BARE_TOKEN_RES:
        if not collect(match.span() for match in pattern.finditer(text)):
            return None

    if not spans:
        return []
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _redact_credentials(text: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    spans = _credential_spans(text)
    if spans is None:
        return _REDACTION_MARKER, ((0, len(_REDACTION_MARKER)),)
    if not spans:
        return text, ()

    pieces: list[str] = []
    redacted_spans: list[tuple[int, int]] = []
    cursor = 0
    output_length = 0
    for start, end in spans:
        visible = text[cursor:start]
        pieces.append(visible)
        output_length += len(visible)
        pieces.append(_REDACTION_MARKER)
        marker_end = output_length + len(_REDACTION_MARKER)
        redacted_spans.append((output_length, marker_end))
        output_length = marker_end
        cursor = end
    visible = text[cursor:]
    pieces.append(visible)
    return "".join(pieces), tuple(redacted_spans)


def _kmp_prefix(pattern: str) -> list[int]:
    prefix = [0] * len(pattern)
    matched = 0
    for index in range(1, len(pattern)):
        while matched and pattern[index] != pattern[matched]:
            matched = prefix[matched - 1]
        if pattern[index] == pattern[matched]:
            matched += 1
        prefix[index] = matched
    return prefix


def _streaming_casefold_match(
    text: str,
    folded_query: str,
    redacted_spans: tuple[tuple[int, int], ...],
) -> tuple[int, int] | None:
    """流式字面匹配；坐标环大小只与最长 128 字符查询相关。"""
    if not folded_query:
        return None
    prefix = _kmp_prefix(folded_query)
    coordinates: deque[int] = deque(maxlen=len(folded_query))
    matched = 0
    redaction_index = 0
    for source_index, character in enumerate(text):
        for folded_character in character.casefold():
            coordinates.append(source_index)
            while matched and folded_character != folded_query[matched]:
                matched = prefix[matched - 1]
            if folded_character == folded_query[matched]:
                matched += 1
            if matched != len(folded_query):
                continue

            start = coordinates[0]
            end = source_index + 1
            while (
                redaction_index < len(redacted_spans)
                and redacted_spans[redaction_index][1] <= start
            ):
                redaction_index += 1
            overlaps_redaction = (
                redaction_index < len(redacted_spans)
                and redacted_spans[redaction_index][0] < end
            )
            if not overlaps_redaction:
                return start, end
            matched = prefix[matched - 1]
    return None


def _literal_match(text: str, folded_query: str) -> tuple[str, int, int] | None:
    redacted, redacted_spans = _redact_credentials(text)
    match = _streaming_casefold_match(redacted, folded_query, redacted_spans)
    if match is None:
        return None
    start, end = match
    return redacted, start, end


def _snippet(text: str, start: int, end: int) -> dict:
    matched = text[start:end]
    if len(matched) >= MAX_SEARCH_SNIPPET_LENGTH:
        return {
            "prefix": "",
            "match": matched[:MAX_SEARCH_SNIPPET_LENGTH],
            "suffix": "",
        }

    context_budget = MAX_SEARCH_SNIPPET_LENGTH - len(matched)
    prefix_length = min(start, context_budget // 2)
    suffix_length = min(len(text) - end, context_budget - prefix_length)
    remaining = context_budget - prefix_length - suffix_length
    if remaining:
        extra_prefix = min(start - prefix_length, remaining)
        prefix_length += extra_prefix
        remaining -= extra_prefix
    if remaining:
        suffix_length += min(len(text) - end - suffix_length, remaining)
    return {
        "prefix": text[start - prefix_length:start],
        "match": matched,
        "suffix": text[end:end + suffix_length],
    }


def _safe_timestamp(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_TIMESTAMP_RE.fullmatch(value):
        return ""
    return value


def _message_time(message: dict) -> str:
    timestamps = message.get("timestamps")
    if isinstance(timestamps, dict):
        created_at = _safe_timestamp(timestamps.get("created_at"))
        if created_at:
            return created_at
    return (
        _safe_timestamp(message.get("created_at"))
        or _safe_timestamp(message.get("time"))
    )


def _summary_values(summary: dict) -> Iterator[tuple[str, str]]:
    for field in ("text", "time"):
        value = summary.get(field)
        if isinstance(value, str):
            yield field, value
    for field in ("facts", "relations"):
        values = summary.get(field)
        if isinstance(values, str):
            yield field, values
        elif isinstance(values, list):
            for value in values:
                if isinstance(value, str):
                    yield field, value


def _skipped(save_id: str, code: str) -> dict:
    return {"save_id": save_id, "code": code}


def _safe_entry_save_id(path: Path) -> str:
    try:
        return validate_file_id(path.stem, label="存档 ID")
    except PathPolicyError:
        return ""


class _UnsupportedSessionEntry(OSError):
    """候选项不是可安全读取的普通文件。"""


def _stat_signature(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _read_stable_bytes(path: Path, byte_budget: int) -> bytes:
    """从同一普通文件句柄做受限读取，并拒绝读取期间的替换或改写。"""
    if byte_budget < 0:
        raise SearchLimitExceeded(
            "项目存档总量超过搜索上限",
            code="search_byte_limit_exceeded",
        )

    for _attempt in range(3):
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise _UnsupportedSessionEntry("搜索候选项不是普通文件")
        if before.st_size > byte_budget:
            raise SearchLimitExceeded(
                "项目存档总量超过搜索上限",
                code="search_byte_limit_exceeded",
            )

        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise _UnsupportedSessionEntry("搜索候选项不是普通文件")
            if _stat_signature(before) != _stat_signature(opened):
                continue
            if opened.st_size > byte_budget:
                raise SearchLimitExceeded(
                    "项目存档总量超过搜索上限",
                    code="search_byte_limit_exceeded",
                )
            payload = handle.read(byte_budget + 1)
            if len(payload) > byte_budget:
                raise SearchLimitExceeded(
                    "项目存档总量超过搜索上限",
                    code="search_byte_limit_exceeded",
                )
            after_read = os.fstat(handle.fileno())

        try:
            current = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise _UnsupportedSessionEntry("搜索候选项不是普通文件")
        signature = _stat_signature(opened)
        if (
            _stat_signature(before) == signature
            and _stat_signature(after_read) == signature
            and _stat_signature(current) == signature
            and len(payload) == opened.st_size
        ):
            return payload

    raise OSError("搜索候选项在读取期间持续变化")


def _decode_session_snapshot(path: Path, payload: bytes, project: str, save_id: str) -> dict:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataCorruptionError.from_bytes(
            path,
            payload,
            entity_type="session",
            project=project,
            entity_id=save_id,
            reason=f"JSON 解析失败: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise DataCorruptionError.from_bytes(
            path,
            payload,
            entity_type="session",
            project=project,
            entity_id=save_id,
            reason="JSON 顶层必须是对象",
        )
    if _contains_surrogate(data):
        raise DataCorruptionError.from_bytes(
            path,
            payload,
            entity_type="session",
            project=project,
            entity_id=save_id,
            reason="Session 包含不可序列化的 Unicode surrogate",
        )
    return normalize_session(data, project, save_id)


def _contains_surrogate(value: object) -> bool:
    """以深度有界的迭代方式拒绝 JSON 中的非 Unicode 标量值。"""
    iterators = [iter((value,))]
    while iterators:
        try:
            current = next(iterators[-1])
        except StopIteration:
            iterators.pop()
            continue
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                return True
        elif isinstance(current, dict):
            iterators.append(iter(current.items()))
        elif isinstance(current, tuple) and len(current) == 2:
            iterators.append(iter(current))
        elif isinstance(current, list):
            iterators.append(iter(current))
    return False


class SearchService:
    """在一个 projects 根目录中搜索，不创建目录、不修改或隔离坏档。"""

    def __init__(self, store: SessionStore | Path):
        self.store = store if isinstance(store, SessionStore) else SessionStore(store)
        self.projects_root = self.store.projects_root

    def _entries(self, project: str) -> Iterable[Path]:
        project_dir = resolve_project_dir(self.projects_root, project)
        if not project_dir.is_dir():
            raise SearchProjectNotFound(
                "项目不存在",
                code="search_project_not_found",
            )
        saves_dir = self.store.saves_dir(project)
        if not saves_dir.is_dir():
            return ()
        try:
            entries: list[Path] = []
            for path in saves_dir.iterdir():
                if path.name.startswith(".") or path.suffix != ".json":
                    continue
                entries.append(path)
                if len(entries) > MAX_SEARCH_SESSION_FILES:
                    raise SearchLimitExceeded(
                        "项目存档数量超过搜索上限",
                        code="search_file_limit_exceeded",
                    )
        except OSError as exc:
            raise SearchError("搜索数据暂时不可读取", code="search_unavailable") from exc
        return sorted(entries, key=lambda path: (path.name.casefold(), path.name))

    def _read_snapshot_bytes(self, path: Path, byte_budget: int) -> bytes:
        return _read_stable_bytes(path, byte_budget)

    def search(
        self,
        *,
        project: str,
        q: str,
        scope: str = "all",
        limit: int = 50,
    ) -> dict:
        query = normalize_search_query(q)
        scope = validate_search_scope(scope)
        limit = validate_search_limit(limit)
        try:
            project = validate_file_id(project, label="项目 ID")
        except PathPolicyError as exc:
            raise SearchInputError("项目 ID 无效", code="invalid_search_project") from exc
        if not query:
            return empty_search_response(project, scope)

        folded_query = query.casefold()
        response = empty_search_response(project, scope)
        results = response["results"]
        scanned = response["scanned"]
        skipped = response["skipped"]
        total_matches = 0

        with library_lock.shared():
            for path in self._entries(project):
                entry_save_id = _safe_entry_save_id(path)
                try:
                    payload = self._read_snapshot_bytes(
                        path,
                        MAX_SEARCH_BYTES - scanned["bytes"],
                    )
                except _UnsupportedSessionEntry:
                    skipped.append(_skipped(entry_save_id, "unsupported_session_entry"))
                    continue
                except SearchLimitExceeded:
                    raise
                except OSError:
                    skipped.append(_skipped(entry_save_id, "session_unavailable"))
                    continue
                scanned["bytes"] += len(payload)

                save_id = entry_save_id
                if not save_id:
                    skipped.append(_skipped("", "invalid_save_id"))
                    continue

                try:
                    session = _decode_session_snapshot(path, payload, project, save_id)
                except DataCorruptionError:
                    skipped.append(_skipped(save_id, "session_corrupt"))
                    continue
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    skipped.append(_skipped(save_id, "session_unavailable"))
                    continue
                except (TypeError, ValueError, KeyError, AttributeError, UnicodeError, RecursionError):
                    skipped.append(_skipped(save_id, "session_corrupt"))
                    continue

                scanned["sessions"] += 1
                revision = session.get("revision", 0)
                if (
                    isinstance(revision, bool)
                    or not isinstance(revision, int)
                    or revision < 0
                    or revision > MAX_SAFE_INTEGER
                ):
                    skipped.append(_skipped(save_id, "session_corrupt"))
                    continue

                if scope in {"all", "messages", "pinned"}:
                    history = session.get("message_history")
                    if isinstance(history, list):
                        for message in history:
                            if not isinstance(message, dict):
                                continue
                            pinned = message.get("pinned") is True
                            if scope == "pinned" and not pinned:
                                continue
                            scanned["sources"] += 1
                            if scanned["sources"] > MAX_SEARCH_SOURCES:
                                raise SearchLimitExceeded(
                                    "项目可搜索来源超过上限",
                                    code="search_source_limit_exceeded",
                                )
                            content = message.get("content")
                            if not isinstance(content, str):
                                continue
                            role = message.get("role")
                            if role not in {"user", "assistant"}:
                                continue
                            match = _literal_match(content, folded_query)
                            if match is None:
                                continue
                            redacted, start, end = match
                            total_matches += 1
                            if len(results) < limit:
                                results.append({
                                    "kind": "message",
                                    "save_id": save_id,
                                    "revision": revision,
                                    "time": _message_time(message),
                                    "field": "content",
                                    "snippet": _snippet(redacted, start, end),
                                    "message_id": message["id"],
                                    "role": role,
                                    "pinned": pinned,
                                })

                if scope in {"all", "summaries"}:
                    summaries = session.get("summaries")
                    if isinstance(summaries, list):
                        for summary in summaries:
                            if not isinstance(summary, dict):
                                continue
                            scanned["sources"] += 1
                            if scanned["sources"] > MAX_SEARCH_SOURCES:
                                raise SearchLimitExceeded(
                                    "项目可搜索来源超过上限",
                                    code="search_source_limit_exceeded",
                                )
                            for field, value in _summary_values(summary):
                                match = _literal_match(value, folded_query)
                                if match is None:
                                    continue
                                redacted, start, end = match
                                total_matches += 1
                                if len(results) < limit:
                                    results.append({
                                        "kind": "summary",
                                        "save_id": save_id,
                                        "revision": revision,
                                        "time": _safe_timestamp(summary.get("created_at")),
                                        "field": field,
                                        "snippet": _snippet(redacted, start, end),
                                        "summary_id": summary["id"],
                                    })
                                break

        response["total_matches"] = total_matches
        response["truncated"] = total_matches > len(results)
        return response
