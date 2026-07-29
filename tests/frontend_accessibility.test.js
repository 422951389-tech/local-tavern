'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const ROOT = path.resolve(__dirname, '..');
const INDEX = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
const STYLE = fs.readFileSync(path.join(ROOT, 'web', 'style.css'), 'utf8');
const APP = fs.readFileSync(path.join(ROOT, 'web', 'app.mjs'), 'utf8');

function tagById(id) {
    const escaped = id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = INDEX.match(new RegExp(`<[^>]+\\bid=["']${escaped}["'][^>]*>`, 'i'));
    assert.ok(match, `缺少 #${id}`);
    return match[0];
}

function assertAttribute(tag, name, value = null) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (value === null) {
        assert.match(tag, new RegExp(`\\s${escaped}(?:\\s|=|>)`, 'i'));
        return;
    }
    assert.match(tag, new RegExp(`\\s${escaped}=["']${value}["']`, 'i'));
}

function relativeLuminance(hex) {
    const channels = hex.slice(1).match(/../g).map(part => parseInt(part, 16) / 255);
    const linear = channels.map(value => (
        value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
    ));
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrastRatio(first, second) {
    const values = [relativeLuminance(first), relativeLuminance(second)].sort((a, b) => b - a);
    return (values[0] + 0.05) / (values[1] + 0.05);
}

test('静态顶栏、下拉、输入和 Thinking 控件具有稳定可访问名称与关系', () => {
    const projectButton = tagById('project-btn');
    assertAttribute(projectButton, 'aria-label', '切换项目（世界观）');
    assertAttribute(projectButton, 'aria-haspopup', 'listbox');
    assertAttribute(projectButton, 'aria-controls', 'project-list');
    assertAttribute(projectButton, 'aria-expanded', 'false');

    const saveButton = tagById('tab-saves');
    assertAttribute(saveButton, 'aria-label', '切换存档');
    assertAttribute(saveButton, 'aria-haspopup', 'listbox');
    assertAttribute(saveButton, 'aria-controls', 'save-list');
    assertAttribute(saveButton, 'aria-expanded', 'false');

    for (const [id, label] of [
        ['reset-btn', '重置当前存档内容'],
        ['model-params-btn', '调整 AI 参数'],
        ['history-btn', '查看历史快照'],
        ['search-btn', '全局剧情搜索'],
        ['prompts-btn', '编辑提示词'],
        ['send-cancel-btn', '取消生成'],
        ['thinking-close', '收起 AI 思考面板'],
    ]) {
        assertAttribute(tagById(id), 'aria-label', label);
    }

    assertAttribute(tagById('model-select'), 'aria-label', '对话模型');
    assertAttribute(tagById('user-input'), 'aria-label', '对话输入');
    assertAttribute(tagById('project-list'), 'role', 'listbox');
    assertAttribute(tagById('save-list'), 'role', 'listbox');
    assertAttribute(tagById('thinking-panel'), 'role', 'region');
    assertAttribute(tagById('modal'), 'tabindex', '-1');
    assertAttribute(tagById('search-btn'), 'aria-haspopup', 'dialog');
    assertAttribute(tagById('search-btn'), 'aria-controls', 'modal');
});

test('CSS 固定 4.5:1 文本对比、44px 触控、焦点和 reduced-motion 契约', () => {
    const faint = STYLE.match(/--text-faint:\s*(#[0-9a-f]{6})/i);
    assert.ok(faint, '缺少 --text-faint');
    assert.ok(contrastRatio(faint[1], '#2a2a2a') >= 4.5, '--text-faint 在输入背景上不足 4.5:1');

    assert.match(STYLE, /height:\s*100dvh/);
    assert.match(STYLE, /min-height:\s*44px/);
    assert.match(STYLE, /:focus-visible/);
    assert.match(STYLE, /\.msg:focus-within\s+\.msg-actions/);
    assert.match(STYLE, /\.msg:focus-within\s+\.msg-include-control/);
    assert.match(STYLE, /@media\s*\(hover:\s*none\),\s*\(pointer:\s*coarse\)/);
    assert.match(STYLE, /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*animation-duration:\s*0\.01ms\s*!important/);
});

test('Modal、卡片编辑器、下拉与 Thinking 具有窄屏边界契约', () => {
    assert.match(STYLE, /width:\s*min\(720px,\s*calc\(100vw\s*-\s*32px\)\)/);
    assert.match(STYLE, /max-height:\s*min\(80dvh,\s*calc\(100dvh\s*-\s*32px\)\)/);
    assert.match(STYLE, /@media\s*\(max-width:\s*600px\)[\s\S]*\.modal-footer\s*\{[\s\S]*flex-direction:\s*column/);
    assert.match(STYLE, /@media\s*\(max-width:\s*600px\)[\s\S]*\.cards-panel,[\s\S]*\.cards-panel-inner[\s\S]*flex-direction:\s*column/);
    assert.match(STYLE, /@media\s*\(max-width:\s*375px\)[\s\S]*\.dropdown-panel\s*\{[\s\S]*left:\s*16px\s*!important/);
    assert.match(STYLE, /@media\s*\(max-width:\s*375px\)[\s\S]*\.dropdown-panel\s*\{[\s\S]*bottom:\s*16px/);
    assert.match(STYLE, /@media\s*\(max-width:\s*375px\)[\s\S]*\.thinking-panel\s*\{[\s\S]*left:\s*16px/);
    assert.match(STYLE, /\.worldbook-editor\s*\{[\s\S]*width:\s*100%[\s\S]*max-width:\s*100%/);
    assert.match(APP, /const row = domElement\('button', 'card-row'\)/);
    assert.match(APP, /row\.type = 'button'/);
    assert.match(APP, /syncCardListSelection\(\);\s*renderForm\(\);/);
    assert.match(STYLE, /\.chat-stream\s*\{[\s\S]*?overflow-x:\s*hidden/);
    assert.match(STYLE, /\.msg \.content,[\s\S]*?overflow-wrap:\s*anywhere/);
});

test('动态表单、历史恢复和卡片编辑器具有标签、状态与单次提交契约', () => {
    assert.match(APP, /function labeledModalTextInput\(/);
    assert.match(APP, /label\.htmlFor = id/);
    assert.match(APP, /label\.htmlFor = inp\.id/);
    assert.match(APP, /status\.setAttribute\('aria-live', 'polite'\)/);
    assert.match(APP, /function setCardPending\(/);
    assert.match(APP, /modalController\.setPending\(cardPending\)/);
    assert.match(APP, /if \(restorePending \|\| !isMounted\(\)\) return/);
    assert.match(APP, /restore\.setAttribute\('aria-busy', 'true'\)/);
    assert.match(APP, /modelSwitchGate\.acquire\(\)/);
    assert.match(APP, /resetGate\.acquire\(\)/);
    assert.match(STYLE, /\.modal-body input\[type="text"\]:focus-visible/);
    assert.match(STYLE, /\.param-switch input:focus-visible \+ \.switch-slider/);
});

test('全局剧情搜索具有表单标签、实时状态、焦点和窄屏契约', () => {
    assert.match(APP, /queryLabel\.htmlFor = 'global-search-query'/);
    assert.match(APP, /query\.maxLength = 128/);
    assert.match(APP, /query\.setAttribute\('aria-describedby', 'global-search-status'\)/);
    assert.match(APP, /scopeLabel\.htmlFor = 'global-search-scope'/);
    assert.match(APP, /status\.setAttribute\('role', 'status'\)/);
    assert.match(APP, /status\.setAttribute\('aria-live', 'polite'\)/);
    assert.match(APP, /searchNavigationGate\.acquire\(\)/);
    assert.match(APP, /searchNavigationGate\.release\(navigationToken\)/);
    assert.match(APP, /querySelectorAll\('button, input, select'\)/);
    assert.match(APP, /modalController\.setPending\(true\)/);
    assert.match(APP, /results\.setAttribute\('aria-label', '搜索结果'\)/);
    assert.match(STYLE, /\.search-form[\s\S]*?min-height:\s*44px/);
    assert.match(STYLE, /\.search-result:focus-visible/);
    assert.match(STYLE, /@media\s*\(max-width:\s*600px\)[\s\S]*?\.search-form\s*\{[\s\S]*?grid-template-columns:\s*1fr/);
    assert.match(STYLE, /\.search-result-snippet\s*\{[\s\S]*?overflow-wrap:\s*anywhere/);
});

class FakeClassList {
    constructor() { this.values = new Set(); }
    add(...names) { names.forEach(name => this.values.add(name)); }
    remove(...names) { names.forEach(name => this.values.delete(name)); }
    contains(name) { return this.values.has(name); }
    toggle(name, force) {
        const enabled = force === undefined ? !this.contains(name) : Boolean(force);
        if (enabled) this.add(name);
        else this.remove(name);
        return enabled;
    }
}

class FakeElement {
    constructor(tagName, documentRef) {
        this.tagName = tagName.toUpperCase();
        this.ownerDocument = documentRef;
        this.classList = new FakeClassList();
        this.children = [];
        this.parentNode = null;
        this.attributes = new Map();
        this.listeners = new Map();
        this.dataset = {};
        this.textContent = '';
        this.disabled = false;
        this.hidden = false;
        this.type = '';
        this.focusCount = 0;
        this._innerHTML = '';
    }

    set innerHTML(value) { this._innerHTML = String(value); }
    get innerHTML() { return this._innerHTML; }

    appendChild(child) {
        if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(item => item !== child);
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    replaceChildren(...children) {
        this.children.forEach(child => { child.parentNode = null; });
        this.children = [];
        children.forEach(child => this.appendChild(child));
    }

    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
    addEventListener(type, listener) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(listener);
    }

    focus() {
        this.focusCount += 1;
        this.ownerDocument.activeElement = this;
    }

    contains(target) {
        for (let current = target; current; current = current.parentNode) {
            if (current === this) return true;
        }
        return false;
    }

    closest(selector) {
        if (!selector.includes('.hidden')) return null;
        for (let current = this; current; current = current.parentNode) {
            if (current.hidden || current.classList.contains('hidden')) return current;
        }
        return null;
    }

    isFocusable() {
        if (this.disabled || this.hidden || this.type === 'hidden') return false;
        if (this.getAttribute('tabindex') === '-1' || this.getAttribute('aria-hidden') === 'true') return false;
        return ['A', 'BUTTON', 'INPUT', 'TEXTAREA', 'SELECT'].includes(this.tagName)
            || this.getAttribute('tabindex') !== null
            || this.getAttribute('contenteditable') === 'true';
    }

    querySelectorAll() {
        const found = [];
        const visit = element => {
            for (const child of element.children) {
                if (child.isFocusable()) found.push(child);
                visit(child);
            }
        };
        visit(this);
        return found;
    }

    querySelector() { return this.querySelectorAll()[0] || null; }
}

class FakeDocument {
    constructor() {
        this.activeElement = null;
        this.listeners = new Map();
    }

    createElement(tagName) { return new FakeElement(tagName, this); }
    addEventListener(type, listener) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(listener);
    }

    dispatch(type, init = {}) {
        const event = {
            type,
            key: '',
            shiftKey: false,
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
    const launcher = documentRef.createElement('button');
    launcher.focus();
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
    elements.footer.classList.add('hidden');
    elements.error.classList.add('hidden');
    elements.backdrop.appendChild(elements.dialog);
    elements.dialog.appendChild(elements.closeButton);
    elements.dialog.appendChild(elements.title);
    elements.dialog.appendChild(elements.body);
    elements.dialog.appendChild(elements.error);
    elements.dialog.appendChild(elements.footer);
    elements.footer.appendChild(elements.cancelButton);
    elements.footer.appendChild(elements.confirmButton);
    return { documentRef, launcher, elements };
}

test('Modal 圈闭 Tab/Shift+Tab，并在替换后恢复最初触发点', async () => {
    const { createModalController } = await import('../web/modal.mjs');
    const { documentRef, launcher, elements } = modalFixture();
    const controller = createModalController(elements, { documentRef });
    controller.bind();

    const firstInput = documentRef.createElement('input');
    controller.show({
        title: '第一个对话框',
        body: firstInput,
        footer: { onConfirm: async () => true },
    });
    assert.equal(documentRef.activeElement, firstInput, '初始焦点应进入第一个表单控件');

    elements.confirmButton.focus();
    const forward = documentRef.dispatch('keydown', { key: 'Tab' });
    assert.equal(forward.defaultPrevented, true);
    assert.equal(documentRef.activeElement, elements.closeButton, '末尾 Tab 应回到首个控件');

    elements.closeButton.focus();
    const backward = documentRef.dispatch('keydown', { key: 'Tab', shiftKey: true });
    assert.equal(backward.defaultPrevented, true);
    assert.equal(documentRef.activeElement, elements.confirmButton, '首位 Shift+Tab 应回到末尾控件');

    const replacementInput = documentRef.createElement('textarea');
    elements.body.scrollTop = 240;
    assert.equal(controller.show({
        title: '替换后的对话框',
        body: replacementInput,
        footer: { onConfirm: async () => true },
    }), true);
    assert.equal(elements.body.scrollTop, 0, '替换 Modal 必须从内容顶部开始，不能继承上一个弹窗滚动位置');
    assert.equal(documentRef.activeElement, replacementInput);
    assert.equal(controller.hide(), true);
    assert.equal(documentRef.activeElement, launcher, '替换对话框关闭后应恢复最初触发点');
    assert.equal(launcher.focusCount, 2);
});

test('Modal pending 时保持 Escape 禁止关闭语义，解除后恢复关闭与焦点', async () => {
    const { createModalController } = await import('../web/modal.mjs');
    const { documentRef, launcher, elements } = modalFixture();
    const input = documentRef.createElement('input');
    const controller = createModalController(elements, { documentRef });
    controller.bind();
    controller.show({
        title: '保存中',
        body: input,
        footer: { onConfirm: async () => true },
    });

    controller.setPending(true);
    const blockedEscape = documentRef.dispatch('keydown', { key: 'Escape' });
    assert.equal(blockedEscape.defaultPrevented, true);
    assert.equal(elements.backdrop.classList.contains('hidden'), false);
    assert.equal(documentRef.activeElement, input);

    controller.setPending(false);
    const closingEscape = documentRef.dispatch('keydown', { key: 'Escape' });
    assert.equal(closingEscape.defaultPrevented, true);
    assert.equal(elements.backdrop.classList.contains('hidden'), true);
    assert.equal(documentRef.activeElement, launcher);
});
