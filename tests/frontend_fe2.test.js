'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const moduleCache = new Map();

function loadModule(name) {
    if (!moduleCache.has(name)) moduleCache.set(name, import(`../web/${name}.mjs`));
    return moduleCache.get(name);
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((onResolve, onReject) => {
        resolve = onResolve;
        reject = onReject;
    });
    return { promise, resolve, reject };
}

async function flushTasks() {
    await Promise.resolve();
    await new Promise(resolve => setImmediate(resolve));
}

class FakeClassList {
    constructor(owner) {
        this.owner = owner;
        this.values = new Set();
    }

    setFromString(value) {
        this.values = new Set(String(value || '').split(/\s+/).filter(Boolean));
    }

    add(...values) {
        values.filter(Boolean).forEach(value => this.values.add(value));
    }

    remove(...values) {
        values.forEach(value => this.values.delete(value));
    }

    contains(value) {
        return this.values.has(value);
    }

    toggle(value, force) {
        const shouldAdd = force === undefined ? !this.contains(value) : Boolean(force);
        if (shouldAdd) this.add(value);
        else this.remove(value);
        return shouldAdd;
    }

    toString() {
        return [...this.values].join(' ');
    }
}

function matchesSelector(element, selector) {
    const normalized = selector.trim();
    if (!normalized) return false;
    if (normalized.startsWith('.')) return element.classList.contains(normalized.slice(1));
    const dataField = normalized.match(/^\[data-field="([^"]+)"\]$/);
    if (dataField) return element.dataset.field === dataField[1];
    const tag = normalized.match(/^(input|textarea|select|button)(?::not\(\[disabled\]\))?$/i);
    if (tag) {
        if (element.tagName !== tag[1].toUpperCase()) return false;
        return !normalized.includes(':not') || !element.disabled;
    }
    return element.tagName === normalized.toUpperCase();
}

class FakeElement {
    constructor(tagName = 'div', ownerDocument = null) {
        this.tagName = String(tagName).toUpperCase();
        this.ownerDocument = ownerDocument;
        this.classList = new FakeClassList(this);
        this.children = [];
        this.parentNode = null;
        this.dataset = {};
        this.attributes = new Map();
        this.listeners = new Map();
        this.textContent = '';
        this.value = '';
        this.type = '';
        this.checked = false;
        this.disabled = false;
        this.title = '';
        this.focusCount = 0;
        this._innerHTML = '';
    }

    get className() {
        return this.classList.toString();
    }

    set className(value) {
        this.classList.setFromString(value);
    }

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
    }

    get isConnected() {
        return Boolean(this.parentNode);
    }

    appendChild(child) {
        if (child.parentNode) child.remove();
        this.children.push(child);
        child.parentNode = this;
        return child;
    }

    replaceChildren(...children) {
        this.children.forEach(child => { child.parentNode = null; });
        this.children = [];
        children.forEach(child => this.appendChild(child));
    }

    replaceWith(replacement) {
        if (!this.parentNode) return;
        const parent = this.parentNode;
        const index = parent.children.indexOf(this);
        if (replacement.parentNode) replacement.remove();
        parent.children[index] = replacement;
        replacement.parentNode = parent;
        this.parentNode = null;
    }

    insertAdjacentElement(position, element) {
        if (position !== 'afterend' || !this.parentNode) throw new Error('测试假 DOM 仅支持 afterend');
        const parent = this.parentNode;
        const index = parent.children.indexOf(this);
        if (element.parentNode) element.remove();
        parent.children.splice(index + 1, 0, element);
        element.parentNode = parent;
        return element;
    }

    remove() {
        if (!this.parentNode) return;
        const index = this.parentNode.children.indexOf(this);
        if (index >= 0) this.parentNode.children.splice(index, 1);
        this.parentNode = null;
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    removeAttribute(name) {
        this.attributes.delete(name);
    }

    addEventListener(type, listener) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(listener);
    }

    dispatch(type, init = {}) {
        const event = {
            type,
            target: this,
            currentTarget: this,
            defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; },
            ...init,
        };
        for (const listener of this.listeners.get(type) || []) listener(event);
        return event;
    }

    focus() {
        this.focusCount += 1;
        if (this.ownerDocument) this.ownerDocument.activeElement = this;
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }

    querySelectorAll(selector) {
        const selectors = String(selector).split(',').map(value => value.trim());
        const matches = [];
        const visit = element => {
            for (const child of element.children) {
                if (selectors.some(candidate => matchesSelector(child, candidate))) matches.push(child);
                visit(child);
            }
        };
        visit(this);
        return matches;
    }
}

class FakeDocument {
    constructor() {
        this.activeElement = null;
        this.listeners = new Map();
    }

    createElement(tagName) {
        return new FakeElement(tagName, this);
    }

    addEventListener(type, listener) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(listener);
    }

    dispatch(type, init = {}) {
        const event = {
            type,
            defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; },
            ...init,
        };
        for (const listener of this.listeners.get(type) || []) listener(event);
        return event;
    }
}

function modalFixture() {
    const documentRef = new FakeDocument();
    const previousFocus = documentRef.createElement('button');
    previousFocus.focus();
    const elements = {
        backdrop: documentRef.createElement('div'),
        dialog: documentRef.createElement('section'),
        title: documentRef.createElement('h2'),
        body: documentRef.createElement('div'),
        footer: documentRef.createElement('footer'),
        cancelButton: documentRef.createElement('button'),
        confirmButton: documentRef.createElement('button'),
        closeButton: documentRef.createElement('button'),
        error: documentRef.createElement('div'),
    };
    elements.backdrop.classList.add('hidden');
    elements.error.classList.add('hidden');
    return { documentRef, previousFocus, elements };
}

test('Modal 只在成功后关闭；失败保留输入、错误和焦点', async () => {
    const { createModalController } = await loadModule('modal');
    const success = modalFixture();
    const successInput = success.documentRef.createElement('input');
    successInput.value = '保留的名称';
    let calls = 0;
    const successController = createModalController(success.elements, { documentRef: success.documentRef });
    successController.show({
        title: '新建存档',
        body: successInput,
        footer: { onConfirm: async () => { calls += 1; } },
    });
    assert.equal(await successController.confirm(), true);
    assert.equal(calls, 1);
    assert.equal(success.elements.backdrop.classList.contains('hidden'), true);
    assert.equal(success.elements.body.children.length, 0);
    assert.equal(success.previousFocus.focusCount, 2);

    const failure = modalFixture();
    const failureInput = failure.documentRef.createElement('input');
    failureInput.value = '不能丢失的新文本';
    const failureController = createModalController(failure.elements, { documentRef: failure.documentRef });
    failureController.show({
        title: '重命名',
        body: failureInput,
        footer: { onConfirm: async () => { throw new Error('服务拒绝写入'); } },
    });
    assert.equal(await failureController.confirm(), false);
    assert.equal(failure.elements.backdrop.classList.contains('hidden'), false);
    assert.equal(failure.elements.body.children[0], failureInput);
    assert.equal(failureInput.value, '不能丢失的新文本');
    assert.equal(failure.elements.error.textContent, '服务拒绝写入');
    assert.equal(failure.elements.error.classList.contains('hidden'), false);
    assert.equal(failure.documentRef.activeElement, failureInput);
    assert.equal(failure.elements.dialog.getAttribute('aria-busy'), 'false');
    assert.equal(failure.elements.confirmButton.disabled, false);
});

test('Modal 双击确认只写一次，pending 期间取消和 Escape 均无效', async () => {
    const { createModalController } = await loadModule('modal');
    const { documentRef, elements } = modalFixture();
    const input = documentRef.createElement('input');
    const gate = deferred();
    let calls = 0;
    const controller = createModalController(elements, { documentRef });
    controller.bind();
    controller.show({
        title: '异步保存',
        body: input,
        footer: {
            pendingText: '保存中…',
            onConfirm: () => {
                calls += 1;
                return gate.promise;
            },
        },
    });

    elements.confirmButton.dispatch('click');
    elements.confirmButton.dispatch('click');
    assert.equal(calls, 1);
    assert.equal(controller.isPending(), true);
    assert.equal(elements.backdrop.getAttribute('aria-busy'), 'true');
    assert.equal(elements.confirmButton.disabled, true);
    assert.equal(elements.cancelButton.disabled, true);

    elements.cancelButton.dispatch('click');
    const escape = documentRef.dispatch('keydown', { key: 'Escape' });
    assert.equal(escape.defaultPrevented, true);
    assert.equal(elements.backdrop.classList.contains('hidden'), false);
    assert.equal(elements.body.children[0], input);

    gate.resolve(true);
    await flushTasks();
    assert.equal(elements.backdrop.classList.contains('hidden'), true);
    assert.equal(calls, 1);
});

test('Modal 字符串正文仅作为纯文本呈现', async () => {
    const { createModalController } = await loadModule('modal');
    const { documentRef, elements } = modalFixture();
    const malicious = '<img src=x onerror="globalThis.pwned=true"><script>alert(1)</script>';
    const controller = createModalController(elements, { documentRef });

    assert.equal(controller.show({ title: '纯文本', body: malicious }), true);
    assert.equal(elements.body.textContent, malicious);
    assert.equal(elements.body.children.length, 0);
    assert.equal(elements.body.innerHTML, '');
});

function fieldRow(documentRef, { key = '', type = '', array = false, value = '', checked = false, customKey }) {
    const row = documentRef.createElement('div');
    row.className = 'fld-row';
    row.dataset.key = key;
    row.dataset.type = type;
    row.dataset.array = array ? '1' : '0';
    if (customKey !== undefined) {
        const keyInput = documentRef.createElement('input');
        keyInput.className = 'ce-field-key';
        keyInput.value = customKey;
        row.appendChild(keyInput);
    }
    const valueInput = documentRef.createElement(type === 'textarea' || array ? 'textarea' : 'input');
    valueInput.className = 'ce-field-val';
    valueInput.type = type === 'checkbox' ? 'checkbox' : (type === 'number' ? 'number' : 'text');
    valueInput.value = value;
    valueInput.checked = checked;
    row.appendChild(valueInput);
    return row;
}

test('卡片字段收集不依赖 dataset.key，并保留 custom、嵌套、数组和 checkbox 语义', async () => {
    const { collectCardEditorData } = await loadModule('card-editor');
    const documentRef = new FakeDocument();
    const root = documentRef.createElement('div');
    root.appendChild(fieldRow(documentRef, {
        key: '',
        customKey: '  新字段  ',
        value: '  新值  ',
    }));
    root.appendChild(fieldRow(documentRef, {
        key: 'profile.stats.level',
        type: 'number',
        value: '7',
    }));
    root.appendChild(fieldRow(documentRef, {
        key: 'tags',
        array: true,
        value: '第一项\n\n 第二项 ',
    }));
    root.appendChild(fieldRow(documentRef, {
        key: 'enabled',
        type: 'checkbox',
        checked: false,
    }));

    assert.deepEqual(collectCardEditorData(root), {
        data: {
            custom: { 新字段: '新值' },
            profile: { stats: { level: 7 } },
            tags: ['第一项', '第二项'],
            enabled: false,
            id: 'user',
        },
        idValue: '',
    });
});

test('角色、世界书和用户三类卡片重开时均恢复 custom 字段', async () => {
    const { groupsWithCustomFields } = await loadModule('card-editor');
    const base = [{ key: 'base', label: '基础', fields: [{ key: 'name', label: '名称' }] }];
    const cases = [
        ['character', { 口癖: '晚上好' }, '角色扩展'],
        ['worldbook', { 气候: '永冬' }, '世界书扩展'],
        ['user', { 偏好: '红茶' }, '用户扩展'],
    ];

    for (const [kind, custom, label] of cases) {
        const groups = groupsWithCustomFields(base, { custom }, { key: `${kind}-custom`, label });
        assert.equal(groups.length, 2, kind);
        assert.equal(groups[1].key, `${kind}-custom`, kind);
        assert.equal(groups[1].label, label, kind);
        assert.deepEqual(groups[1].fields, Object.entries(custom).map(([key, value]) => ({
            key,
            label: key,
            value,
            type: 'custom',
            builtin: false,
        })), kind);
        assert.notStrictEqual(groups[0], base[0]);
        assert.notStrictEqual(groups[0].fields[0], base[0].fields[0]);
    }
});

test('拖拽结束会清理编辑器根节点内全部临时 class', async () => {
    const { clearDragState } = await loadModule('card-editor');
    const documentRef = new FakeDocument();
    const root = documentRef.createElement('div');
    const group = documentRef.createElement('div');
    group.className = 'dragging drag-over permanent';
    const field = documentRef.createElement('div');
    field.className = 'fld-dragging fld-drag-over permanent-field';
    group.appendChild(field);
    root.appendChild(group);

    clearDragState(root);
    assert.equal(group.className, 'permanent');
    assert.equal(field.className, 'permanent-field');
});

function messageFixture() {
    const documentRef = new FakeDocument();
    const messageElement = documentRef.createElement('article');
    const content = documentRef.createElement('div');
    content.className = 'content';
    content.textContent = '原文本';
    messageElement.appendChild(content);
    return { documentRef, messageElement, content };
}

test('消息保存失败时保留 textarea、新文本、错误和焦点', async () => {
    const { enterMessageEditor } = await loadModule('message-editor');
    const fixture = messageFixture();
    const editor = enterMessageEditor({
        documentRef: fixture.documentRef,
        messageElement: fixture.messageElement,
        messageRef: { messageId: 'message-a' },
        save: async () => { throw new Error('版本冲突'); },
    });
    editor.textarea.value = '失败后仍在的新文本';

    assert.equal(await editor.commit(), false);
    assert.equal(editor.state(), 'editing');
    assert.equal(editor.textarea.isConnected, true);
    assert.equal(editor.textarea.value, '失败后仍在的新文本');
    assert.equal(editor.textarea.disabled, false);
    assert.equal(editor.error.textContent, '保存失败：版本冲突');
    assert.equal(editor.error.classList.contains('hidden'), false);
    assert.equal(fixture.documentRef.activeElement, editor.textarea);
    assert.equal(fixture.messageElement.getAttribute('aria-busy'), null);
    assert.equal(fixture.content.isConnected, false);
});

test('消息保存先重渲染再失败时按 message_id 交给恢复钩子保留草稿', async () => {
    const { enterMessageEditor } = await loadModule('message-editor');
    const fixture = messageFixture();
    const recovered = [];
    const messageRef = { message_id: '11111111-1111-4111-8111-111111111111' };
    const editor = enterMessageEditor({
        documentRef: fixture.documentRef,
        messageElement: fixture.messageElement,
        messageRef,
        save: async () => {
            fixture.messageElement.replaceChildren(fixture.documentRef.createElement('div'));
            throw new Error('revision conflict');
        },
        recover: payload => { recovered.push(payload); },
    });
    editor.textarea.value = '409 刷新后仍需保留的草稿';

    assert.equal(await editor.commit(), false);
    assert.equal(editor.state(), 'finished');
    assert.equal(recovered.length, 1);
    assert.equal(recovered[0].messageRef, messageRef);
    assert.equal(recovered[0].draft, '409 刷新后仍需保留的草稿');
    assert.equal(recovered[0].error.message, 'revision conflict');
});

test('结构化消息编辑使用原始文本，取消后完整恢复原模块 DOM', async () => {
    const { enterMessageEditor } = await loadModule('message-editor');
    const fixture = messageFixture();
    fixture.content.textContent = '剧情推进角色回应';
    const storyModule = fixture.documentRef.createElement('section');
    storyModule.className = 'story-module';
    fixture.content.appendChild(storyModule);
    const raw = '📍 山门 | ⏱️ 清晨\n🎭 守门人 | 💝 55%';
    const editor = enterMessageEditor({
        documentRef: fixture.documentRef,
        messageElement: fixture.messageElement,
        messageRef: { message_id: '11111111-1111-4111-8111-111111111111' },
        initialValue: raw,
        save: async () => true,
    });

    assert.equal(editor.textarea.value, raw);
    assert.equal(editor.cancel(), true);
    assert.equal(fixture.content.isConnected, true);
    assert.equal(fixture.content.textContent, '剧情推进角色回应');
    assert.equal(fixture.content.children[0], storyModule);
    assert.equal(storyModule.isConnected, true);
});

test('消息编辑离开输入框不保存，显式保存按钮成功后才退出编辑', async () => {
    const { enterMessageEditor } = await loadModule('message-editor');
    const fixture = messageFixture();
    const gate = deferred();
    const writes = [];
    const messageRef = { messageId: 'message-b', expectedRevision: 4 };
    const editor = enterMessageEditor({
        documentRef: fixture.documentRef,
        messageElement: fixture.messageElement,
        messageRef,
        save: (ref, text) => {
            writes.push({ ref, text });
            return gate.promise;
        },
    });
    editor.textarea.value = '只写一次的新文本';
    editor.textarea.dispatch('blur');
    await Promise.resolve();

    assert.equal(writes.length, 0);
    assert.equal(editor.state(), 'editing');
    assert.equal(editor.hint.textContent, 'Ctrl+Enter 保存 · Esc 取消；离开输入框不会自动保存');
    assert.equal(editor.saveButton.isConnected, true);
    assert.equal(editor.cancelButton.isConnected, true);

    editor.saveButton.dispatch('click');
    await Promise.resolve();
    assert.equal(writes.length, 1);
    assert.equal(editor.state(), 'saving');
    assert.equal(editor.textarea.disabled, true);
    assert.equal(fixture.messageElement.getAttribute('aria-busy'), 'true');
    assert.equal(editor.cancel(), false);

    gate.resolve();
    await flushTasks();
    assert.equal(writes.length, 1);
    assert.deepEqual(writes[0], { ref: messageRef, text: '只写一次的新文本' });
    assert.equal(editor.state(), 'finished');
    assert.equal(fixture.content.isConnected, true);
    assert.equal(fixture.content.textContent, '只写一次的新文本');
    assert.equal(editor.textarea.isConnected, false);
});

test('消息编辑输入法组合期间 Ctrl+Enter 不提交，组合结束后快捷键只提交一次', async () => {
    const { enterMessageEditor } = await loadModule('message-editor');
    const fixture = messageFixture();
    const writes = [];
    const editor = enterMessageEditor({
        documentRef: fixture.documentRef,
        messageElement: fixture.messageElement,
        messageRef: { messageId: 'message-ime' },
        save: async (_ref, text) => { writes.push(text); },
    });
    editor.textarea.value = '输入法文本';

    const composing = editor.textarea.dispatch('keydown', {
        key: 'Enter', ctrlKey: true, isComposing: true, keyCode: 229,
    });
    await flushTasks();
    assert.equal(composing.defaultPrevented, false);
    assert.deepEqual(writes, []);
    assert.equal(editor.state(), 'editing');

    const committed = editor.textarea.dispatch('keydown', { key: 'Enter', ctrlKey: true });
    await flushTasks();
    assert.equal(committed.defaultPrevented, true);
    assert.deepEqual(writes, ['输入法文本']);
    assert.equal(editor.state(), 'finished');
});

test('Chat payload 固定 SessionRef/revision，活动回合可写入、恢复和清除', async () => {
    const { createTurnPayload, createTurnPersistence } = await loadModule('chat');
    assert.deepEqual(createTurnPayload({
        ref: { project: '项目 A', save: '存档 A', epoch: 9 },
        revision: 12,
        provider: 'anthropic',
        model: 'local-model',
        params: { temperature: 0.4, max_tokens: 800 },
        userInput: '继续故事',
    }), {
        project: '项目 A',
        save: '存档 A',
        expected_revision: 12,
        provider: 'anthropic',
        model: 'local-model',
        temperature: 0.4,
        max_tokens: 800,
        user_input: '继续故事',
    });

    const values = new Map();
    const storage = {
        getItem: key => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value),
        removeItem: key => values.delete(key),
    };
    const persistence = createTurnPersistence(storage, 'test.active-turn');
    const stored = persistence.write({
        turnId: 'turn-a',
        ref: { project: '项目 A', save: '存档 A', epoch: 9 },
        lastEventId: 17,
    });
    assert.deepEqual(stored, {
        turnId: 'turn-a', project: '项目 A', save: '存档 A', lastEventId: 17,
    });
    assert.deepEqual(persistence.read(), stored);
    assert.equal(Object.isFrozen(persistence.read()), true);
    persistence.clear();
    assert.equal(persistence.read(), null);

    values.set('test.active-turn', JSON.stringify({
        turn_id: 'legacy-turn', project: '旧项目', save: '旧存档', last_event_id: 5,
    }));
    assert.deepEqual(persistence.read(), {
        turnId: 'legacy-turn', project: '旧项目', save: '旧存档', lastEventId: 5,
    });
    values.set('test.active-turn', '{bad json');
    assert.equal(persistence.read(), null);
});

test('Render 模块输出稳定消息结构且只写 textContent', async () => {
    const { affinityBar, createMessageElement } = await loadModule('render');
    const documentRef = new FakeDocument();
    assert.equal(affinityBar(55, value => value), '██████░░░░');
    const rendered = createMessageElement(documentRef, {
        role: 'assistant',
        content: '<img src=x onerror=alert(1)>',
        timeText: '12:00',
        message: { id: 'stable-id', pinned: true, in_prompt: false },
    });
    assert.equal(rendered.container.className, 'msg assistant');
    assert.equal(rendered.container.dataset.messageId, 'stable-id');
    assert.equal(rendered.contentElement.textContent, '<img src=x onerror=alert(1)>');
    assert.equal(rendered.contentElement.innerHTML, '');
    const checkbox = rendered.container.querySelector('.msg-checkbox');
    const pin = rendered.container.querySelector('.pin');
    assert.equal(checkbox.checked, false);
    assert.equal(pin.classList.contains('active'), true);
    assert.equal(pin.getAttribute('aria-label'), '钉选为常驻记忆');
});

test('项目与存档服务封装 URL、payload 和响应 schema', async () => {
    const { createProjectService } = await loadModule('projects');
    const { createSaveService } = await loadModule('saves');
    const calls = [];
    const endpoints = {
        projects: '/api/projects',
        characters: '/api/characters',
        worldbook: '/api/worldbook',
        sessions: '/api/sessions',
        session: '/api/session',
        sessionCreate: '/api/session/create',
        sessionExport: '/api/session/export',
        sessionImport: '/api/session/import',
    };
    const responses = new Map([
        ['/api/projects', { projects: ['项目 A'] }],
        ['/api/sessions?project=%E9%A1%B9%E7%9B%AE%20A', { sessions: [{ session_id: '存档 A' }] }],
        ['/api/session?project=%E9%A1%B9%E7%9B%AE%20A&save=%E5%AD%98%E6%A1%A3%20A', {
            session_id: '存档 A', revision: 3,
        }],
        ['/api/session/export?project=%E9%A1%B9%E7%9B%AE%20A&save=%E5%AD%98%E6%A1%A3%20A', {
            json_str: '{"ok":true}',
        }],
    ]);
    const client = {
        async get(path, options = {}) {
            calls.push({ method: 'GET', path, options });
            return responses.get(path);
        },
        async post(path, json, options = {}) {
            calls.push({ method: 'POST', path, json, options });
            if (path === '/api/projects') return { name: json.name };
            return { session: { session_id: '存档 B', revision: 0 } };
        },
    };
    const projects = createProjectService(client, endpoints);
    const listedProjects = await projects.list();
    assert.deepEqual(listedProjects, ['项目 A']);
    assert.notStrictEqual(listedProjects, responses.get('/api/projects').projects);
    await projects.create('  项目 B  ');
    const saves = createSaveService(client, endpoints);
    assert.deepEqual(await saves.list('项目 A'), [{ session_id: '存档 A' }]);
    assert.deepEqual(await saves.get('项目 A', '存档 A'), { session_id: '存档 A', revision: 3 });
    assert.deepEqual(await saves.create('项目 A', '存档 B'), { session_id: '存档 B', revision: 0 });
    assert.deepEqual(await saves.exportJson('项目 A', '存档 A'), { json_str: '{"ok":true}' });
    assert.deepEqual(await saves.importJson('项目 A', '{"name":"导入"}', '导入存档'), {
        session_id: '存档 B', revision: 0,
    });

    assert.deepEqual(calls.map(call => [call.method, call.path]), [
        ['GET', '/api/projects'],
        ['POST', '/api/projects'],
        ['GET', '/api/sessions?project=%E9%A1%B9%E7%9B%AE%20A'],
        ['GET', '/api/session?project=%E9%A1%B9%E7%9B%AE%20A&save=%E5%AD%98%E6%A1%A3%20A'],
        ['POST', '/api/session/create'],
        ['GET', '/api/session/export?project=%E9%A1%B9%E7%9B%AE%20A&save=%E5%AD%98%E6%A1%A3%20A'],
        ['POST', '/api/session/import'],
    ]);
    assert.deepEqual(calls[1].json, { name: '项目 B' });
    assert.deepEqual(calls[4].json, { project: '项目 A', name: '存档 B' });
    assert.deepEqual(calls[6].json, {
        project: '项目 A', json_str: '{"name":"导入"}', name: '导入存档',
    });
    for (const call of calls.filter(item => item.options.schema)) {
        const response = call.method === 'GET'
            ? responses.get(call.path)
            : (call.path === '/api/projects'
                ? { name: '项目 B' }
                : { session: { session_id: '存档 B', revision: 0 } });
        assert.equal(call.options.schema(response), true, call.path);
    }
});

test('摘要与 Prompt 服务精确代理 sessionWrite 和 ApiClient', async () => {
    const { createSummaryService, summaryPatchFromForm } = await loadModule('summaries');
    const { createPromptService } = await loadModule('prompt-editor');
    const documentRef = new FakeDocument();
    const form = documentRef.createElement('form');
    for (const [field, value] of [
        ['text', '  摘要正文  '],
        ['time', ' 第三日 '],
        ['facts', '事实一\n\n事实二 '],
        ['relations', '甲→乙\n '],
    ]) {
        const input = documentRef.createElement('textarea');
        input.dataset.field = field;
        input.value = value;
        form.appendChild(input);
    }
    const patch = summaryPatchFromForm(form);
    assert.deepEqual(patch, {
        text: '摘要正文', time: '第三日', facts: ['事实一', '事实二'], relations: ['甲→乙'],
    });

    const writes = [];
    const summary = createSummaryService((...args) => {
        writes.push(args);
        return Promise.resolve({ revision: 8 });
    }, { summary: '/api/summary', summaryRegen: '/api/summary/regenerate' });
    const ref = { project: '项目 A', save: '存档 A', epoch: 2 };
    const summaryId = '797f1fe4-0e9a-4b95-b7ff-e729df22e4aa';
    await summary.update(ref, summaryId, patch);
    await summary.regenerate(ref, summaryId);
    assert.throws(() => summary.regenerate(ref), /稳定 summary_id/);
    assert.deepEqual(writes, [
        ['/api/summary', 'PATCH', { project: '项目 A', save: '存档 A', summary_id: summaryId, ...patch }, '编辑摘要'],
        ['/api/summary/regenerate', 'POST', { project: '项目 A', save: '存档 A', summary_id: summaryId }, '重生成摘要'],
    ]);

    const calls = [];
    const promptBody = { system: 'system text', group_chat: 'group text', summary: 'summary text' };
    const promptClient = {
        async get(path, options) {
            calls.push(['GET', path]);
            assert.equal(options.schema(promptBody), true);
            return promptBody;
        },
        async put(path, json) {
            calls.push(['PUT', path, json]);
            return { ok: true };
        },
        async post(path) {
            calls.push(['POST', path]);
            return { ok: true };
        },
    };
    const prompts = createPromptService(promptClient, {
        prompts: '/api/prompts',
        promptSave: name => `/api/prompts/${name}`,
        promptReset: name => `/api/prompts/${name}/reset`,
    });
    assert.strictEqual(await prompts.load(), promptBody);
    await prompts.save('summary', 'new summary text');
    assert.strictEqual(await prompts.reset('summary'), promptBody);
    assert.deepEqual(calls, [
        ['GET', '/api/prompts'],
        ['PUT', '/api/prompts/summary', { content: 'new summary text' }],
        ['POST', '/api/prompts/summary/reset'],
        ['GET', '/api/prompts'],
    ]);
});
