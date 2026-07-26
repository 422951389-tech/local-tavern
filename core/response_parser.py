"""AI 回复解析器

把 LLM 输出的纯文本拆分成结构化数据：
- 场景元数据
- 每个角色的状态卡
- 行动建议

不解析失败时，返回原始 content，由前端展示原文
"""
import re
from typing import Optional


_NARRATION_RE = re.compile(
    r"^[ \t]*📖[ \t]*场景旁白"
    r"(?:[ \t]*[:：][ \t]*(?P<inline>[^\r\n]*))?"
    r"[ \t]*\r?\n"
    r"(?P<body>.*?)"
    r"(?=^[ \t]*💡[ \t]*行动建议[ \t]*\r?$)",
    re.MULTILINE | re.DOTALL,
)


def parse_response(raw: str) -> dict:
    """解析 AI 回复

    返回:
    {
        "scene_meta": { ... } | None,
        "characters": [
            { "name", "affinity", "inner_thought", "outfit", "posture", "dialogue", "expected_effect" },
            ...
        ],
        "narration": "场景旁白文本" | "",
        "suggestions": ["建议1", "建议2", "建议3"],
        "roleplay_warnings": [],
        "raw": 原始文本
    }
    """
    result = {
        "scene_meta": None,
        "characters": [],
        "narration": "",
        "suggestions": [],
        "roleplay_warnings": [],
        "raw": raw,
    }

    if not raw or not raw.strip():
        return result

    # 有些模型会把整段输出包在代码块里，先剥离
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)

    text = text.strip()

    # 1. 解析场景元数据条（📍 开头到第一个 🎭 之前）
    scene_match = re.search(
        r"📍.*?(?=🎭|📖|💡|$)",
        text,
        re.DOTALL,
    )
    if scene_match:
        scene_block = scene_match.group(0).strip()
        result["scene_meta"] = _parse_scene_meta(scene_block)

    # 2. 解析每个角色的状态卡（🎭 ... 💬 ...）
    # 用 🎭 作为分隔符切分
    char_blocks = re.split(r"(?=🎭\s)", text)
    for block in char_blocks:
        if "🎭" not in block:
            continue
        char_data = _parse_character_block(block)
        if char_data:
            result["characters"].append(char_data)

    # 3. 解析显式场景旁白。旁白范围必须由行动建议标签闭合，避免吞掉尾部文本。
    narration_match = _NARRATION_RE.search(text)
    if narration_match:
        narration_parts = [
            part.strip()
            for part in (
                narration_match.group("inline") or "",
                narration_match.group("body") or "",
            )
            if part.strip()
        ]
        result["narration"] = "\n".join(narration_parts)

    # 4. 解析行动建议（💡 行动建议 之后的内容）
    sug_match = re.search(
        r"💡\s*行动建议\s*\n(.*?)(?=$|```)",
        text,
        re.DOTALL,
    )
    if sug_match:
        sug_text = sug_match.group(1).strip()
        # 按行分割，过滤空行
        lines = [line.strip() for line in sug_text.split("\n") if line.strip()]
        # 去掉 markdown 列表标记
        lines = [re.sub(r"^[-*•]\s*", "", line) for line in lines]
        # 去掉编号
        lines = [re.sub(r"^\d+[.、)]\s*", "", line) for line in lines]
        result["suggestions"] = lines[:5]  # 最多 5 个

    return result


def _parse_scene_meta(block: str) -> dict:
    """解析场景元数据条"""
    meta = {
        "location": "",
        "time_weather": "",
        "main_quest": "",
        "current_scene": "",
        "next_goal": "",
        "user_line": "",
    }

    # 📍 xxx | ⏱️ xxx
    loc_match = re.search(r"📍\s*([^|\n]+)", block)
    if loc_match:
        meta["location"] = loc_match.group(1).strip()

    time_match = re.search(r"⏱️\s*([^|\n]+)", block)
    if time_match:
        meta["time_weather"] = time_match.group(1).strip()

    quest_match = re.search(r"🎯\s*\[?主线\]?\s*任务名称[:：]\s*([^\n]+)", block)
    if quest_match:
        meta["main_quest"] = quest_match.group(1).strip()

    scene_match = re.search(r"📌\s*当前场景[:：]\s*([^\n]+)", block)
    if scene_match:
        meta["current_scene"] = scene_match.group(1).strip()

    goal_match = re.search(r"➡️\s*下一目标[:：]\s*([^\n]+)", block)
    if goal_match:
        meta["next_goal"] = goal_match.group(1).strip()

    user_match = re.search(r"👤\s*([^\n]+)", block)
    if user_match:
        meta["user_line"] = user_match.group(1).strip()

    return meta


def _parse_character_block(block: str) -> Optional[dict]:
    """解析单个角色的状态卡"""
    # 标准化：把换行拆开的格式压缩为单行
    # 🎭 老李\n💝 ██ 60% → 🎭 老李 | 💝 ██ 60%
    block_normalized = re.sub(r"\n\s*💝", " | 💝", block)
    block_normalized = re.sub(r"\n\s*💭", "\n💭", block_normalized)
    block_normalized = re.sub(r"\n\s*👗", "\n👗", block_normalized)
    block_normalized = re.sub(r"\n\s*🧍", "\n🧍", block_normalized)
    block_normalized = re.sub(r"\n\s*💬", "\n💬", block_normalized)

    # 角色名和好感度：🎭 xxx | 💝 ...
    header_match = re.search(r"🎭\s*([^|\n]+?)\s*\|\s*💝\s*([█░▏▎▍▌▋▊▉\s]*?[█░▏▎▍▌▋▊▉]+\s*)([+-]?\d+)%?", block_normalized)
    if not header_match:
        # 退化：只匹配 🎭 xxx 后面某处有 💝 和数字
        fallback_name = re.search(r"🎭\s*(\S[^\n|]*?)\s*$", block_normalized, re.MULTILINE)
        fallback_aff = re.search(r"💝[^\d+\-]*?([+-]?\d+)%?", block_normalized)
        if not fallback_name or not fallback_aff:
            return None
        name = fallback_name.group(1).strip()
        affinity = int(fallback_aff.group(1)) if fallback_aff.group(1) else 0
    else:
        name = header_match.group(1).strip()
        affinity = int(header_match.group(3)) if header_match.group(3) else 0

    # 内心
    inner = re.search(r"💭\s*内心想法[:：]\s*([^\n]+(?:\n(?!👗)[^\n]+)*)", block)
    inner_thought = inner.group(1).strip() if inner else ""

    # 穿着
    outfit = re.search(r"👗\s*穿着[:：]\s*([^\n]+)", block)
    outfit_str = outfit.group(1).strip() if outfit else ""

    # 姿势
    posture = re.search(r"🧍\s*当前姿势[:：]\s*([^\n]+)", block)
    posture_str = posture.group(1).strip() if posture else ""

    # 对白 + 预期影响
    dialogue = re.search(r'💬\s*对白[:：]\s*"([^"]+)"(?:\s*\(预期影响[:：]\s*([^\)]+)\))?', block)
    if dialogue:
        dialogue_str = dialogue.group(1).strip()
        effect_str = dialogue.group(2).strip() if dialogue.group(2) else ""
    else:
        # 退化：💬 对白：xxx（无引号）
        dialogue2 = re.search(r"💬\s*对白[:：]\s*([^\n]+)", block)
        dialogue_str = dialogue2.group(1).strip() if dialogue2 else ""
        effect_str = ""

    if not name:
        return None
    # D4：name 长度上限防御（防止幻觉输出垃圾）
    if len(name) > 50:
        name = name[:50]
    affinity = max(0, min(100, affinity))  # 好感度统一限制在 [0, 100]

    return {
        "name": name,
        "affinity": affinity,
        "inner_thought": inner_thought,
        "outfit": outfit_str,
        "posture": posture_str,
        "dialogue": dialogue_str,
        "expected_effect": effect_str,
    }


def check_voice_confusion(parsed: dict, characters: list[dict]) -> list[str]:
    """
    检查角色对白是否混入了其他角色的标志性台词/口癖。
    返回警告文案列表（为空表示未发现串味）。
    """
    warnings = []
    if not parsed or not parsed.get("characters"):
        return warnings

    # 构建每个角色的"声音指纹"
    fingerprints = {}
    for c in characters:
        cid = c.get("id") or c.get("name")
        if not cid:
            continue
        phrases = []
        if c.get("catchphrases"):
            phrases.extend([str(p).strip() for p in c["catchphrases"] if str(p).strip()])
        if c.get("speaking_style"):
            style = str(c["speaking_style"])
            # 取整行作为指纹
            for line in style.splitlines():
                line = line.strip(" ，。、：").strip()
                if len(line) >= 4:
                    phrases.append(line)
            # 提取引号内的标志性表达（单/双引号）
            for quote in re.findall(r"['\"]([^'\"]{2,})['\"]", style):
                quote = quote.strip(" ，。、：")
                if quote and quote not in phrases:
                    phrases.append(quote)
            # 提取常见自称、称呼等短词（如"老夫"、"在下"）
            for word in re.findall(r"[老在鄙奴妾俺咱你您][夫在下鄙人奴家妾身俺咱娘爷哥姐弟妹子]+", style):
                if word and len(word) >= 2 and word not in phrases:
                    phrases.append(word)
        if phrases:
            fingerprints[cid] = {
                "name": c.get("name", cid),
                "phrases": phrases,
            }

    for c in parsed["characters"]:
        speaker_name = c.get("name", "")
        # 同时检测对白和内心想法
        text_to_check = " ".join([c.get("dialogue", ""), c.get("inner_thought", "")])
        if not speaker_name or not text_to_check.strip():
            continue
        own_ids = {cid for cid, info in fingerprints.items() if info["name"] == speaker_name}
        for cid, info in fingerprints.items():
            if cid in own_ids:
                continue
            for phrase in info["phrases"]:
                if len(phrase) <= 2:
                    # 短指纹要求词边界，避免"在下"匹配"在下来也"
                    idx = text_to_check.find(phrase)
                    if idx != -1:
                        before_ok = idx == 0 or not text_to_check[idx - 1].isalnum()
                        after_ok = idx + len(phrase) == len(text_to_check) or not text_to_check[idx + len(phrase)].isalnum()
                        if before_ok and after_ok:
                            warnings.append(
                                f"{speaker_name} 的台词/内心中出现了 {info['name']} 的标志性表达：\"{phrase}\""
                            )
                            break
                elif phrase in text_to_check:
                    warnings.append(
                        f"{speaker_name} 的台词/内心中出现了 {info['name']} 的标志性表达：\"{phrase}\""
                    )
                    break
    return warnings
