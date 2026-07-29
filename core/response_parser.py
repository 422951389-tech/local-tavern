"""AI 回复解析器

把 LLM 输出的纯文本拆分成结构化数据：
- 场景元数据
- 每个角色的状态卡
- 行动建议

不解析失败时，返回原始 content，由前端展示原文
"""
import re
from typing import Optional


_TASK_LABEL = (
    r"🎯(?:\ufe0f)?[ \t]*"
    r"(?:\[?[ \t]*主线[ \t]*\]?[ \t]*)?"
    r"(?:任务(?:名称)?|主线任务)[ \t]*[:：][ \t]*"
)
_CURRENT_SCENE_LABEL = r"📌(?:\ufe0f)?[ \t]*当前场景[ \t]*[:：][ \t]*"
_NEXT_GOAL_LABEL = r"➡(?:\ufe0f)?[ \t]*下一目标[ \t]*[:：][ \t]*"
_USER_LABEL = r"👤(?:\ufe0f)?[ \t]*(?:用户|玩家)(?:[ \t]*[:：][ \t]*|[ \t]+)"
_INNER_LABEL = (
    r"💭(?:\ufe0f)?[ \t]*(?:内心(?:想法|活动)?|想法)[ \t]*[:：][ \t]*"
)
_OUTFIT_LABEL = r"👗(?:\ufe0f)?[ \t]*穿着[ \t]*[:：][ \t]*"
_POSTURE_LABEL = (
    r"🧍(?:\ufe0f)?(?:\u200d[♀♂](?:\ufe0f)?)?[ \t]*"
    r"(?:当前)?姿势[ \t]*[:：][ \t]*"
)
_DIALOGUE_LABEL = r"💬(?:\ufe0f)?[ \t]*对白[ \t]*[:：][ \t]*"
_NARRATION_LABEL = (
    r"(?:📖(?:\ufe0f)?[ \t]*|#{1,6}[ \t]+)"
    r"场景旁白(?:[ \t]*[:：][ \t]*)?"
)
_SUGGESTIONS_LABEL = r"💡(?:\ufe0f)?[ \t]*行动建议(?:[ \t]*[:：][ \t]*)?"

# 角色标签只有在同一卡片中很快出现好感度标记时才算协议边界。这样对白里的普通
# 🎭 或其他 emoji 不会被误当成新卡片；同时保留旧模型把 💝 放到下一行的退化格式。
_ROLE_HEADER_START = (
    r"🎭(?:\ufe0f)?"
    r"(?=(?:(?!🎭|📖(?:\ufe0f)?[ \t]*场景旁白|"
    r"💡(?:\ufe0f)?[ \t]*行动建议)[\s\S]){1,160}?💝)"
)

_PROTOCOL_BOUNDARY_PATTERN = "(?:" + "|".join((
    _TASK_LABEL,
    _CURRENT_SCENE_LABEL,
    _NEXT_GOAL_LABEL,
    _USER_LABEL,
    _ROLE_HEADER_START,
    _INNER_LABEL,
    _OUTFIT_LABEL,
    _POSTURE_LABEL,
    _DIALOGUE_LABEL,
    _NARRATION_LABEL,
    _SUGGESTIONS_LABEL,
)) + ")"
_PROTOCOL_BOUNDARY_RE = re.compile(_PROTOCOL_BOUNDARY_PATTERN)
_SECTION_END = rf"(?=^[ \t]*{_PROTOCOL_BOUNDARY_PATTERN}|\Z)"

_NARRATION_RE = re.compile(
    rf"^[ \t]*(?:{_NARRATION_LABEL})(?P<body>.*?)"
    rf"(?=^[ \t]*(?:{_SUGGESTIONS_LABEL}))",
    re.MULTILINE | re.DOTALL,
)


def _normalize_protocol_boundaries(text: str) -> str:
    """只给已知协议标签补物理行边界，不改动字段内容。"""
    positions = {match.start() for match in _PROTOCOL_BOUNDARY_RE.finditer(text)}

    # 📍 本身没有固定文字标签，只允许最早的场景标记成为结构边界。若它位于角色、
    # 旁白或建议之后，则视为普通正文 emoji，不参与拆分。
    location = re.search(r"📍(?:\ufe0f)?", text)
    if location and (not positions or location.start() < min(positions)):
        positions.add(location.start())

    if not positions:
        return text

    chunks: list[str] = []
    cursor = 0
    for position in sorted(positions):
        chunks.append(text[cursor:position])
        line_start = text.rfind("\n", 0, position) + 1
        if text[line_start:position].strip():
            chunks.append("\n")
        cursor = position
    chunks.append(text[cursor:])
    return "".join(chunks)


def _extract_field(block: str, label_pattern: str) -> str:
    match = re.search(
        rf"^[ \t]*(?:{label_pattern})(?P<value>.*?){_SECTION_END}",
        block,
        re.MULTILINE | re.DOTALL,
    )
    return match.group("value").strip() if match else ""


def _strip_edge_separator(value: str) -> str:
    return re.sub(r"[ \t]*[|｜][ \t]*$", "", value.strip())


def _split_suggestions(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []

    # 单行截图式输出常把 1/2/3 三项压在一起。只识别行首或空白后的明确列表标记，
    # 不按句中数字、标点或 emoji 任意拆分。
    marker = re.compile(r"(?:^|(?<=\s))(?:[-*•][ \t]+|\d{1,2}[.、)][ \t]*)")
    matches = list(marker.finditer(text))
    if matches:
        items = []
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            item = text[start:end].strip()
            if item:
                items.append(item)
        if items:
            return items[:5]

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [re.sub(r"^[-*•]\s*", "", line) for line in lines]
    lines = [re.sub(r"^\d+[.、)]\s*", "", line) for line in lines]
    return [line for line in lines if line][:5]


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

    text = _normalize_protocol_boundaries(text.strip())

    # 1. 解析场景元数据条（📍 开头到第一个 🎭 之前）
    scene_match = re.search(
        rf"^[ \t]*📍(?:\ufe0f)?.*?"
        rf"(?=^[ \t]*(?:{_ROLE_HEADER_START}|{_NARRATION_LABEL}|{_SUGGESTIONS_LABEL})|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if scene_match:
        scene_block = scene_match.group(0).strip()
        result["scene_meta"] = _parse_scene_meta(scene_block)

    # 2. 解析每个角色的状态卡。只有后方存在 💝 的 🎭 才会成为角色边界。
    role_starts = [match.start() for match in re.finditer(_ROLE_HEADER_START, text)]
    terminal_starts = [
        match.start()
        for match in re.finditer(
            rf"^[ \t]*(?:{_NARRATION_LABEL}|{_SUGGESTIONS_LABEL})",
            text,
            re.MULTILINE,
        )
    ]
    for index, start in enumerate(role_starts):
        candidates = role_starts[index + 1:index + 2]
        candidates.extend(position for position in terminal_starts if position > start)
        end = min(candidates) if candidates else len(text)
        block = text[start:end]
        char_data = _parse_character_block(block)
        if char_data:
            result["characters"].append(char_data)

    # 3. 解析显式场景旁白。旁白范围必须由行动建议标签闭合，避免吞掉尾部文本。
    narration_match = _NARRATION_RE.search(text)
    if narration_match:
        result["narration"] = narration_match.group("body").strip()

    # 4. 解析行动建议（💡 行动建议 之后的内容）
    result["suggestions"] = _split_suggestions(
        _extract_field(text, _SUGGESTIONS_LABEL)
    )

    return result


def build_response_presentation(
    parsed: dict,
    *,
    previous_affinities: list[object] | None = None,
    character_moods: list[object] | None = None,
    scene_changes: list[dict] | None = None,
) -> dict:
    """生成可持久化的精简展示快照，不重复保存 raw 原文。"""
    source = parsed if isinstance(parsed, dict) else {}
    previous_values = previous_affinities or []
    mood_values = character_moods or []
    scene_source = source.get("scene_meta")
    scene_meta = None
    if isinstance(scene_source, dict):
        scene_meta = {
            key: value
            for key in (
                "location",
                "time_weather",
                "main_quest",
                "current_scene",
                "next_goal",
                "user_line",
            )
            if isinstance((value := scene_source.get(key)), str)
        }

    characters = []
    for index, character in enumerate(source.get("characters", [])):
        if not isinstance(character, dict):
            continue
        item = {
            key: value
            for key in (
                "name",
                "inner_thought",
                "outfit",
                "posture",
                "dialogue",
                "expected_effect",
            )
            if isinstance((value := character.get(key)), str)
        }
        affinity = character.get("affinity")
        if isinstance(affinity, (int, float)) and not isinstance(affinity, bool):
            item["affinity"] = max(0, min(100, round(affinity)))
        previous = previous_values[index] if index < len(previous_values) else None
        if isinstance(previous, (int, float)) and not isinstance(previous, bool):
            item["previous_affinity"] = max(0, min(100, round(previous)))
        mood = mood_values[index] if index < len(mood_values) else None
        if isinstance(mood, str) and mood.strip():
            item["mood"] = mood.strip()
        characters.append(item)

    normalized_scene_changes = []
    for change in scene_changes or []:
        if not isinstance(change, dict):
            continue
        key = change.get("key")
        value = change.get("value")
        if key in {"location", "time", "weather"} and isinstance(value, str):
            normalized_scene_changes.append({"key": key, "value": value})

    return {
        "schema_version": 1,
        "scene_meta": scene_meta,
        "characters": characters,
        "narration": (
            source.get("narration")
            if isinstance(source.get("narration"), str)
            else ""
        ),
        "suggestions": [
            value
            for value in source.get("suggestions", [])[:5]
            if isinstance(value, str)
        ] if isinstance(source.get("suggestions"), list) else [],
        "warnings": [
            value
            for value in source.get("warnings", [])[:50]
            if isinstance(value, str)
        ] if isinstance(source.get("warnings"), list) else [],
        "scene_changes": normalized_scene_changes[:3],
    }


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

    # 📍 xxx | ⏱️ xxx。时间标记是场景条内部字段，不作为全局协议边界。
    loc_match = re.search(
        rf"📍(?:\ufe0f)?[ \t]*(?:(?:场景|地点)[ \t]*[:：][ \t]*)?"
        rf"(?P<value>.*?)(?=[ \t]*[|｜]?[ \t]*⏱(?:\ufe0f)?|"
        rf"^[ \t]*{_PROTOCOL_BOUNDARY_PATTERN}|\Z)",
        block,
        re.MULTILINE | re.DOTALL,
    )
    if loc_match:
        meta["location"] = _strip_edge_separator(loc_match.group("value"))

    time_match = re.search(
        rf"⏱(?:\ufe0f)?[ \t]*"
        rf"(?:(?:时间(?:[ \t]*/[ \t]*天气)?|时间天气|天气)[ \t]*[:：][ \t]*)?"
        rf"(?P<value>.*?)(?=^[ \t]*{_PROTOCOL_BOUNDARY_PATTERN}|\Z)",
        block,
        re.MULTILINE | re.DOTALL,
    )
    if time_match:
        meta["time_weather"] = _strip_edge_separator(time_match.group("value"))

    meta["main_quest"] = _extract_field(block, _TASK_LABEL)
    meta["current_scene"] = _extract_field(block, _CURRENT_SCENE_LABEL)
    meta["next_goal"] = _extract_field(block, _NEXT_GOAL_LABEL)
    meta["user_line"] = _extract_field(block, _USER_LABEL)

    return meta


def _parse_character_block(block: str) -> Optional[dict]:
    """解析单个角色的状态卡"""
    name_match = re.search(
        r"🎭(?:\ufe0f)?[ \t]*"
        r"(?:(?:出场)?角色(?:名)?(?:[ \t]*[:：][ \t]*|[ \t]+))?"
        r"(?P<name>.*?)"
        r"(?=[ \t]*(?:[|｜:：\-—][ \t]*)?(?:\r?\n[ \t]*)?💝|\r?$)",
        block,
        re.MULTILINE,
    )
    affinity_match = re.search(
        r"💝(?:\ufe0f)?[ \t]*(?:好感度[ \t]*[:：]?[ \t]*)?"
        r"[^\d+\-\r\n]{0,80}(?P<affinity>[+-]?\d+)[ \t]*%?",
        block,
    )
    if not name_match or not affinity_match:
        return None
    name = name_match.group("name").strip().rstrip("|｜:：-— ")
    affinity = int(affinity_match.group("affinity"))

    inner_thought = _extract_field(block, _INNER_LABEL)
    outfit_str = _extract_field(block, _OUTFIT_LABEL)
    posture_str = _extract_field(block, _POSTURE_LABEL)

    dialogue_str = _extract_field(block, _DIALOGUE_LABEL)
    effect_str = ""
    effect_match = re.search(
        r"[ \t]*[（(][ \t]*预期影响[ \t]*[:：][ \t]*"
        r"(?P<effect>.*?)[ \t]*[）)][ \t]*$",
        dialogue_str,
        re.DOTALL,
    )
    if effect_match:
        effect_str = effect_match.group("effect").strip()
        dialogue_str = dialogue_str[:effect_match.start()].strip()

    quote_pairs = (("\"", "\""), ("“", "”"), ("'", "'"), ("‘", "’"))
    for opening, closing in quote_pairs:
        if dialogue_str.startswith(opening) and dialogue_str.endswith(closing):
            dialogue_str = dialogue_str[len(opening):-len(closing)].strip()
            break

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
