'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const { createTurnState } = require('../web/turn-client.js');

async function loadWorldbook() {
    return import('../web/worldbook.mjs');
}

function worldbookSchema() {
    return {
        id: 'worldbook',
        label: '世界书',
        groups: [{
            key: '_basic',
            label: '基本信息',
            builtin: true,
            fields: [
                { key: 'id', label: '稳定 ID', type: 'text', fixed: true, required: true },
                { key: 'title', label: '标题', type: 'text' },
                { key: 'enabled', label: '启用', type: 'checkbox' },
                {
                    key: 'activation',
                    label: '触发方式',
                    type: 'select',
                    options: [
                        { value: 'always', label: '常驻' },
                        { value: 'keywords', label: '关键词' },
                        { value: 'manual', label: '手动' },
                    ],
                },
                {
                    key: 'keywords',
                    label: '关键词',
                    type: 'textarea',
                    array: true,
                    rows: 4,
                    maxItems: 64,
                    hint: '每行一个关键词',
                },
                {
                    key: 'priority',
                    label: '优先级',
                    type: 'number',
                    min: -1000000,
                    max: 1000000,
                },
                { key: 'content', label: '内容', type: 'textarea', rows: 8 },
            ],
        }],
        customGroup: { key: '_custom', label: '自定义字段' },
        activationValues: ['always', 'keywords', 'manual'],
    };
}

function entryFixture(overrides = {}) {
    return {
        id: 'manual-lore',
        title: '手动设定',
        enabled: true,
        activation: 'manual',
        keywords: [],
        priority: 8,
        content: '只在选中时注入',
        custom: { nested: { preserved: true } },
        keys: ['旧触发词'],
        constant: false,
        position: 'after',
        legacy_fact: { nested: ['原样保留'] },
        ...overrides,
    };
}

test('worldbook schema and legacy entries normalize without losing the backend contract', async () => {
    const {
        normalizeWorldbookSchema,
        normalizeWorldbookEntry,
        normalizeWorldbookEntries,
    } = await loadWorldbook();
    const normalized = normalizeWorldbookSchema(worldbookSchema());

    assert.equal(normalized.id, 'worldbook');
    assert.equal(normalized.label, '世界书');
    assert.deepEqual(
        normalized.fields.activation.options,
        [
            { value: 'always', label: '常驻' },
            { value: 'keywords', label: '关键词' },
            { value: 'manual', label: '手动' },
        ],
    );
    assert.equal(normalized.fields.id.fixed, true);
    assert.equal(normalized.fields.keywords.maxItems, 64);
    assert.equal(normalized.fields.priority.min, -1000000);
    assert.equal(normalized.fields.priority.max, 1000000);
    assert.deepEqual(normalized.customGroup, { key: '_custom', label: '自定义字段' });

    const legacy = normalizeWorldbookEntry({ id: 'legacy', content: '旧格式内容' });
    assert.deepEqual({
        id: legacy.id,
        title: legacy.title,
        enabled: legacy.enabled,
        activation: legacy.activation,
        keywords: legacy.keywords,
        priority: legacy.priority,
        content: legacy.content,
        custom: legacy.custom,
    }, {
        id: 'legacy',
        title: '',
        enabled: true,
        activation: 'always',
        keywords: [],
        priority: 0,
        content: '旧格式内容',
        custom: {},
    });
    assert.throws(() => normalizeWorldbookEntry({ id: 'bad', activation: 'random' }), /activation 无效/);
    assert.throws(() => normalizeWorldbookEntry({ id: 'bad', enabled: 'yes' }), /enabled 必须是布尔值/);
    assert.throws(() => normalizeWorldbookEntries([entryFixture(), entryFixture()]), /ID 重复/);
});

test('worldbook draft serialization validates limits and preserves untouched custom JSON values', async () => {
    const { serializeWorldbookDraft, WorldbookValidationError } = await loadWorldbook();
    const draft = {
        id: 'keyword-lore',
        title: '关键词条目',
        enabled: true,
        activation: 'keywords',
        keywordsText: ' 龙 \nDRAGON\n龙\n',
        priority: '25',
        content: '龙类设定',
        customRows: [{
            key: 'metadata',
            value: '{"preserved":true}',
            originalKey: 'metadata',
            originalValue: { preserved: true },
            dirty: false,
        }],
    };

    assert.deepEqual(serializeWorldbookDraft(draft, { schema: worldbookSchema() }), {
        id: 'keyword-lore',
        title: '关键词条目',
        enabled: true,
        activation: 'keywords',
        keywords: ['龙', 'DRAGON'],
        priority: 25,
        content: '龙类设定',
        custom: { metadata: { preserved: true } },
    });

    assert.throws(
        () => serializeWorldbookDraft({ ...draft, keywordsText: '' }, { schema: worldbookSchema() }),
        error => error instanceof WorldbookValidationError
            && error.field === 'keywords'
            && /至少需要一个关键词/.test(error.message),
    );
    assert.throws(
        () => serializeWorldbookDraft({ ...draft, priority: '1000001' }, { schema: worldbookSchema() }),
        error => error instanceof WorldbookValidationError
            && error.field === 'priority'
            && /-1000000 到 1000000/.test(error.message),
    );
});

test('worldbook service uses schema validation and delegates manual selection to sessionWrite CAS', async () => {
    const { createWorldbookService } = await loadWorldbook();
    const calls = [];
    const sessionWrites = [];
    const schemaBody = worldbookSchema();
    const listBody = { entries: [entryFixture()] };
    const client = {
        async get(url, options) {
            const body = url === '/api/schema/worldbook' ? schemaBody : listBody;
            assert.equal(options.schema(body), true);
            calls.push({ method: 'GET', url });
            return body;
        },
        async put(url, body, options) {
            const response = { saved: true, id: body.data.id };
            assert.equal(options.schema(response), true);
            calls.push({ method: 'PUT', url, body });
            return response;
        },
        async delete(url, options) {
            const response = { deleted: true, id: 'manual-lore' };
            assert.equal(options.schema(response), true);
            calls.push({ method: 'DELETE', url });
            return response;
        },
    };
    const sessionWrite = async (...args) => {
        sessionWrites.push(args);
        return { session: { project: '默认 项目', session_id: 'save-a', revision: 12 } };
    };
    const service = createWorldbookService(client, sessionWrite, {
        worldbookSchema: '/api/schema/worldbook',
        worldbook: '/api/worldbook',
        worldbookSave: id => `/api/worldbook/${encodeURIComponent(id)}`,
        worldbookDelete: id => `/api/worldbook/${encodeURIComponent(id)}`,
        worldbookManual: '/api/session/worldbook/manual',
    });

    await service.schema();
    const entries = await service.list('默认 项目');
    await service.save('默认 项目', 'manual-lore', entries[0]);
    await service.remove('默认 项目', 'manual-lore');
    const result = await service.saveManual(
        { project: '默认 项目', save: 'save-a', epoch: 3 },
        ['z-last', 'manual-lore', 'manual-lore'],
    );

    const encodedProject = encodeURIComponent('默认 项目');
    assert.deepEqual(calls.map(call => `${call.method} ${call.url}`), [
        'GET /api/schema/worldbook',
        `GET /api/worldbook?project=${encodedProject}`,
        `PUT /api/worldbook/manual-lore?project=${encodedProject}`,
        `DELETE /api/worldbook/manual-lore?project=${encodedProject}`,
    ]);
    assert.deepEqual(calls[2].body, { data: entryFixture() });
    assert.deepEqual(sessionWrites, [[
        '/api/session/worldbook/manual',
        'PATCH',
        {
            project: '默认 项目',
            save: 'save-a',
            entry_ids: ['manual-lore', 'z-last'],
        },
        '保存当前存档的手动世界书',
    ]]);
    assert.equal(result.session.revision, 12);
});

test('worldbook diagnostics whitelist display fields and never retain content or source text', async () => {
    const { sanitizeWorldbookDiagnostics } = await loadWorldbook();
    const diagnostics = sanitizeWorldbookDiagnostics({
        worldbook_matches: [{
            id: 'dragon-lore',
            activation: 'keywords',
            enabled: true,
            priority: 16,
            trigger: 'keywords',
            matched_keywords: ['龙', ' dragon '],
            matched_sources: [{
                scope: 'history',
                ref: 'message-8',
                turn_distance: 1,
                text: 'SECRET_SOURCE_TEXT',
            }],
            hit_count: 2,
            recency_distance: 1,
            rank: 1,
            activated: true,
            kept: true,
            reason: 'matched',
            content: 'SECRET_WORLDBOOK_CONTENT',
        }],
    });

    assert.deepEqual(diagnostics, [{
        id: 'dragon-lore',
        activation: 'keywords',
        enabled: true,
        activated: true,
        priority: 16,
        matchCount: 2,
        recency: 1,
        matchedKeywords: ['龙', 'dragon'],
        matchedSources: ['history:message-8，距当前 1 轮'],
        rank: 1,
        trigger: 'keywords',
        kept: true,
        reason: 'matched',
    }]);
    const rendered = JSON.stringify(diagnostics);
    assert.doesNotMatch(rendered, /SECRET_WORLDBOOK_CONTENT|SECRET_SOURCE_TEXT|content|text/);
});

test('turn state retains prompt diagnostics for the specialized worldbook view', () => {
    const promptDiagnostics = { worldbook_matches: [{ id: 'lore-a', kept: true }] };
    const turn = createTurnState({
        turn_id: '11111111-1111-4111-8111-111111111111',
        project: 'project-a',
        save: 'save-a',
        status: 'pending',
        events_url: '/events',
        cancel_url: '/cancel',
        prompt_diagnostics: promptDiagnostics,
    }, { project: 'project-a', save: 'save-a', epoch: 1 });

    assert.equal(turn.promptDiagnostics, promptDiagnostics);
});

test('worldbook integration clears stale diagnostics, locks writes, and keeps accessible mobile controls', () => {
    const appSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.mjs'), 'utf8');
    const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'worldbook.mjs'), 'utf8');
    const styleSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'style.css'), 'utf8');

    assert.match(appSource, /worldbookSchema:\s*'\/api\/schema\/worldbook'/);
    assert.match(appSource, /worldbookManual:\s*'\/api\/session\/worldbook\/manual'/);
    assert.match(appSource, /state\.lastWorldbookDiagnostics = null;/);
    assert.match(appSource, /'#project-btn', '#tab-world', '#tab-relations', '#tab-saves'/);
    assert.match(appSource, /if \(!canPerformTurnAction\('card_write', editorRef\)\) \{/);
    assert.match(appSource, /加载世界书期间已开始生成，请等待完成后重试/);
    assert.match(appSource, /'\.worldbook-write-control'/);
    assert.match(appSource, /worldbookService\.saveManual/);
    assert.match(moduleSource, /createElement\(documentRef, 'button'/);
    assert.match(moduleSource, /createElement\(documentRef, 'label'/);
    assert.match(moduleSource, /tag:\s*'select'/);
    assert.match(moduleSource, /createElement\(documentRef, config\.tag \|\| 'input'/);
    assert.match(moduleSource, /idInput\.readOnly = selectedId !== null/);
    assert.doesNotMatch(moduleSource, /\.innerHTML\s*=/);
    assert.match(styleSource, /min-height:\s*44px/);
    assert.match(styleSource, /:focus-visible/);
    assert.match(styleSource, /100dvh/);
    assert.match(styleSource, /font-size:\s*16px/);
});
