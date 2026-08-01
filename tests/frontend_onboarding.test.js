'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

class FakeElement {
    constructor(tagName) {
        this.tagName = String(tagName).toUpperCase();
        this.className = '';
        this.textContent = '';
        this.children = [];
        this.attributes = new Map();
        this.listeners = new Map();
        this.disabled = false;
        this.isConnected = true;
        this.focusCalls = 0;
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    append(...children) {
        for (const child of children) this.appendChild(child);
    }

    setAttribute(name, value) {
        this.attributes.set(String(name), String(value));
    }

    getAttribute(name) {
        return this.attributes.get(String(name)) ?? null;
    }

    addEventListener(type, listener) {
        this.listeners.set(type, listener);
    }

    focus() {
        this.focusCalls += 1;
    }

    async click() {
        const listener = this.listeners.get('click');
        if (listener) await listener({ currentTarget: this, target: this });
    }
}

class FakeDocument {
    createElement(tagName) {
        return new FakeElement(tagName);
    }

    createElementNS(_namespace, tagName) {
        return new FakeElement(tagName);
    }
}

function descendants(root) {
    return [root, ...root.children.flatMap(descendants)];
}

function buttonByLabel(root, label) {
    const button = descendants(root).find(node => (
        node.tagName === 'BUTTON'
        && descendants(node).some(child => child.textContent === label)
    ));
    assert.ok(button, `缺少按钮：${label}`);
    return button;
}

async function onboardingFixture(showProviderSettings) {
    const { createOnboardingController } = await import('../web/onboarding-controller.mjs');
    const storage = new Map();
    const modalCalls = [];
    const hideCalls = [];
    const toasts = [];
    const controller = createOnboardingController({
        documentRef: new FakeDocument(),
        getStorage: () => ({
            getItem: key => storage.get(key) ?? null,
            setItem: (key, value) => storage.set(key, value),
        }),
        storageKey: 'onboarding-test',
        showModal: config => { modalCalls.push(config); return true; },
        hideModal: options => hideCalls.push(options),
        showToast: message => toasts.push(message),
        showProviderSettings,
    });
    assert.equal(controller.show(), true);
    assert.equal(modalCalls.length, 1);
    const body = modalCalls[0].body;
    return {
        controller,
        storage,
        modalCalls,
        hideCalls,
        toasts,
        cloud: buttonByLabel(body, '配置云端 API'),
        local: buttonByLabel(body, '使用本地模型'),
    };
}

for (const [label, open] of [
    ['返回 false', async () => false],
    ['抛出异常', async () => { throw new Error('provider modal failed'); }],
]) {
    test(`云端设置${label}时引导保持未完成并恢复按钮`, async () => {
        const fixture = await onboardingFixture(open);
        const click = fixture.cloud.click();
        assert.equal(fixture.cloud.disabled, true);
        assert.equal(fixture.local.disabled, true);
        assert.equal(fixture.cloud.getAttribute('aria-busy'), 'true');

        await click;

        assert.equal(fixture.storage.has('onboarding-test'), false);
        assert.equal(fixture.controller.seen(), false);
        assert.equal(fixture.hideCalls.length, 0);
        assert.equal(fixture.modalCalls.length, 1);
        assert.equal(fixture.cloud.disabled, false);
        assert.equal(fixture.local.disabled, false);
        assert.equal(fixture.cloud.getAttribute('aria-busy'), 'false');
        assert.equal(fixture.cloud.focusCalls, 1);
        assert.equal(fixture.toasts.length, 1);
    });
}

test('云端设置成功展示后才把引导标记为完成', async () => {
    let resolveOpen;
    const opened = new Promise(resolve => { resolveOpen = resolve; });
    const fixture = await onboardingFixture(() => opened);
    const click = fixture.cloud.click();

    assert.equal(fixture.controller.seen(), false);
    assert.equal(fixture.cloud.disabled, true);
    resolveOpen(true);
    await click;

    assert.equal(fixture.storage.get('onboarding-test'), 'complete');
    assert.equal(fixture.controller.seen(), true);
    assert.equal(fixture.hideCalls.length, 0);
    assert.equal(fixture.cloud.disabled, false);
    assert.equal(fixture.local.disabled, false);
    assert.equal(fixture.cloud.getAttribute('aria-busy'), 'false');
    assert.equal(fixture.cloud.focusCalls, 0);
});
