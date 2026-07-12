'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const security = require('../web/security.js');

test('malicious markup is escaped and never returned as an element', () => {
    const input = '<img src=x onerror="globalThis.pwned=true"><script>alert(1)</script>';
    const escaped = security.escapeHtml(input);
    assert.equal(escaped.includes('<img'), false);
    assert.equal(escaped.includes('<script>'), false);
    assert.match(escaped, /&lt;img/);
    assert.match(escaped, /&quot;/);
});

test('affinity from imported JSON is normalized to a finite number', () => {
    assert.equal(security.normalizeAffinity('<img onerror=alert(1)>'), 0);
    assert.equal(security.normalizeAffinity(Number.NaN), 0);
    assert.equal(security.normalizeAffinity(-20), 0);
    assert.equal(security.normalizeAffinity(140), 100);
    assert.equal(security.normalizeAffinity('42'), 42);
});

test('client import preflight enforces extension and 8 MiB limit', () => {
    assert.equal(security.validateImportFile({ name: 'save.json', size: 100 }), '');
    assert.match(security.validateImportFile({ name: 'save.html', size: 100 }), /\.json/);
    assert.match(security.validateImportFile({ name: 'save.json', size: 9 * 1024 * 1024 }), /8 MiB/);
});

test('security module loads before app and app has one escaping entry point', () => {
    const root = path.resolve(__dirname, '..');
    const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'web', 'app.js'), 'utf8');
    assert.ok(html.indexOf('security.js') < html.indexOf('app.js'));
    assert.equal((app.match(/function escapeHtml\s*\(/g) || []).length, 1);
    assert.match(app, /aria-live/);
    assert.doesNotMatch(app, /\$\{c\.affinity\}/);
});

test('session writes carry revisions and message actions prefer UUIDs', () => {
    const app = fs.readFileSync(path.resolve(__dirname, '..', 'web', 'app.js'), 'utf8');
    assert.match(app, /expected_revision:\s*currentRevision\(\)/);
    assert.match(app, /expected_revision:\s*expectedRevision/);
    assert.match(app, /dataset\.messageId/);
    assert.match(app, /message_id:\s*messageId\s*\|\|\s*undefined/);
    assert.match(app, /data\.type === 'conflict'/);
    assert.equal((app.match(/function enterMessageEditMode\s*\(/g) || []).length, 1);
    assert.equal((app.match(/function enterSummaryEditMode\s*\(/g) || []).length, 1);
    assert.doesNotMatch(app, /function enterEditMode\s*\(/);
});
