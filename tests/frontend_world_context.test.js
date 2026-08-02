const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

async function loadModule() {
    return import('../web/world-context.mjs');
}

test('世界脉络区分生效设定、发现边界和存档变化', async () => {
    const { buildWorldContextModel } = await loadModule();
    const model = buildWorldContextModel({
        scene_meta: { location: '琉璃宫·花园' },
        world_state: {
            discovered_entry_ids: ['palace'],
            changes: [{
                id: '11111111-1111-4111-8111-111111111111',
                category: 'faction',
                title: '守卫转向',
                detail: '花园守卫协助主角',
                status: 'active',
                related_entry_ids: ['palace'],
                evidence_message_ids: [],
            }],
        },
    }, {
        worldbook_matches: [
            { id: 'palace', title: '琉璃宫', category: 'location', visibility: 'discovered', activated: true, kept: true },
            { id: 'hidden', title: '幕后真相', category: 'secret', visibility: 'hidden', activated: true, kept: true },
            { id: 'public', title: '宫廷礼法', category: 'rule', visibility: 'public', activated: true, kept: false },
        ],
    });

    assert.equal(model.location, '琉璃宫·花园');
    assert.deepEqual(model.active.map(item => item.id), ['palace']);
    assert.deepEqual(model.triggered.map(item => item.id), ['palace', 'public']);
    assert.equal(model.worldState.changes[0].title, '守卫转向');
});

test('世界脉络前端采用 textContent 渲染并接入主界面和消息级标签', () => {
    const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'world-context.mjs'), 'utf8');
    const appSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.mjs'), 'utf8');
    const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

    assert.match(html, /id="world-context-bar"/);
    assert.match(moduleSource, /node\.textContent = content/);
    assert.doesNotMatch(moduleSource, /innerHTML\s*=/);
    assert.match(appSource, /appendMessageWorldEffects/);
    assert.match(appSource, /worldContextController\.showWorkbench/);
    assert.match(appSource, /worldDiscoveries:\s*'\/api\/session\/world-state\/discoveries'/);
    assert.match(appSource, /worldChangeUpdate:/);
    assert.match(moduleSource, /世界设定视图/);
    assert.match(moduleSource, /记录为世界变化/);
    assert.match(moduleSource, /设定关联/);
    assert.match(moduleSource, /evidence_message_ids:\s*draft\.evidenceMessageIds/);
    assert.doesNotMatch(moduleSource, /related_entry_ids:\s*\[\]/);
    assert.doesNotMatch(moduleSource, /evidence_message_ids:\s*\[\]/);
});

test('世界变化前端保留状态流转、关联设定和更新时间', async () => {
    const { normalizeWorldState } = await loadModule();
    const state = normalizeWorldState({
        discovered_entry_ids: ['garden'],
        changes: [{
            id: '11111111-1111-4111-8111-111111111111',
            title: '旧事件被推翻',
            category: 'event',
            status: 'retconned',
            related_entry_ids: ['garden'],
            evidence_message_ids: ['22222222-2222-4222-8222-222222222222'],
            created_at: '2026-08-02T00:00:00+00:00',
            updated_at: '2026-08-02T01:00:00+00:00',
        }],
    });
    assert.equal(state.changes[0].status, 'retconned');
    assert.deepEqual(state.changes[0].relatedEntryIds, ['garden']);
    assert.equal(state.changes[0].updatedAt, '2026-08-02T01:00:00+00:00');
});
