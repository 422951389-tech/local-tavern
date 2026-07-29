'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const LEGACY_RESPONSE = `📍 琉璃宫·花园 | ⏱️ 午后 / 晴朗
🎯 [主线] 任务名称: 初遇·命运交汇
📌 当前场景: 花园石径上，阳光洒落。
➡️ 下一目标: 等待用户选择行动方向
👤 用户: 晴川 | 🆔 身份: 游历者
🎭 炽霞 | 💝 ████░░░░░░ 60%
💭 内心想法: 要不要逗逗他？
👗 穿着: 红白轻纱长裙
🧍 当前姿势: 坐在石凳上
💬 对白: "你就是传说中的游历者吗？"（预期影响: 拉近关系）
### 场景旁白
阳光透过树影洒下斑驳光影。
💡 行动建议
- 回应炽霞
- 观察凉亭`;

test('旧存档原文可只读解析为剧情、角色、好感度和折叠细节数据', async () => {
    const { parseLegacyAssistantResponse, presentationForMessage } = await import(
        '../web/response-presentation.mjs'
    );
    const parsed = parseLegacyAssistantResponse(LEGACY_RESPONSE);
    assert.deepEqual(parsed.scene_meta, {
        location: '琉璃宫·花园',
        time_weather: '午后 / 晴朗',
        main_quest: '初遇·命运交汇',
        current_scene: '花园石径上，阳光洒落。',
        next_goal: '等待用户选择行动方向',
        user_line: '晴川 | 🆔 身份: 游历者',
    });
    assert.deepEqual(parsed.characters, [{
        name: '炽霞',
        affinity: 60,
        mood: '',
        inner_thought: '要不要逗逗他？',
        outfit: '红白轻纱长裙',
        posture: '坐在石凳上',
        dialogue: '你就是传说中的游历者吗？',
        expected_effect: '拉近关系',
    }]);
    assert.equal(parsed.narration, '阳光透过树影洒下斑驳光影。');
    assert.deepEqual(parsed.suggestions, ['回应炽霞', '观察凉亭']);
    assert.deepEqual(presentationForMessage({ content: LEGACY_RESPONSE }), parsed);
});

test('持久化展示快照优先于旧原文且不会携带 raw 副本', async () => {
    const { presentationForMessage } = await import('../web/response-presentation.mjs');
    const presentation = presentationForMessage({
        content: LEGACY_RESPONSE,
        presentation: {
            schema_version: 1,
            scene_meta: { main_quest: '持久化主线', next_goal: '刷新后仍保留' },
            characters: [{
                name: '秧秧', affinity: 50, previous_affinity: 40, mood: '平静', dialogue: '已经保存。',
            }],
            narration: '持久化旁白',
            suggestions: ['继续'],
            warnings: [],
            scene_changes: [{ key: 'location', value: '新地点' }],
        },
    });
    assert.equal(presentation.scene_meta.main_quest, '持久化主线');
    assert.equal(presentation.characters[0].name, '秧秧');
    assert.equal(presentation.characters[0].previous_affinity, 40);
    assert.equal(presentation.characters[0].mood, '平静');
    assert.deepEqual(presentation.scene_changes, [
        { key: 'location', label: '地点', value: '新地点' },
    ]);
    assert.equal(Object.hasOwn(presentation, 'raw'), false);
});

test('同版本事件优先复用持久化的本轮好感变化、心情和场景变化', async () => {
    const { captureResponsePresentationBaseline } = await import(
        '../web/response-presentation.mjs'
    );
    const baseline = captureResponsePresentationBaseline({
        revision: 7,
        characters_state: {
            test_character: { name: '测试角色', affinity: 50, mood: '平静' },
        },
        message_history: [{
            role: 'assistant',
            turn_id: 'turn-7',
            content: LEGACY_RESPONSE,
            presentation: {
                schema_version: 1,
                scene_meta: { main_quest: '持久化主线' },
                characters: [{
                    name: '测试角色',
                    affinity: 50,
                    previous_affinity: 40,
                    mood: '平静',
                    dialogue: '刷新后仍保留。',
                }],
                scene_changes: [{ key: 'location', value: '新地点' }],
            },
        }],
    }, {
        revision: 7,
        session_delta: {
            characters_state: {
                test_character: { name: '测试角色', affinity: 50, mood: '平静' },
            },
        },
    }, 'turn-7');

    assert.deepEqual(baseline.characters['persisted-0'], {
        id: '',
        name: '测试角色',
        currentAffinity: 50,
        previousAffinity: 40,
        mood: '平静',
    });
    assert.deepEqual(baseline.sceneChanges, [
        { key: 'location', label: '地点', value: '新地点' },
    ]);
});

test('普通非协议回复保持原文降级，不误生成空模块', async () => {
    const { presentationForMessage } = await import('../web/response-presentation.mjs');
    assert.equal(presentationForMessage({ content: '只是一段普通回复。' }), null);
});

test('空、损坏或未知版本展示快照均安全回退到旧原文解析', async () => {
    const { presentationForMessage } = await import('../web/response-presentation.mjs');
    const invalidPresentations = [
        null,
        '',
        {},
        { schema_version: 1, scene_meta: null, characters: [] },
        { schema_version: 2, scene_meta: { main_quest: '不得采用' } },
        { schema_version: 1, characters: ['损坏角色'] },
    ];

    for (const presentation of invalidPresentations) {
        const restored = presentationForMessage({
            content: LEGACY_RESPONSE,
            presentation,
        });
        assert.equal(restored.scene_meta.main_quest, '初遇·命运交汇');
        assert.equal(restored.characters[0].affinity, 60);
    }
});
