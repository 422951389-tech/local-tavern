'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

class FakeClassList {
    constructor(owner) {
        this.owner = owner;
        this.values = new Set();
    }
    set(value) {
        this.values = new Set(String(value || '').split(/\s+/).filter(Boolean));
    }
    contains(value) { return this.values.has(value); }
    toString() { return [...this.values].join(' '); }
}

class FakeElement {
    constructor(tagName) {
        this.tagName = String(tagName).toUpperCase();
        this.children = [];
        this.attributes = {};
        this.style = {};
        this.textContent = '';
        this.classList = new FakeClassList(this);
        this._className = '';
    }
    set className(value) {
        this._className = String(value || '');
        this.classList.set(this._className);
    }
    get className() { return this._className; }
    appendChild(child) {
        this.children.push(child);
        return child;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
}

class FakeDocument {
    createElement(tagName) { return new FakeElement(tagName); }
}

const normalize = value => Math.max(0, Math.min(100, Math.round(Number(value) || 0)));

test('好感度展示提供数值、关系阶段和本轮变化，不依赖颜色传意', async () => {
    const { affinityPresentation } = await import('../web/conversation-view.mjs');
    assert.deepEqual(affinityPresentation(60, normalize, 55), {
        percent: 60,
        previous: 55,
        delta: 5,
        stage: '亲近',
        direction: 'up',
        changeText: '本轮 +5',
        ariaText: '好感度 60/100，关系阶段亲近，本轮上升 5',
    });
    assert.equal(affinityPresentation(20, normalize, 24).changeText, '本轮 −4');
    assert.equal(affinityPresentation(80, normalize, 80).changeText, '本轮无变化');
    assert.equal(affinityPresentation(101, normalize).stage, '深厚');
});

test('好感度组件输出语义 meter、文字标签和精确填充比例', async () => {
    const { createAffinityIndicator } = await import('../web/conversation-view.mjs');
    const rendered = createAffinityIndicator(new FakeDocument(), {
        value: 63,
        previousValue: 60,
        normalize,
    });
    const [copy, meter] = rendered.element.children;
    assert.equal(rendered.element.classList.contains('affinity-up'), true);
    assert.deepEqual(copy.children.map(child => child.textContent), [
        '好感度', '63/100', '亲近', '本轮 +3',
    ]);
    assert.equal(meter.attributes.role, 'meter');
    assert.equal(meter.attributes['aria-valuenow'], '63');
    assert.match(meter.attributes['aria-valuetext'], /关系阶段亲近/);
    assert.equal(meter.children[0].style.width, '63%');
});
