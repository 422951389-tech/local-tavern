'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const loadModule = () => import('../web/model-params.mjs');

class FakeElement {
    constructor(tagName) {
        this.tagName = String(tagName).toUpperCase();
        this.children = [];
        this.attributes = new Map();
        this.listeners = new Map();
        this.className = '';
        this.textContent = '';
        this.id = '';
        this.type = '';
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.htmlFor = '';
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    addEventListener(type, listener) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(listener);
    }

    dispatch(type) {
        for (const listener of this.listeners.get(type) || []) listener({ target: this });
    }
}

class FakeDocument {
    createElement(tagName) {
        return new FakeElement(tagName);
    }
}

function descendants(root) {
    const values = [];
    const visit = node => {
        values.push(node);
        node.children.forEach(visit);
    };
    visit(root);
    return values;
}

test('模型参数只接受有限数值、安全整数和真实布尔值', async () => {
    const { DEFAULT_MODEL_PARAMS, normalizeModelParams } = await loadModule();
    const malicious = normalizeModelParams({
        temperature: '<img src=x onerror=alert(1)>',
        top_p: '0.2',
        top_k: Number.NaN,
        num_predict: Number.POSITIVE_INFINITY,
        think: 'false',
    });
    assert.deepEqual(malicious, DEFAULT_MODEL_PARAMS);
    assert.equal(Object.isFrozen(malicious), true);

    assert.deepEqual(normalizeModelParams({
        temperature: 9,
        top_p: -2,
        top_k: 201,
        num_predict: 17,
        think: false,
    }), {
        temperature: 2,
        top_p: 0.1,
        top_k: 200,
        num_predict: 256,
        think: false,
    });

    const fractional = normalizeModelParams({ top_k: 3.5, num_predict: 512.5 });
    assert.equal(fractional.top_k, DEFAULT_MODEL_PARAMS.top_k);
    assert.equal(fractional.num_predict, DEFAULT_MODEL_PARAMS.num_predict);
});

test('模型参数编辑器使用 DOM 控件、显式标签和纯文本输出', async () => {
    const { createModelParamsEditor } = await loadModule();
    const editor = createModelParamsEditor(new FakeDocument(), {
        temperature: '<script>alert(1)</script>',
        top_p: 0.5,
        top_k: 60,
        num_predict: 2048,
        think: false,
    });
    const nodes = descendants(editor.root);
    const tags = nodes.map(node => node.tagName);
    const labels = nodes.filter(node => node.tagName === 'LABEL');
    assert.equal(tags.includes('SCRIPT'), false);
    assert.equal(tags.includes('IMG'), false);
    assert.equal(labels.length, 5);
    assert.deepEqual(labels.map(label => label.htmlFor), [
        'p-temp', 'p-topp', 'p-topk', 'p-nump', 'p-think',
    ]);
    assert.equal(editor.controls.temperature.value, '0.8');
    assert.equal(editor.controls.temperature.getAttribute('aria-describedby'), 'p-temp-hint');
    const temperatureOutput = nodes.find(node => node.id === 'val-p-temp');
    assert.equal(temperatureOutput.getAttribute('for'), 'p-temp');

    editor.controls.temperature.value = '1.2';
    editor.controls.temperature.dispatch('input');
    assert.equal(temperatureOutput.textContent, '1.2');
    assert.deepEqual(editor.read(), {
        temperature: 1.2,
        top_p: 0.5,
        top_k: 60,
        num_predict: 2048,
        think: false,
    });

    editor.setDisabled(true);
    assert.equal(Object.values(editor.controls).every(control => control.disabled), true);
});
