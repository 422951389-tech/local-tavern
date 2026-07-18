'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const { canPerformAction } = require('../web/turn-client.js');

async function loadRoleplay() {
    return import('../web/roleplay.mjs');
}

async function loadCardEditor() {
    return import('../web/card-editor.mjs');
}

class FakeClassList {
    constructor(owner) {
        this.owner = owner;
        this.values = new Set();
    }
    add(...names) {
        names.filter(Boolean).forEach(name => this.values.add(name));
        this.owner._className = [...this.values].join(' ');
    }
    contains(name) { return this.values.has(name); }
}

class FakeElement {
    constructor(tagName) {
        this.tagName = tagName.toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.dataset = {};
        this.attributes = {};
        this.listeners = {};
        this.classList = new FakeClassList(this);
        this._className = '';
        this.textContent = '';
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.isConnected = true;
        this.focused = false;
    }
    set className(value) {
        this._className = String(value || '');
        this.classList.values = new Set(this._className.split(/\s+/).filter(Boolean));
    }
    get className() { return this._className; }
    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }
    replaceChildren(...children) {
        this.children.forEach(child => { child.parentNode = null; child.isConnected = false; });
        this.children = [];
        children.forEach(child => this.appendChild(child));
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(type, listener) { this.listeners[type] = listener; }
    dispatch(type) {
        const listener = this.listeners[type];
        return listener ? listener({ currentTarget: this, target: this }) : undefined;
    }
    focus() { this.focused = true; }
}

class FakeDocument {
    createElement(tagName) { return new FakeElement(tagName); }
}

function sessionFixture(count = 1, overrides = {}) {
    const characters_state = {};
    for (let index = 0; index < count; index += 1) {
        characters_state[`char-${index + 1}`] = {
            name: `角色 ${index + 1}`,
            affinity: index * 17,
            mood: index === 0 ? '专注' : '',
            remaining_silent_turns: index,
        };
    }
    return {
        project: '项目 A',
        session_id: '存档 A',
        revision: 7,
        characters_state,
        roleplay_policy: { strict_muted_writeback: false },
        ...overrides,
    };
}

test('roleplay session normalization supports 0/1/N and rejects invalid strict values', async () => {
    const { normalizeRoleplaySession, normalizeRemainingSilentTurns } = await loadRoleplay();
    assert.equal(normalizeRoleplaySession(sessionFixture(0)).characters.length, 0);
    assert.equal(normalizeRoleplaySession(sessionFixture(1)).characters.length, 1);
    assert.equal(normalizeRoleplaySession(sessionFixture(4)).characters.length, 4);
    assert.equal(normalizeRoleplaySession({ characters_state: { old: { name: '旧角色' } } }).characters[0].remainingSilentTurns, 0);
    assert.equal(normalizeRoleplaySession({}).strictMutedWriteback, false);
    for (const invalid of [-1, 1000, 1.5, 'abc']) {
        assert.throws(
            () => normalizeRemainingSilentTurns(invalid, { strict: true }),
            /0 到 999 的整数/,
        );
    }
});

test('roleplay service delegates exact PATCH payloads to sessionWrite CAS', async () => {
    const { createRoleplayService } = await loadRoleplay();
    const calls = [];
    const service = createRoleplayService(async (...args) => {
        calls.push(args);
        return { session: sessionFixture(1) };
    }, {
        roleplaySilence: id => `/api/session/characters/${encodeURIComponent(id)}/silence`,
        roleplayPolicy: '/api/session/roleplay-policy',
    });
    const ref = { project: '项目 A', save: '存档 A', epoch: 2 };

    await service.saveSilence(ref, 'char / A', '9');
    await service.savePolicy(ref, true);

    assert.deepEqual(calls, [
        [
            '/api/session/characters/char%20%2F%20A/silence',
            'PATCH',
            { project: '项目 A', save: '存档 A', remaining_silent_turns: 9 },
            '保存角色「char / A」禁言轮次',
        ],
        [
            '/api/session/roleplay-policy',
            'PATCH',
            { project: '项目 A', save: '存档 A', strict_muted_writeback: true },
            '保存严格禁言写回设置',
        ],
    ]);
    assert.throws(() => service.savePolicy(ref, 1), /必须是布尔值/);
});

test('roleplay panel renders 0/1/N controls with explicit labels and strict policy', async () => {
    const { createRoleplayPanel } = await loadRoleplay();
    const documentRef = new FakeDocument();
    const service = { saveSilence: async () => ({}), savePolicy: async () => ({}) };
    const ref = { project: '项目 A', save: '存档 A', epoch: 2 };

    for (const count of [0, 1, 3]) {
        const container = new FakeElement('div');
        const panel = createRoleplayPanel({ documentRef, container, session: sessionFixture(count), sessionRef: ref, service });
        assert.equal(panel.elements.characters.size, count);
        assert.equal(container.children[0], panel.root);
        assert.equal(panel.elements.policy.label.htmlFor, panel.elements.policy.checkbox.id);
        for (const controls of panel.elements.characters.values()) {
            assert.equal(controls.label.htmlFor, controls.input.id);
            assert.equal(controls.input.min, '0');
            assert.equal(controls.input.max, '999');
            assert.equal(controls.input.step, '1');
        }
    }
});

test('roleplay panel preserves invalid input, restores focus, and locks an in-flight response race', async () => {
    const { createRoleplayPanel } = await loadRoleplay();
    const documentRef = new FakeDocument();
    const container = new FakeElement('div');
    let resolveSave;
    let saveCalls = 0;
    let turnActive = false;
    const service = {
        savePolicy: async () => ({}),
        saveSilence: async () => {
            saveCalls += 1;
            await new Promise(resolve => { resolveSave = resolve; });
            return {};
        },
    };
    const panel = createRoleplayPanel({
        documentRef,
        container,
        session: sessionFixture(1),
        sessionRef: { project: '项目 A', save: '存档 A', epoch: 2 },
        service,
        isTurnActive: () => turnActive,
    });
    const controls = panel.elements.characters.get('char-1');

    controls.input.value = '1.5';
    await controls.save.dispatch('click');
    assert.equal(saveCalls, 0);
    assert.equal(controls.input.value, '1.5');
    assert.equal(controls.input.focused, true);
    assert.match(controls.status.textContent, /整数/);

    controls.input.focused = false;
    controls.input.value = '5';
    const pending = controls.save.dispatch('click');
    await Promise.resolve();
    assert.equal(controls.input.disabled, true);
    assert.equal(controls.save.disabled, true);
    turnActive = true;
    panel.setDisabled(true);
    resolveSave();
    await pending;
    assert.equal(panel.isDisabled(), true);
    assert.equal(controls.input.disabled, true);
    assert.equal(controls.save.disabled, true);
    assert.match(controls.status.textContent, /控件已锁定/);
});

test('strict policy save keeps its own draft and failure feedback in place', async () => {
    const { createRoleplayPanel } = await loadRoleplay();
    const panel = createRoleplayPanel({
        documentRef: new FakeDocument(),
        container: new FakeElement('div'),
        session: sessionFixture(0),
        sessionRef: { project: '项目 A', save: '存档 A', epoch: 2 },
        service: {
            saveSilence: async () => ({}),
            savePolicy: async () => { throw new Error('revision conflict'); },
        },
    });
    panel.elements.policy.checkbox.checked = true;
    await panel.elements.policy.save.dispatch('click');
    assert.equal(panel.elements.policy.checkbox.checked, true);
    assert.equal(panel.elements.policy.checkbox.focused, true);
    assert.match(panel.elements.policy.status.textContent, /revision conflict/);
});

test('same-save reload can recover a detached draft and error after a CAS conflict', async () => {
    const { createRoleplayPanel } = await loadRoleplay();
    const recovered = [];
    let inputToDetach = null;
    const panel = createRoleplayPanel({
        documentRef: new FakeDocument(),
        container: new FakeElement('div'),
        session: sessionFixture(1),
        sessionRef: { project: '项目 A', save: '存档 A', epoch: 2 },
        service: {
            savePolicy: async () => ({}),
            saveSilence: async () => {
                inputToDetach.isConnected = false;
                throw new Error('revision conflict');
            },
        },
        recoverDraft: draft => recovered.push(draft),
    });
    const controls = panel.elements.characters.get('char-1');
    inputToDetach = controls.input;
    controls.input.value = '8';
    await controls.save.dispatch('click');
    assert.deepEqual(recovered, [{
        kind: 'silence',
        characterId: 'char-1',
        value: '8',
        message: '保存失败：revision conflict',
    }]);
});

test('roleplay warnings whitelist fields, distinguish actions, and discard secret text', async () => {
    const { sanitizeRoleplayWarnings, formatRoleplayWarning, renderRoleplayWarnings } = await loadRoleplay();
    const source = {
        roleplay_warnings: [
            {
                code: 'muted_character_output',
                action: 'writeback_applied',
                character_id: 'alice',
                content: 'SECRET_CONTENT',
                model_name: 'SECRET_MODEL_NAME',
            },
            {
                code: 'muted_character_output',
                action: 'writeback_skipped',
                character_id: 'bob',
                raw_output: 'SECRET_RAW_OUTPUT',
            },
            {
                code: 'ambiguous_character_identity',
                action: 'unresolved_skipped',
                candidate_ids: ['z', 'a', 'a'],
                original_name: 'SECRET_ORIGINAL_NAME',
            },
            { code: 'unknown_character_identity', action: 'unresolved_skipped', original_name: 'SECRET_UNKNOWN' },
            { code: 'not_whitelisted', action: 'writeback_applied', content: 'SECRET_UNKNOWN_CODE' },
        ],
    };
    const warnings = sanitizeRoleplayWarnings(source);
    assert.equal(warnings.length, 4);
    assert.deepEqual(warnings[2].candidateIds, ['a', 'z']);
    assert.match(formatRoleplayWarning(warnings[0]), /已警告但按宽松模式写回/);
    assert.match(formatRoleplayWarning(warnings[1]), /严格模式已跳过/);
    const serialized = JSON.stringify(warnings);
    assert.doesNotMatch(serialized, /SECRET|content|raw_output|original_name|model_name/);

    const host = new FakeElement('div');
    const rendered = renderRoleplayWarnings(new FakeDocument(), host, source);
    assert.equal(host.children[0], rendered);
    assert.equal(rendered.attributes['aria-live'], 'polite');
});

test('roleplay action is blocked during active turns', () => {
    assert.equal(canPerformAction('roleplay', 'pending'), false);
    assert.equal(canPerformAction('roleplay', 'streaming'), false);
    assert.equal(canPerformAction('roleplay', 'completed'), true);
});

test('old cards default chattiness to 50, validate integers, and preserve unknown JSON fields', async () => {
    const {
        getPathValue,
        mergeCardEditorData,
        serializeCardFields,
    } = await loadCardEditor();
    const field = { key: 'chattiness', label: '活跃度', type: 'number', default: 50, min: 0, max: 100, integer: true };
    assert.equal(getPathValue({}, 'chattiness', field), 50);
    const serialized = serializeCardFields([{
        key: 'chattiness',
        label: '活跃度',
        type: 'number',
        value: '73',
        min: 0,
        max: 100,
        integer: true,
    }], { idField: null });
    assert.equal(serialized.data.chattiness, 73);
    assert.throws(() => serializeCardFields([{
        key: 'chattiness', label: '活跃度', type: 'number', value: '1.5', min: 0, max: 100, integer: true,
    }]), /必须是整数/);
    assert.throws(() => serializeCardFields([{
        key: 'chattiness', label: '活跃度', type: 'number', value: '101', min: 0, max: 100, integer: true,
    }]), /不能大于 100/);

    const merged = mergeCardEditorData({
        id: 'alice',
        chattiness: 50,
        legacy_fact: { nested: ['keep'] },
        profile: { known: 'old', unknown: 'keep nested' },
        custom: { removed: true },
    }, {
        id: 'alice',
        chattiness: 73,
        profile: { known: 'new' },
    });
    assert.deepEqual(merged, {
        id: 'alice',
        chattiness: 73,
        legacy_fact: { nested: ['keep'] },
        profile: { known: 'new', unknown: 'keep nested' },
    });
});

test('ROLE integration uses DOM APIs, active-turn locks, accessible 44px controls, and mobile layout', () => {
    const root = path.resolve(__dirname, '..');
    const app = fs.readFileSync(path.join(root, 'web', 'app.mjs'), 'utf8');
    const roleplay = fs.readFileSync(path.join(root, 'web', 'roleplay.mjs'), 'utf8');
    const css = fs.readFileSync(path.join(root, 'web', 'style.css'), 'utf8');
    const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');

    assert.match(app, /createRoleplayPanel/);
    assert.match(app, /renderRoleplayWarnings/);
    assert.match(app, /recoverDraft:\s*draft =>/);
    assert.match(app, /'\.roleplay-write-control'/);
    assert.match(app, /加载角色卡期间已开始生成/);
    assert.match(app, /f\.key === 'chattiness'/);
    assert.doesNotMatch(roleplay, /\.innerHTML\s*=/);
    assert.match(roleplay, /label\.htmlFor = inputId/);
    assert.match(roleplay, /项目共享的“发言倾向”请从顶部“角色”编辑/);
    assert.match(roleplay, /aria-live/);
    assert.match(roleplay, /if \(isTurnActive\(\)\) \{/);
    assert.match(css, /\.roleplay-silence-input,[\s\S]*min-height:\s*44px/);
    assert.match(css, /\.roleplay-save-button:focus-visible/);
    assert.match(css, /@media \(max-width:\s*800px\)[\s\S]*\.character-panel[\s\S]*display:\s*block/);
    assert.match(css, /font-size:\s*16px/);
    assert.match(html, /角色状态与禁言/);
});
