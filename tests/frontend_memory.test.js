const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

async function load(name) {
    return import(`../web/${name}.mjs`);
}

test('summary selection remains stable by ID across reordering and 0/1/N lists', async () => {
    const { selectSummaryId } = await load('summary-panel');
    const summaries = [
        { id: 'summary-a' },
        { id: 'summary-b' },
        { id: 'summary-c' },
        { id: 'summary-d' },
    ];
    assert.equal(selectSummaryId([], null), null);
    assert.equal(selectSummaryId([summaries[0]], null), 'summary-a');
    assert.equal(selectSummaryId(summaries, 'summary-b'), 'summary-b');
    assert.equal(selectSummaryId([...summaries].reverse(), 'summary-b'), 'summary-b');
    assert.equal(selectSummaryId(summaries, 'removed'), 'summary-d');
});

test('summary lifecycle labels distinguish active, interrupted, failed and missing source', async () => {
    const { summaryStatusLabel, summarySourceLabel } = await load('summary-panel');
    assert.equal(summaryStatusLabel({ status: 'pending', generation_active: true }), '生成中');
    assert.equal(summaryStatusLabel({ status: 'pending', generation_active: false }), '生成已中断，可恢复');
    assert.equal(summaryStatusLabel({ status: 'failed' }), '生成失败');
    assert.equal(summaryStatusLabel({ status: 'completed' }), '已完成');
    assert.equal(summarySourceLabel({ source_status: 'available' }), '原文快照可用');
    assert.equal(summarySourceLabel({ source_status: 'missing' }), '原文快照缺失');
    assert.equal(summarySourceLabel({ source_status: 'unlinked' }), '旧摘要未绑定原文快照');
});

test('summary form validation enforces required text, counts and item length', async () => {
    const { summaryPatchFromForm } = await load('summaries');
    const fields = new Map([
        ['text', { value: '有效摘要' }],
        ['time', { value: '' }],
        ['facts', { value: '一\n二\n三\n四\n五\n六' }],
        ['relations', { value: '' }],
    ]);
    const root = { querySelector: selector => fields.get(selector.match(/"(.+)"/)[1]) || null };
    assert.throws(() => summaryPatchFromForm(root), /最多 5 条/);
    fields.get('facts').value = '一\n二';
    assert.deepEqual(summaryPatchFromForm(root), {
        text: '有效摘要', time: '', facts: ['一', '二'], relations: [],
    });
    fields.get('text').value = '   ';
    assert.throws(() => summaryPatchFromForm(root), /不能为空/);
});

test('Prompt response schema requires the online summary template', async () => {
    const { createPromptService } = await load('prompt-editor');
    let validation;
    const service = createPromptService({
        async get(_path, options) {
            validation = options.schema({ system: 's', group_chat: 'g' });
            return { system: 's', group_chat: 'g', summary: 'm' };
        },
        async put() {},
        async post() {},
    }, {
        prompts: '/api/prompts',
        promptSave: name => `/api/prompts/${name}`,
        promptReset: name => `/api/prompts/${name}/reset`,
    });
    await service.load();
    assert.equal(typeof validation, 'string');
});

test('Prompt tabs support roving keyboard navigation', async () => {
    const { promptTabTargetIndex } = await load('prompt-editor');
    assert.equal(promptTabTargetIndex('ArrowRight', 0, 3), 1);
    assert.equal(promptTabTargetIndex('ArrowRight', 2, 3), 0);
    assert.equal(promptTabTargetIndex('ArrowLeft', 0, 3), 2);
    assert.equal(promptTabTargetIndex('Home', 2, 3), 0);
    assert.equal(promptTabTargetIndex('End', 0, 3), 2);
    assert.equal(promptTabTargetIndex('Tab', 0, 3), null);
});

test('pending summaries are watched after every committed session update', () => {
    const appSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.mjs'), 'utf8');
    assert.match(appSource, /watchPendingSummaries\(ref, commit\.session\);/);
    assert.match(appSource, /summary\.status !== 'pending'/);
    assert.match(appSource, /summary\.generation_active === false/);
    assert.match(appSource, /summaryWatchers\.has\(summary\.id\)/);
});

test('summary polling refreshes only its panel and rollback restarts watchers', () => {
    const appSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.mjs'), 'utf8');
    assert.match(appSource, /commitSummaryRefresh\(fresh, ref\);/);
    assert.doesNotMatch(appSource, /applySessionResult\(fresh, ref\);/);
    assert.match(appSource, /summariesUnchanged/);
    assert.match(appSource, /watchPendingSummaries\(restoredRef, state\.session\);/);
});

test('Prompt tabpanel wraps the native textarea instead of replacing its role', () => {
    const appSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.mjs'), 'utf8');
    assert.match(appSource, /editorPanel\.setAttribute\('role', 'tabpanel'\);/);
    assert.match(appSource, /promptTabTargetIndex\(/);
    assert.doesNotMatch(appSource, /textarea\.setAttribute\('role', 'tabpanel'\);/);
});
