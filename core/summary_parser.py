"""短期总结解析器

把 ollama_client.summarize_once 产出的纯文本解析成结构化字段：
  - text:       前情提要（纯文本；解析失败时回退为完整原文）
  - time:       时间线一句话（可空）
  - facts:      关键事件列表（可空数组）
  - relations:  角色关系/立场列表（可空数组）

设计原则：解析只负责无损结构化，不截断、不补占位符；长度、数量和必填语义
由 summary_lifecycle 的统一验证器决定，违规输出必须进入 failed。
"""
import re
from typing import Optional


def parse_summary(raw: str) -> dict:
    """解析 summarize_once 的输出。

    返回: {"text": str, "time": str, "facts": [str], "relations": [str]}
    """
    out = {"text": "", "time": "", "facts": [], "relations": []}
    if not raw or not raw.strip():
        return out

    text = raw.strip()
    # 剥掉可能的外层代码块
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

    # 提取"前情提要"
    t = _extract_field(text, r"前情提要[^:：]*[:：]\s*(.+?)(?=\n\s*(时间线|关键事件|角色关系|关系))", r"前情提要[^:：]*[:：]\s*(.+?)$")
    if t:
        out["text"] = t.strip()
    else:
        # 兜底：整段当 text（已剔除空"无"）
        out["text"] = _clean_fallback_text(text)

    out["time"] = _extract_field(text, r"时间线[^:：]*[:：]\s*(.+?)(?=\n\s*(关键事件|角色关系|关系))", r"时间线[^:：]*[:：]\s*(.+?)$") or ""
    if out["time"]:
        out["time"] = out["time"].strip()
        if out["time"] in ("无", "无。", "—", "-"):
            out["time"] = ""

    out["facts"] = _extract_list(text, "关键事件")
    out["relations"] = _extract_list(text, r"角色关系[/／]?立场|角色关系|关系[/／]?立场")

    return out


def _extract_field(text: str, pat_mid: str, pat_end: str) -> Optional[str]:
    """提取单行字段值，先用"到下一字段为止"匹配，不行再用"到行尾"。"""
    m = re.search(pat_mid, text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(pat_end, text, re.DOTALL)
    if m:
        return m.group(1)
    return None


def _extract_list(text: str, header_pat: str) -> list:
    """提取列表型字段（- 或 • 或编号开头的逐行）。

    header_pat: 匹配表头（如"关键事件"或"角色关系/立场"）
    """
    # 找到表头所在行，取其后到下一个表头或文末的内容
    m = re.search(rf"({header_pat})[^:：]*[:：]\s*\n(.+?)(?=\n\s*(前情提要|时间线|关键事件|角色关系|关系[/／]?立场))", text, re.DOTALL)
    if not m:
        m = re.search(rf"({header_pat})[^:：]*[:：]\s*\n(.+?)$", text, re.DOTALL)
    if not m:
        return []
    block = m.group(2)
    items = []
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        # 去掉列表标记
        line = re.sub(r"^[-*•]\s*", "", line)
        line = re.sub(r"^\d+[.、)]\s*", "", line)
        line = line.strip()
        if line and line not in ("无", "无。", "—", "-"):
            items.append(line)
    return items


def _clean_fallback_text(text: str) -> str:
    """兜底：把整段当 text，去掉明显的模板表头行，让面板不显得杂乱。"""
    lines = []
    for line in text.splitlines():
        s = line.strip()
        # 去掉空行和孤立模板表头
        if not s:
            continue
        if re.match(r"^(前情提要|时间线|关键事件|角色关系|关系[/／]?立场)[^:：]*[:：]?\s*$", s):
            continue
        lines.append(s)
    cleaned = " ".join(lines) if lines else text
    return cleaned
