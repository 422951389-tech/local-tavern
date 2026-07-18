'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');
const { pathToFileURL } = require('node:url');

const { createTurnState, reduceTurnEvent, turnLocksSession } = require('../web/turn-client.js');

function moduleUrl(name) {
    return `${pathToFileURL(path.join(__dirname, '..', 'web', `${name}.mjs`)).href}?test=${Date.now()}-${Math.random()}`;
}

class FakeClassList {
    constructor(value = '') { this.values = new Set(String(value).split(/\s+/).filter(Boolean)); }
    add(...values) { values.forEach(value => this.values.add(value)); }
    remove(...values) { values.forEach(value => this.values.delete(value)); }
    contains(value) { return this.values.has(value); }
    toggle(value, force) {
        const next = force === undefined ? !this.contains(value) : Boolean(force);
        if (next) this.add(value); else this.remove(value);
        return next;
    }
}

class FakeElement {
    constructor(documentRef, className = '') {
        this.ownerDocument = documentRef;
        this.classList = new FakeClassList(className);
        this.attributes = new Map();
        this.listeners = new Map();
        this.children = [];
        this.dataset = {};
        this.parentElement = null;
        this.id = '';
        this.focusCount = 0;
        this.textContent = '';
        this.type = '';
        this.checked = false;
    }
    set className(value) { this.classList = new FakeClassList(value); }
    get className() { return [...this.classList.values].join(' '); }
    addEventListener(type, listener) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(listener);
    }
    removeEventListener(type, listener) {
        this.listeners.set(type, (this.listeners.get(type) || []).filter(item => item !== listener));
    }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }
    append(...children) { children.forEach(child => this.appendChild(child)); }
    contains(candidate) {
        return candidate === this || this.children.some(child => child.contains(candidate));
    }
    matches(selector) {
        if (selector === '.option') return this.classList.contains('option');
        if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
        return false;
    }
    closest(selector) {
        let current = this;
        while (current) {
            if (current.matches(selector)) return current;
            current = current.parentElement;
        }
        return null;
    }
    querySelectorAll(selector) {
        const matches = [];
        for (const child of this.children) {
            if (child.matches(selector)) matches.push(child);
            matches.push(...child.querySelectorAll(selector));
        }
        return matches;
    }
    focus() {
        this.focusCount += 1;
        this.ownerDocument.activeElement = this;
    }
    dispatch(type, init = {}) {
        const event = {
            type,
            target: init.target || this,
            key: init.key,
            shiftKey: Boolean(init.shiftKey),
            defaultPrevented: false,
            propagationStopped: false,
            preventDefault() { this.defaultPrevented = true; },
            stopPropagation() { this.propagationStopped = true; },
        };
        for (const listener of this.listeners.get(type) || []) listener(event);
        return event;
    }
}

class FakeFragment {
    constructor() { this.children = []; }
    appendChild(node) { this.children.push(node); return node; }
}

class FakeDocument {
    constructor() { this.activeElement = null; this.listeners = new Map(); }
    createElement() { return new FakeElement(this); }
    createDocumentFragment() { return new FakeFragment(); }
    addEventListener(type, listener) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(listener);
    }
    removeEventListener(type, listener) {
        this.listeners.set(type, (this.listeners.get(type) || []).filter(item => item !== listener));
    }
    dispatch(type, init = {}) {
        const event = { type, target: init.target || null, ...init };
        for (const listener of this.listeners.get(type) || []) listener(event);
        return event;
    }
}

test('100 个 content chunk 同步归并 reducer/cursor，DOM 每帧只提交最后快照', async () => {
    const { createFrameRenderer } = await import(moduleUrl('frame-renderer'));
    const scheduled = new Map();
    let nextFrame = 0;
    const renders = [];
    const renderer = createFrameRenderer({
        requestFrame(callback) { const id = ++nextFrame; scheduled.set(id, callback); return id; },
        cancelFrame(id) { scheduled.delete(id); },
        render(snapshot) { renders.push(snapshot); },
    });
    let state = createTurnState({
        turn_id: 'turn-a', project: 'project-a', save: 'save-a', status: 'pending',
        content: '', thinking: '', error: null, last_event_id: 0,
        events_url: '/events', cancel_url: '/cancel',
    }, { project: 'project-a', save: 'save-a', epoch: 1 });
    for (let id = 1; id <= 100; id += 1) {
        state = reduceTurnEvent(state, { id, type: 'content', content: '字' }).state;
        renderer.enqueue({ content: state.content, cursor: state.lastEventId });
    }
    assert.equal(state.content.length, 100);
    assert.equal(state.lastEventId, 100);
    assert.equal(scheduled.size, 1);
    [...scheduled.values()][0]();
    assert.deepEqual(renders, [{ content: '字'.repeat(100), cursor: 100 }]);
});

test('帧渲染器 flush 同步提交，cancel 与 current guard 拦截旧帧', async () => {
    const { createFrameRenderer } = await import(moduleUrl('frame-renderer'));
    const callbacks = [];
    let current = 'turn-a';
    const renders = [];
    const renderer = createFrameRenderer({
        requestFrame(callback) { callbacks.push(callback); return callbacks.length; },
        cancelFrame() {},
        isCurrent: snapshot => snapshot.turnId === current,
        render: snapshot => renders.push(snapshot.value),
    });
    renderer.enqueue({ turnId: 'turn-a', value: 'flush' });
    assert.equal(renderer.flush(), true);
    callbacks[0]();
    assert.deepEqual(renders, ['flush']);

    renderer.enqueue({ turnId: 'turn-a', value: 'cancelled' });
    renderer.cancel();
    callbacks[1]();
    assert.deepEqual(renders, ['flush']);

    renderer.enqueue({ turnId: 'turn-a', value: 'stale' });
    current = 'turn-b';
    assert.equal(renderer.flush(), false);
    assert.deepEqual(renders, ['flush']);
});

test('生成中与终态刷新失败都保持同一 SessionRef 写锁', () => {
    const ref = { project: 'project-a', save: 'save-a', epoch: 1 };
    const otherRef = { project: 'project-a', save: 'save-b', epoch: 2 };
    assert.equal(turnLocksSession({ ref, terminal: false }, ref), true);
    assert.equal(turnLocksSession({ ref, terminal: true, syncPending: true }, ref), true);
    assert.equal(turnLocksSession({ ref, terminal: true, syncPending: false }, ref), false);
    assert.equal(turnLocksSession({ ref, terminal: false }, otherRef), false);
});

test('历史消息通过一个 DocumentFragment 单次 replaceChildren 挂载并保留 message id', async () => {
    const { createMessageElement, mountMessageHistory } = await import(moduleUrl('render'));
    const documentRef = new FakeDocument();
    const container = new FakeElement(documentRef);
    let replaceCount = 0;
    container.replaceChildren = fragment => {
        replaceCount += 1;
        container.children = [...fragment.children];
    };
    const history = [
        { id: 'message-a', role: 'user', content: '甲' },
        { id: 'message-b', role: 'assistant', content: '乙' },
    ];
    const mounted = mountMessageHistory(documentRef, container, history, message => (
        createMessageElement(documentRef, {
            role: message.role,
            content: message.content,
            message,
        }).container
    ));
    assert.equal(mounted, 2);
    assert.equal(replaceCount, 1);
    assert.deepEqual(container.children.map(node => node.dataset.messageId), ['message-a', 'message-b']);
});

test('项目统计严格校验并只请求一次聚合端点', async () => {
    const { createProjectService, validateProjectStatsResponse } = await import(moduleUrl('projects'));
    const payload = {
        stats: [
            { project: '项目 A', characters: 2, worldbook: 3, saves: 4, status: 'ready', errors: [] },
            {
                project: '项目 B', characters: 0, worldbook: 1, saves: 1,
                status: 'partial', errors: ['characters_unavailable'],
            },
        ],
    };
    const calls = [];
    const client = {
        async get(url, options) {
            calls.push(url);
            assert.equal(options.schema(payload), true);
            return payload;
        },
        async post() { return {}; },
    };
    const service = createProjectService(client, {
        projects: '/api/projects',
        projectStats: '/api/projects/stats',
    });
    const stats = await service.loadStats(['项目 A', '项目 B']);
    assert.deepEqual(calls, ['/api/projects/stats']);
    assert.equal(stats['项目 A'].characters, 2);
    assert.equal(stats['项目 B'].errors[0], 'characters_unavailable');
    assert.notEqual(validateProjectStatsResponse({
        stats: [{ ...payload.stats[0], extra: true }],
    }), true);
    assert.notEqual(validateProjectStatsResponse({
        stats: [{ ...payload.stats[1], errors: ['internal_exception'] }],
    }), true);
    assert.notEqual(validateProjectStatsResponse({
        stats: [{ ...payload.stats[1], characters: 1 }],
    }), true);
    assert.notEqual(validateProjectStatsResponse({
        stats: [{ ...payload.stats[1], errors: ['characters_unavailable', 'characters_unavailable'] }],
    }), true);
});

test('parsed 新协议只保留白名单且 delta 不会伪装完整 Session，旧事件单独保留 legacySession', () => {
    const baseTurn = {
        turn_id: 'turn-a', project: 'project-a', save: 'save-a', status: 'pending',
        content: '', thinking: '', error: null, last_event_id: 0,
        events_url: '/events', cancel_url: '/cancel',
    };
    const ref = { project: 'project-a', save: 'save-a', epoch: 1 };
    const modern = reduceTurnEvent(createTurnState(baseTurn, ref), {
        id: 1,
        type: 'parsed',
        parsed: { narration: '正文' },
        roleplay_warnings: [],
        revision: 7,
        session_delta: { scene_meta: { location: '酒馆' } },
        session: { session_id: '不得使用', revision: 999 },
        unexpected: 'drop-me',
    }).state.parsed;
    assert.deepEqual(Object.keys(modern).sort(), [
        'legacySession', 'parsed', 'revision', 'roleplay_warnings', 'session_delta',
    ]);
    assert.equal(modern.legacySession, null);
    assert.equal(modern.revision, 7);
    assert.deepEqual(modern.session_delta, { scene_meta: { location: '酒馆' } });
    assert.equal(Object.hasOwn(modern, 'session'), false);
    assert.equal(Object.hasOwn(modern, 'unexpected'), false);

    const legacySession = { session_id: 'save-a', revision: 6 };
    const legacy = reduceTurnEvent(createTurnState(baseTurn, ref), {
        id: 1, type: 'parsed', parsed: {}, session: legacySession,
    }).state.parsed;
    assert.strictEqual(legacy.legacySession, legacySession);
    assert.deepEqual(legacy.roleplay_warnings, []);
    assert.throws(() => reduceTurnEvent(createTurnState(baseTurn, ref), {
        id: 1, type: 'parsed', parsed: {}, revision: 8, session_delta: {},
    }), /结构无效/);
    assert.throws(() => reduceTurnEvent(createTurnState(baseTurn, ref), {
        id: 1, type: 'parsed', parsed: {}, roleplay_warnings: [], revision: 8,
    }), /delta\/revision/);
});

test('listbox 支持方向键、Home/End、Enter/Space、Escape、Tab 和焦点恢复', async () => {
    const { clampAnchoredLeft, createListboxController } = await import(moduleUrl('listbox'));
    assert.equal(clampAnchoredLeft(490, 240, 600), 344);
    assert.equal(clampAnchoredLeft(8, 240, 600), 16);
    assert.equal(clampAnchoredLeft(200, 240, 600), 200);
    const documentRef = new FakeDocument();
    const trigger = new FakeElement(documentRef);
    trigger.id = 'project-btn';
    const panel = new FakeElement(documentRef, 'hidden');
    const listbox = new FakeElement(documentRef);
    listbox.id = 'project-list';
    const options = ['A', 'B', 'C'].map(value => {
        const option = new FakeElement(documentRef, `option${value === 'B' ? ' active' : ''}`);
        option.dataset.value = value;
        listbox.appendChild(option);
        return option;
    });
    const actions = ['new', 'rename'].map(value => {
        const action = new FakeElement(documentRef, 'action');
        action.dataset.value = value;
        panel.appendChild(action);
        return action;
    });
    const selected = [];
    const controller = createListboxController({
        documentRef,
        trigger,
        panel,
        listbox,
        optionSelector: '.option',
        actionSelector: '.action',
        onSelect: option => selected.push(option.dataset.value),
    });
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');
    assert.deepEqual(options.map(option => option.getAttribute('aria-selected')), ['false', 'true', 'false']);

    trigger.dispatch('keydown', { key: 'ArrowDown' });
    assert.equal(controller.isOpen(), true);
    assert.strictEqual(documentRef.activeElement, options[0]);
    listbox.dispatch('keydown', { key: 'End' });
    assert.strictEqual(documentRef.activeElement, options[2]);
    listbox.dispatch('keydown', { key: 'Home' });
    assert.strictEqual(documentRef.activeElement, options[0]);
    listbox.dispatch('keydown', { key: 'ArrowDown' });
    assert.strictEqual(documentRef.activeElement, options[1]);
    listbox.dispatch('keydown', { key: 'Enter' });
    assert.deepEqual(selected, ['B']);
    assert.strictEqual(documentRef.activeElement, trigger);
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');

    trigger.dispatch('keydown', { key: ' ' });
    assert.strictEqual(documentRef.activeElement, options[1]);
    listbox.dispatch('keydown', { key: 'Escape' });
    assert.equal(controller.isOpen(), false);
    assert.strictEqual(documentRef.activeElement, trigger);

    trigger.dispatch('click');
    trigger.dispatch('keydown', { key: 'Tab' });
    assert.equal(controller.isOpen(), true);
    assert.strictEqual(documentRef.activeElement, options[1]);
    listbox.dispatch('keydown', { key: 'Tab' });
    assert.strictEqual(documentRef.activeElement, actions[0]);
    panel.dispatch('keydown', { key: 'Tab', shiftKey: true, target: actions[0] });
    assert.strictEqual(documentRef.activeElement, options[1]);
    listbox.dispatch('keydown', { key: 'Tab' });
    actions[1].focus();
    const outside = new FakeElement(documentRef);
    outside.focus();
    documentRef.dispatch('focusin', { target: outside });
    assert.equal(controller.isOpen(), false);

    trigger.dispatch('keydown', { key: 'ArrowUp' });
    assert.strictEqual(documentRef.activeElement, options[2]);
    listbox.dispatch('keydown', { key: 'Tab' });
    assert.equal(controller.isOpen(), true);
    assert.strictEqual(documentRef.activeElement, actions[0]);
    panel.dispatch('keydown', { key: 'Escape', target: actions[0] });
    assert.equal(controller.isOpen(), false);
    assert.strictEqual(documentRef.activeElement, trigger);
});
