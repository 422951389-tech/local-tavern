'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

async function loadRelationships() {
    return import('../web/relationships.mjs');
}

const IDS = [
    '00000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-000000000002',
    '00000000-0000-0000-0000-000000000003',
];

function sessionFixture(overrides = {}) {
    return {
        session_id: '存档A',
        revision: 7,
        characters_state: {
            alpha: { name: '阿尔法', affinity: 99, mood: '亲密' },
            beta: { name: '贝塔' },
        },
        message_history: IDS.map((id, index) => ({
            id,
            role: index % 2 ? 'assistant' : 'user',
            content: `证据消息 ${index}`,
        })),
        relationship_edges: [],
        summaries: [{ relations: ['文本中声称关系亲密'] }],
        ...overrides,
    };
}

function storedEdge(overrides = {}) {
    return {
        source_character_id: 'alpha',
        target_character_id: 'beta',
        relation_type: '盟友',
        strength: 80,
        evidence_message_ids: [IDS[0]],
        updated_at: '2026-07-22T12:00:00+08:00',
        ...overrides,
    };
}

class FakeClassList {
    constructor(element) { this.element = element; }
    add(...names) {
        const values = new Set(this.element.className.split(/\s+/).filter(Boolean));
        names.forEach(name => values.add(name));
        this.element.className = [...values].join(' ');
    }
    contains(name) { return this.element.className.split(/\s+/).filter(Boolean).includes(name); }
}

class FakeElement {
    constructor(tagName, ownerDocument) {
        this.tagName = String(tagName).toUpperCase();
        this.ownerDocument = ownerDocument;
        this.children = [];
        this.parentNode = null;
        this.className = '';
        this.classList = new FakeClassList(this);
        this.attributes = new Map();
        this.listeners = new Map();
        this.dataset = {};
        this.textContent = '';
        this.type = '';
        this.id = '';
        this.value = '';
        this.disabled = false;
        this.checked = false;
        this.selected = false;
        this.isConnected = true;
        this.scrollCalls = [];
        this.focusCalls = [];
    }
    set innerHTML(_value) { throw new Error('关系图谱禁止 innerHTML'); }
    get innerHTML() { return ''; }
    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }
    replaceChildren(...children) {
        this.children.forEach(child => { child.isConnected = false; });
        this.children = [];
        children.forEach(child => this.appendChild(child));
    }
    setAttribute(name, value) {
        this.attributes.set(name, String(value));
        if (name === 'id') this.id = String(value);
        if (name === 'class') this.className = String(value);
    }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    hasAttribute(name) { return this.attributes.has(name); }
    removeAttribute(name) { this.attributes.delete(name); }
    addEventListener(type, listener) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(listener);
    }
    dispatch(type, init = {}) {
        const event = { type, target: this, currentTarget: this, preventDefault() {}, ...init };
        for (const listener of this.listeners.get(type) || []) listener(event);
    }
    click() {
        if (!this.disabled) this.dispatch('click');
    }
    matches(selector) {
        if (selector.startsWith('#')) return this.id === selector.slice(1);
        if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
        const inputMatch = selector.match(/^input\[type="([^"]+)"\](:checked)?$/);
        if (inputMatch) {
            return this.tagName === 'INPUT'
                && this.type === inputMatch[1]
                && (!inputMatch[2] || this.checked);
        }
        return this.tagName.toLowerCase() === selector.toLowerCase();
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    querySelectorAll(selector) {
        const matches = [];
        for (const child of this.children) {
            if (child.matches(selector)) matches.push(child);
            matches.push(...child.querySelectorAll(selector));
        }
        return matches;
    }
    scrollIntoView(options) { this.scrollCalls.push(options); }
    focus(options) { this.focusCalls.push(options); }
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
}

async function flushAsync() {
    await Promise.resolve();
    await new Promise(resolve => setImmediate(resolve));
}

class FakeDocument {
    constructor() { this.defaultView = null; }
    createElement(tagName) { return new FakeElement(tagName, this); }
    createElementNS(_namespace, tagName) { return new FakeElement(tagName, this); }
}

function allText(node) {
    return [node.textContent, ...node.children.flatMap(allText)].filter(Boolean).join(' | ');
}

test('缺字段和文本型关系不会生成结构化边，空态明确显示未记录', async () => {
    const { normalizeRelationshipSession, renderRelationshipGraph, renderRelationshipList } = await loadRelationships();
    const source = sessionFixture();
    delete source.relationship_edges;
    const model = normalizeRelationshipSession(source);
    assert.deepEqual(model.edges, []);
    assert.equal(model.characters[0].name, '阿尔法');

    const documentRef = new FakeDocument();
    const host = documentRef.createElement('div');
    renderRelationshipGraph(documentRef, host, model);
    renderRelationshipList(documentRef, host, model);
    assert.match(allText(host), /未记录/);
    assert.doesNotMatch(allText(host), /文本中声称关系亲密/);
    assert.doesNotMatch(allText(host), /亲密/);
});

test('含控制、格式或代理字符的旧关系边不会进入渲染模型', async () => {
    const { normalizeRelationshipSession } = await loadRelationships();
    for (const relationType of ['盟\u202e友', `盟${String.fromCharCode(0xd800)}友`]) {
        const model = normalizeRelationshipSession(sessionFixture({
            relationship_edges: [storedEdge({ relation_type: relationType })],
        }));
        assert.deepEqual(model.edges, []);
    }
});

test('关系草稿严格拦截自环、非整数和无效证据', async () => {
    const { validateRelationshipDraft, RelationshipValidationError } = await loadRelationships();
    const session = sessionFixture();
    const valid = {
        source_character_id: 'alpha',
        target_character_id: 'beta',
        relation_type: '  盟友  ',
        strength: '100',
        evidence_message_ids: [IDS[0], IDS[1]],
    };
    assert.deepEqual(validateRelationshipDraft(valid, session), {
        source_character_id: 'alpha',
        target_character_id: 'beta',
        relation_type: '盟友',
        strength: 100,
        evidence_message_ids: [IDS[0], IDS[1]],
    });
    const invalid = [
        { ...valid, target_character_id: 'alpha' },
        { ...valid, strength: '1.5' },
        { ...valid, strength: 101 },
        { ...valid, evidence_message_ids: [] },
        { ...valid, evidence_message_ids: Array.from({ length: 21 }, (_, index) => `00000000-0000-0000-0000-${String(index + 1).padStart(12, '0')}`) },
        { ...valid, evidence_message_ids: [IDS[0], IDS[0]] },
        { ...valid, evidence_message_ids: ['ffffffff-ffff-ffff-ffff-ffffffffffff'] },
        { ...valid, relation_type: '盟\u202e友' },
        { ...valid, relation_type: '盟\u200b友' },
        { ...valid, relation_type: '\ud800' },
    ];
    for (const draft of invalid) {
        assert.throws(() => validateRelationshipDraft(draft, session), RelationshipValidationError);
    }
});

test('完整编辑器双提交只写一次，失败时保留草稿', async () => {
    const { createRelationshipEditor } = await loadRelationships();
    const pending = deferred();
    let saveCalls = 0;
    const documentRef = new FakeDocument();
    const editor = createRelationshipEditor({
        documentRef,
        session: sessionFixture(),
        sessionRef: { project: '项目A', save: '存档A' },
        service: {
            save: () => { saveCalls += 1; return pending.promise; },
            remove: async () => null,
        },
    });
    editor.root.querySelector('#relationship-source').value = 'alpha';
    editor.root.querySelector('#relationship-target').value = 'beta';
    editor.root.querySelector('#relationship-type').value = '盟友';
    editor.root.querySelector('#relationship-strength').value = '81';
    editor.root.querySelector('.relationship-evidence-checkbox').checked = true;
    const form = editor.root.querySelector('form');
    form.dispatch('submit');
    form.dispatch('submit');
    assert.equal(saveCalls, 1);
    assert.equal(editor.root.querySelector('.relationship-save-button').disabled, true);
    pending.resolve({ session: sessionFixture({ relationship_edges: [storedEdge({ strength: 81 })] }) });
    await flushAsync();
    assert.match(allText(editor.root), /关系已记录/);

    const failedDocument = new FakeDocument();
    const failed = createRelationshipEditor({
        documentRef: failedDocument,
        session: sessionFixture(),
        sessionRef: { project: '项目A', save: '存档A' },
        service: {
            save: async () => { throw new Error('写入拒绝'); },
            remove: async () => null,
        },
    });
    failed.root.querySelector('#relationship-source').value = 'alpha';
    failed.root.querySelector('#relationship-target').value = 'beta';
    failed.root.querySelector('#relationship-type').value = '失败草稿';
    failed.root.querySelector('#relationship-strength').value = '63';
    failed.root.querySelector('.relationship-evidence-checkbox').checked = true;
    failed.root.querySelector('form').dispatch('submit');
    await flushAsync();
    assert.equal(failed.root.querySelector('#relationship-type').value, '失败草稿');
    assert.equal(failed.root.querySelector('#relationship-strength').value, '63');
    assert.match(allText(failed.root), /操作失败：写入拒绝/);
});

test('删除挂起会锁定列表全部操作并阻止重复确认', async () => {
    const { createRelationshipEditor } = await loadRelationships();
    const pending = deferred();
    let removeCalls = 0;
    let confirmations = 0;
    const editor = createRelationshipEditor({
        documentRef: new FakeDocument(),
        session: sessionFixture({ relationship_edges: [storedEdge()] }),
        sessionRef: { project: '项目A', save: '存档A' },
        service: {
            save: async () => null,
            remove: () => { removeCalls += 1; return pending.promise; },
        },
        confirmDelete: () => { confirmations += 1; return true; },
    });
    const remove = editor.root.querySelector('.relationship-delete-button');
    remove.click();
    remove.click();
    assert.equal(removeCalls, 1);
    assert.equal(confirmations, 1);
    assert.equal(remove.disabled, true);
    assert.equal(editor.root.querySelector('.relationship-edit-button').disabled, true);
    assert.equal(editor.root.querySelector('.relationship-evidence-button').disabled, true);
    pending.resolve({ session: sessionFixture() });
    await flushAsync();
    assert.match(allText(editor.root), /关系已删除/);
});

test('证据定位协调器仅让当前且同 revision 的精确消息产生副作用', async () => {
    const {
        coordinateRelationshipEvidenceLocation,
        RelationshipNavigationError,
    } = await loadRelationships();
    const session = sessionFixture();
    const target = { id: IDS[0] };
    const effects = [];
    const options = {
        messageId: IDS[0],
        expectedRevision: 7,
        loadAuthoritativeSession: async () => session,
        assertCurrent: () => effects.push('guard'),
        findMessage: (value, id) => value.message_history.find(message => message.id === id),
        locateTarget: id => (id === IDS[0] ? target : null),
        releasePending: () => effects.push('release'),
        hideModal: () => { effects.push('hide'); return true; },
        focusTarget: value => effects.push(`focus:${value.id}`),
    };
    assert.equal(await coordinateRelationshipEvidenceLocation(options), true);
    assert.deepEqual(effects, ['guard', 'guard', 'release', 'hide', `focus:${IDS[0]}`]);

    for (const [patch, code] of [
        [{ loadAuthoritativeSession: async () => ({ ...session, revision: 8 }) }, 'revision_mismatch'],
        [{ findMessage: () => null }, 'message_not_found'],
        [{ locateTarget: () => null }, 'message_target_not_found'],
    ]) {
        const sideEffects = [];
        await assert.rejects(
            () => coordinateRelationshipEvidenceLocation({
                ...options,
                ...patch,
                assertCurrent: () => {},
                releasePending: () => sideEffects.push('release'),
                hideModal: () => { sideEffects.push('hide'); return true; },
                focusTarget: () => sideEffects.push('focus'),
            }),
            error => error instanceof RelationshipNavigationError && error.code === code,
        );
        assert.deepEqual(sideEffects, []);
    }

    const load = deferred();
    let current = true;
    const stale = coordinateRelationshipEvidenceLocation({
        ...options,
        loadAuthoritativeSession: () => load.promise,
        assertCurrent: () => {
            if (!current) throw new RelationshipNavigationError('编辑器已关闭', 'relationship_editor_closed');
        },
    });
    current = false;
    load.resolve(session);
    await assert.rejects(
        () => stale,
        error => error instanceof RelationshipNavigationError && error.code === 'relationship_editor_closed',
    );
});

test('同名角色与同正文证据使用易懂序号区分且不暴露内部 ID', async () => {
    const { createRelationshipEditor } = await loadRelationships();
    const session = sessionFixture({
        characters_state: {
            alpha: { name: '同名角色' },
            beta: { name: '同名角色' },
        },
        message_history: [
            { id: IDS[0], role: 'user', content: '相同正文', created_at: '2026-07-22T10:00:00+08:00' },
            { id: IDS[1], role: 'user', content: '相同正文', created_at: '2026-07-22T10:01:00+08:00' },
        ],
    });
    const editor = createRelationshipEditor({
        documentRef: new FakeDocument(),
        session,
        sessionRef: { project: '项目A', save: '存档A' },
        service: { save: async () => null, remove: async () => null },
    });
    const rendered = allText(editor.root);
    assert.match(rendered, /同名角色（同名角色 1）/);
    assert.match(rendered, /同名角色（同名角色 2）/);
    assert.match(rendered, /第 1 条 · 2026-07-22 10:00：相同正文/);
    assert.match(rendered, /第 2 条 · 2026-07-22 10:01：相同正文/);
    const labels = editor.root.querySelectorAll('.relationship-evidence-checkbox')
        .map(input => input.getAttribute('aria-label'));
    assert.doesNotMatch(labels[0], new RegExp(IDS[0]));
    assert.doesNotMatch(labels[1], new RegExp(IDS[1]));
});

test('关系服务使用 sessionWrite，编辑携带原复合键且不提交 updated_at', async () => {
    const { createRelationshipService } = await loadRelationships();
    const calls = [];
    const write = async (...args) => {
        calls.push(args);
        return { session: sessionFixture() };
    };
    const service = createRelationshipService(write, { relationships: '/api/session/relationships' });
    const ref = { project: '项目A', save: '存档A', epoch: 1 };
    const edge = {
        source_character_id: 'alpha', target_character_id: 'beta', relation_type: '盟友',
        strength: 80, evidence_message_ids: [IDS[0]],
    };
    const original = { source_character_id: 'alpha', target_character_id: 'beta', relation_type: '同伴' };
    await service.save(ref, edge, original);
    await service.remove(ref, original);
    assert.equal(calls[0][0], '/api/session/relationships');
    assert.equal(calls[0][1], 'PUT');
    assert.deepEqual(calls[0][2].original_key, original);
    assert.equal(Object.hasOwn(calls[0][2].edge, 'updated_at'), false);
    assert.equal(calls[1][1], 'DELETE');
    assert.deepEqual(calls[1][2].key, original);
});

test('single-flight gate 只允许持有 token 的操作释放', async () => {
    const { createRelationshipOperationGate } = await loadRelationships();
    const gate = createRelationshipOperationGate();
    const first = gate.acquire();
    assert.equal(typeof first, 'number');
    assert.equal(gate.acquire(), null);
    assert.equal(gate.release(first + 1), false);
    assert.equal(gate.isPending(), true);
    assert.equal(gate.release(first), true);
    assert.equal(gate.isPending(), false);
    assert.notEqual(gate.acquire(), first);
});

test('图谱和语义列表均显示方向、类型、数值强度与证据，恶意文本只进 textContent', async () => {
    const { normalizeRelationshipSession, renderRelationshipGraph, renderRelationshipList } = await loadRelationships();
    const model = normalizeRelationshipSession(sessionFixture({
        characters_state: {
            alpha: { name: '<img src=x onerror=1>' },
            beta: { name: '贝塔' },
        },
        message_history: [
            { id: IDS[0], role: 'user', content: '<script>alert(1)</script>' },
        ],
        relationship_edges: [storedEdge({ relation_type: '<b>盟友</b>' })],
    }));
    const documentRef = new FakeDocument();
    const host = documentRef.createElement('div');
    const graph = renderRelationshipGraph(documentRef, host, model);
    const cards = renderRelationshipList(documentRef, host, model, {});
    const renderedText = allText(host);
    assert.match(renderedText, /<img src=x onerror=1> → 贝塔/);
    assert.match(renderedText, /<b>盟友<\/b>/);
    assert.match(renderedText, /80\/100/);
    assert.equal(cards.length, 1);
    assert.match(allText(graph), /完整文字信息见下方关系列表/);
});

test('证据定位在 reduced-motion 下使用 auto 滚动', async () => {
    const { focusSearchTarget } = await import('../web/search.mjs');
    const documentRef = new FakeDocument();
    documentRef.defaultView = { matchMedia: () => ({ matches: true }) };
    const target = documentRef.createElement('article');
    assert.equal(focusSearchTarget(target), true);
    assert.equal(target.scrollCalls[0].behavior, 'auto');
    assert.equal(target.focusCalls.length, 1);
});

test('REL-1 顶栏、响应式与应用接线完整', () => {
    const root = path.resolve(__dirname, '..');
    const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
    const css = fs.readFileSync(path.join(root, 'web', 'style.css'), 'utf8');
    const moduleCss = fs.readFileSync(path.join(root, 'web', 'styles', 'story-modules.css'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'web', 'app.mjs'), 'utf8');
    assert.match(html, /id="tab-relations"/);
    assert.match(html, /aria-label="编辑人物关系"/);
    assert.match(html, /class="ico relation-tab-icon"/);
    assert.match(css, /\.relationship-list[\s\S]*grid-template-columns: repeat\(2/);
    assert.match(css, /@media \(max-width: 375px\)[\s\S]*\.relationship-editor/);
    assert.match(css, /min-height: 44px/);
    assert.match(moduleCss, /\.relationship-editor[\s\S]*grid-template-columns/);
    assert.match(app, /createRelationshipEditor/);
    assert.match(app, /coordinateRelationshipEvidenceLocation/);
    assert.match(app, /findMessage: findMessageById/);
    assert.match(app, /relationshipModalSerial = modalToken/);
    const showModalIndex = app.indexOf("title: '人物关系'");
    const commitSerialIndex = app.indexOf('relationshipModalSerial = modalToken');
    assert.equal(showModalIndex >= 0 && commitSerialIndex > showModalIndex, true);
});
