'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..');
const HTML = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
const APP = fs.readFileSync(path.join(ROOT, 'web', 'app.mjs'), 'utf8');
const WORLD = fs.readFileSync(path.join(ROOT, 'web', 'worldbook.mjs'), 'utf8');
const MODULE_CSS = fs.readFileSync(path.join(ROOT, 'web', 'styles', 'story-modules.css'), 'utf8');

async function loadSaveManager() {
    return import(`${pathToFileURL(path.join(ROOT, 'web', 'save-manager.mjs')).href}?t=${Date.now()}`);
}

test('五个世界构成入口保持独立并各自打开专用模块', () => {
    for (const id of ['tab-chars', 'tab-relations', 'tab-world', 'tab-user', 'tab-saves']) {
        assert.match(HTML, new RegExp(`id="${id}"[\\s\\S]{0,240}aria-haspopup="dialog"`));
    }
    assert.doesNotMatch(HTML, /世界构成中心/);
    assert.match(APP, /openCharactersEditor\(\)/);
    assert.match(APP, /showRelationshipsEditor/);
    assert.match(APP, /worldContextController\.showWorkbench\('library'\)/);
    assert.match(APP, /openUserEditor\(\)/);
    assert.match(APP, /showSaveManager\(\)/);
    assert.doesNotMatch(HTML, /id="save-dropdown"/);
});

test('世界设定默认使用人话并把内部控制放入专业设置', () => {
    for (const label of [
        '设定名称', '设定类型', '一句话介绍（可选）', '详细内容',
        '什么时候参考', '谁知道这件事', '专业设置', '使用情况',
    ]) assert.match(WORLD, new RegExp(label.replace(/[（）]/g, '.')));
    assert.match(WORLD, /activation: entry \? entry\.activation : 'keywords'/);
    assert.match(WORLD, /draft\.keywordsText = String\(draft\.title/);
    assert.match(WORLD, /draft\.id = candidate/);
    assert.match(WORLD, /createCollapsibleFormSection\('专业设置'/);
    assert.match(WORLD, /record\.kept \? '本轮已使用'/);
});

test('共享设计语言不合并模块并覆盖触控、响应式和 reduced-motion', () => {
    assert.match(MODULE_CSS, /\.character-manager-dialog\s*\{\s*--module-accent/);
    assert.match(MODULE_CSS, /\.relationship-manager-dialog\s*\{\s*--module-accent/);
    assert.match(MODULE_CSS, /\.user-profile-dialog\s*\{\s*--module-accent/);
    assert.match(MODULE_CSS, /\.save-manager-dialog\s*\{\s*--module-accent/);
    assert.match(MODULE_CSS, /min-height:\s*44px/);
    assert.match(MODULE_CSS, /@media \(max-width: 720px\)/);
    assert.match(MODULE_CSS, /@media \(prefers-reduced-motion: reduce\)/);
    assert.doesNotMatch(MODULE_CSS, /backdrop-filter|filter:\s*blur|perspective|parallax/i);
});

test('存档管理器规范化列表并保留当前存档上下文', async () => {
    const { normalizeSaveManagerState } = await loadSaveManager();
    const state = normalizeSaveManagerState({
        project: '默认项目',
        currentSaveId: 'save-a',
        currentModel: 'model-a',
        saves: [
            { session_id: 'save-a', name: '主线', message_count: 12, updated_at: '2026-08-02T10:00:00+08:00' },
            { session_id: '', name: '无效' },
            { session_id: 'save-b', name: '', message_count: -2 },
        ],
    });
    assert.equal(state.project, '默认项目');
    assert.equal(state.currentSaveId, 'save-a');
    assert.equal(state.currentModel, 'model-a');
    assert.deepEqual(state.saves.map(item => [item.id, item.name, item.messageCount]), [
        ['save-a', '主线', 12],
        ['save-b', '未命名存档', 0],
    ]);
});
