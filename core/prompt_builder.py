"""Prompt 构建器 — 组装发给 Ollama 的 messages 列表

读取 prompts/ 下的 Markdown 模板，做变量替换
"""
import json
from pathlib import Path
from typing import Optional

from core.config import MAX_TURNS_IN_PROMPT, PROMPTS_DIR

# PROMPTS_DIR 已从 core.config 导入（line 9），此行已删除（P2 死代码清理）



def _read_template(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def build_messages(
    user_input: str,
    characters: list[dict],
    characters_state: dict,
    scene_meta: dict,
    user_profile: dict,
    worldbook_entries: list[dict],
    history: list[dict],
    summaries: Optional[list] = None,
) -> list[dict]:
    """构建完整的 messages 列表（system + history + user）

    返回：[{"role": "system", ...}, {"role": "user", ...}, ...]
    """

    # ===== system 部分 =====
    system_template = _read_template("system.md")
    system_content = system_template

    # 注入角色列表（含声音指纹，便于模型区分）
    char_lines = []
    for c in characters:
        name = c.get("name", c.get("id", "未知"))
        cid = c.get("id", "")
        tagline = c.get("tagline", "")
        voice = c.get("voice_tone", "")
        style = c.get("speaking_style", "")
        catch = ", ".join(c.get("catchphrases", []))
        char_lines.append(f"- {name} (id: {cid})" + (f"：{tagline}" if tagline else ""))
        if voice:
            char_lines.append(f"  - 语气: {voice}")
        if style:
            char_lines.append(f"  - 说话风格: {style}")
        if catch:
            char_lines.append(f"  - 标志性表达: {catch}")
        custom = c.get("custom", {})
        if custom and isinstance(custom, dict):
            for ck, cv in custom.items():
                if cv:
                    char_lines.append(f"  - {ck}: {cv}")
    char_summary = "\n".join(char_lines)
    system_content = system_content.replace("{{character_list}}", char_summary or "（无）")

    # 注入用户档案
    user_str = json.dumps(user_profile, ensure_ascii=False, indent=2)
    system_content = system_content.replace("{{user_profile}}", user_str or "{}")

    # 注入世界书条目
    if worldbook_entries:
        wb_str = "\n\n---\n\n".join(
            f"### {e.get('id', 'entry')}\n{e.get('content', '')}"
            for e in worldbook_entries
        )
    else:
        wb_str = "（无匹配条目）"
    system_content = system_content.replace("{{worldbook_entries}}", wb_str)

    # 注入格式示例（用真实变量名替换占位符本身，避免循环）
    format_example = _build_format_example(characters, scene_meta, user_profile)
    system_content = system_content.replace("{{format_example}}", format_example)

    # ===== group_chat 部分（作为 user 角色追加） =====
    group_template = _read_template("group_chat.md")

    group_content = group_template

    # 场景元数据
    scene_str = json.dumps(scene_meta, ensure_ascii=False, indent=2)
    group_content = group_content.replace("{{scene_meta_json}}", scene_str)

    # 角色状态
    char_state_str = json.dumps(characters_state, ensure_ascii=False, indent=2)
    group_content = group_content.replace("{{characters_state_json}}", char_state_str)

    # 历史
    if history:
        history_lines = []
        # 长期记忆：已压缩的剧情梗概（按时间正序，每段独立）
        if summaries:
            summary_lines = []
            for s in summaries:
                if not isinstance(s, dict):
                    continue
                text = s.get("text", "") or ""
                if not text:
                    continue
                ts = (s.get("created_at") or "")[:10]
                block = f"【前情提要({ts})】{text}"
                for f in (s.get("facts") if isinstance(s.get("facts"), list) else [])[:5]:
                    block += f"\n  · 关键事件: {f}"
                if s.get("relations") and isinstance(s.get("relations"), list):
                    block += "\n  · 角色关系: " + "；".join(s["relations"][:5])
                summary_lines.append(block)
            if summary_lines:
                history_lines.append("===== 长期记忆（早期剧情梗概，已被压缩）=====")
                history_lines.append("⚠️ 以下剧情梗概只作背景理解，不要原文复述，更不要当作当前正在发生的事。本轮对话已经推进到这些梗概之后。")
                history_lines.append("\n---\n".join(summary_lines))
        # 📌常驻：所有 pinned 消息（哪怕已被截轮、不在最近20条内）都常驻 prompt
        all_pinned = [h for h in history if h.get("pinned")]
        if all_pinned:
            pinned_block = ["===== 📌常驻记忆（用户钉选，须始终记牢）====="]
            for h in all_pinned:
                role = h.get("role", "user")
                pinned_block.append(f"[{role}|📌常驻]: {h.get('content','')}")
            history_lines.append("\n".join(pinned_block))
        # 近期历史不在文本块注入（避免与下方 messages 序列双重注入导致权重翻倍）。
        # 近期对话走 messages 序列的单一注入（见函数末尾 history[-N:]），文本块只承载长期摘要 + 📌常驻。
        if history_lines:
            history_str = "\n\n".join(history_lines)
        else:
            history_str = "（无长期记忆/常驻，这是第一轮或近期对话走 messages 序列）"
    else:
        history_str = "（无历史，这是第一轮对话）"
    group_content = group_content.replace("{{history}}", history_str)

    # 先替换系统占位符，最后替换用户输入（防止用户消息中的系统占位符被二次替换）
    group_content = group_content.replace("{{user_profile}}", user_str or "{}")
    group_content = group_content.replace("{{character_list}}", char_summary or "（无）")
    group_content = group_content.replace("{{worldbook_entries}}", wb_str)

    # 用户输入 — 放在系统占位符之后替换，防止注入
    group_content = group_content.replace("{{user_input}}", user_input)

    # ===== 拼装 messages =====
    messages = [{"role": "system", "content": system_content}]

    # 历史消息（之前已存在）—— recent 单一注入源：messages 序列
    # MAX_TURNS_IN_PROMPT * 2 = 每轮 user+assistant 两条，给模型结构化的近期对话
    recent_n = MAX_TURNS_IN_PROMPT * 2
    for h in history[-recent_n:]:
        role = h.get("role", "user")
        content = h.get("content", "")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": content})

    # 本轮的 group_chat prompt 作为最后的 user 消息
    messages.append({"role": "user", "content": group_content})

    return messages


def _build_format_example(characters: list[dict], scene_meta: dict, user_profile: dict) -> str:
    """生成一个完整格式样例（注入到 system prompt）"""
    sample_char = characters[0] if characters else {"id": "示例", "name": "角色名"}

    scene_meta = scene_meta or {}
    user_profile = user_profile or {}

    return f"""```
📍 {scene_meta.get('location', '某地')} | ⏱️ {scene_meta.get('time', '午后')} / {scene_meta.get('weather', '晴')}

🎯 [主线] 任务名称: {scene_meta.get('main_quest', '示例任务')}
📌 当前场景: {scene_meta.get('current_scene', '示例场景')}
➡️ 下一目标: {scene_meta.get('next_goal', '示例目标')}

👤 用户: {user_profile.get('name', '用户名')} | 🆔 身份: {user_profile.get('identity', '身份')} | 💪 状态: 健康 | ✨ 能力: 无

🎭 {sample_char.get('name', '角色A')} | 💝 ████░░░░░░ 40%

💭 内心想法: 示例内心独白...

👗 穿着: 示例穿着

🧍 当前姿势: 示例姿势

💬 对白: "示例对白。" (预期影响: 示例影响)

🎭 角色B | 💝 ███░░░░░░ 30%

💭 内心想法: ...

👗 穿着: ...

🧍 当前姿势: ...

💬 对白: "..." (预期影响: ...)

💡 行动建议
顺势握住秧秧悬在半空的手，坏笑："既然我不记得了，那你们谁是我的娘子？"
站起身来活动一下身体，冷静地看着天空海："我们快走吧，这里确实不太安全。"
对炽霞的元气表示感谢，并对秧秧温柔一笑："那就拜托你们了，秧秧，还有炽霞。"
```"""