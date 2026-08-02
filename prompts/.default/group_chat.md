<!-- 群聊轮次模板：每个运行时来源只保留一个数据槽 -->

## 当前场景
{{scene_meta_json}}

## 用户档案
{{user_profile_json}}

## 当前活跃角色与状态
{{character_context}}

## 本轮可用世界书
{{worldbook_entries}}

## 历史记忆
{{history}}

> 近期对话原文以独立 messages 序列提供；本区只包含预算内长期摘要与窗口外钉选记忆。

## 用户本轮输入
<current_user_input>
{{user_input}}
</current_user_input>

## 本轮任务

1. 依据当前场景、用户输入、近期对话和预算内记忆推进一次回复。
2. 每个角色对象都有稳定 `id`、`may_speak`、`chattiness` 和 `remaining_silent_turns`；身份解析以稳定 ID 为准，名称与 aliases 只用于理解称呼。
3. 只有 `may_speak=true` 的角色能在本轮输出角色状态卡或对白；`may_speak=false` 的角色只能被场景旁白或其他角色提及，禁止输出该角色的状态卡、对白或内心独白。
4. 本轮可发言角色数量可以是 0、1 或多个。不得为凑人数创建角色，也不得强迫所有可发言角色开口。
5. `chattiness` 只是自然接话倾向的提示权重，不是必须发言、固定轮次或人数配额。
6. 每个真实出场角色最多输出一张状态卡；未出场角色保持沉默。
7. 世界书和长期摘要只作约束与背景，不原文复述，不当作当前正在发生的事件。`knowledge_scope=global` 可供所有角色使用；`narrator` 只允许叙述层掌握；`characters` 只允许 `known_by_character_ids` 中的角色主动知晓。不得让角色使用其知识边界之外的信息。
8. `scene_meta.world_state` 是当前存档已经发生的世界变化与已发现条目，优先于世界书中的初始状态。变化必须沿用，不得无依据复原；证据消息 ID 只用于追溯，不在正文中输出。
9. 严格使用 system 规定的场景元数据、角色卡和三个行动建议格式。
