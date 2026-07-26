'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const security = require('../web/security.js');

function sourceBetween(source, startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    assert.notEqual(start, -1, `missing source marker: ${startMarker}`);
    const end = source.indexOf(endMarker, start + startMarker.length);
    assert.notEqual(end, -1, `missing source marker: ${endMarker}`);
    return source.slice(start, end);
}

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

test('security module loads before the ES module app', () => {
    const root = path.resolve(__dirname, '..');
    const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'web', 'app.mjs'), 'utf8');
    assert.ok(html.indexOf('security.js') < html.indexOf('app.mjs'));
    assert.ok(html.indexOf('api-client.js') < html.indexOf('app.mjs'));
    assert.ok(html.indexOf('session-ref.js') < html.indexOf('app.mjs'));
    assert.ok(html.indexOf('turn-client.js') < html.indexOf('app.mjs'));
    assert.match(html, /<script\s+type="module"\s+src="\/static\/app\.mjs/);
    assert.match(app, /aria-live/);
    assert.doesNotMatch(app, /\$\{c\.affinity\}/);
});

test('全部前端生产模块禁用 HTML 解析型注入入口', () => {
    const webRoot = path.resolve(__dirname, '..', 'web');
    const files = fs.readdirSync(webRoot).filter(name => /\.(?:js|mjs)$/.test(name));
    const forbidden = /\.(?:innerHTML|outerHTML)\b|\.insertAdjacentHTML\s*\(|\bdocument\.write\s*\(/;
    for (const name of files) {
        const source = fs.readFileSync(path.join(webRoot, name), 'utf8');
        assert.doesNotMatch(source, forbidden, name);
    }
});

test('session writes use ApiClient, immutable refs and persistent turn APIs', () => {
    const app = fs.readFileSync(path.resolve(__dirname, '..', 'web', 'app.mjs'), 'utf8');
    assert.match(app, /const expectedRevision = currentRevision\(requestRef\)/);
    assert.match(app, /expected_revision:\s*expectedRevision/);
    assert.match(app, /dataset\.messageId/);
    assert.match(app, /new TurnClient\(apiClient\)/);
    assert.match(app, /turnClient\.events\(/);
    assert.match(app, /turnClient\.cancel\(/);
    assert.match(app, /SessionRefTracker/);
    assert.equal((app.match(/\bfetch\s*\(/g) || []).length, 0);
    assert.equal((app.match(/state\.session\s*=/g) || []).length, 2);
    assert.match(app, /function commitSessionState\(/);
    assert.match(app, /function commitSummaryRefresh\(/);
    assert.doesNotMatch(app, /chat:\s*['"]\/api\/chat['"]/);
    assert.match(app, /const details = error\.details/);
    assert.equal((app.match(/function enterMessageEditMode\s*\(/g) || []).length, 1);
    assert.equal((app.match(/function showSummaryEditor\s*\(/g) || []).length, 1);
    assert.doesNotMatch(app, /function enterEditMode\s*\(/);
});

test('regenerate is one atomic turn command with no client-side history surgery', () => {
    const app = fs.readFileSync(path.resolve(__dirname, '..', 'web', 'app.mjs'), 'utf8');
    const regenerate = sourceBetween(
        app,
        'async function regenerateFrom(messageRef)',
        'async function reloadCurrentSession',
    );

    assert.equal((regenerate.match(/turnClient\.regenerate\s*\(/g) || []).length, 1);
    assert.doesNotMatch(regenerate, /sessionWrite\s*\(/);
    assert.doesNotMatch(regenerate, /\b(?:snapshot|truncate)\b/);
    assert.doesNotMatch(regenerate, /message_history|\.reverse\s*\(|\.find\s*\(/);
    assert.doesNotMatch(regenerate, /sendMessage\s*\(/);
});

test('message actions build stable references from message_id only', () => {
    const app = fs.readFileSync(path.resolve(__dirname, '..', 'web', 'app.mjs'), 'utf8');
    const bindings = sourceBetween(
        app,
        'function bindMessageActions(msgEl)',
        'async function togglePin',
    );

    assert.match(bindings, /const messageId\s*=\s*msgEl\.dataset\.messageId/);
    assert.match(bindings, /const messageRef\s*=\s*\{\s*message_id:\s*messageId\s*\}\s*;/);
    assert.doesNotMatch(bindings, /dataset\.index|\bidxRaw\b|\bindex\s*:/);
    assert.doesNotMatch(bindings, /message_id:\s*messageId\s*\|\|/);
});

test('summary actions use summary_id only and expose accessible lifecycle controls', () => {
    const root = path.resolve(__dirname, '..');
    const service = fs.readFileSync(path.join(root, 'web', 'summaries.mjs'), 'utf8');
    const panel = fs.readFileSync(path.join(root, 'web', 'summary-panel.mjs'), 'utf8');
    const css = fs.readFileSync(path.join(root, 'web', 'style.css'), 'utf8');
    assert.match(service, /summary_id:\s*requireSummaryId\(summaryId\)/);
    assert.doesNotMatch(service, /summary_index|\bindex\s*:/);
    assert.match(panel, /aria-expanded/);
    assert.match(panel, /aria-busy/);
    assert.match(panel, /source_status/);
    assert.match(css, /min-height:\s*44px/);
    assert.match(css, /:focus-visible/);
});

test('frontend modules have no duplicate named function declarations', () => {
    const webRoot = path.resolve(__dirname, '..', 'web');
    const files = fs.readdirSync(webRoot).filter(name => /\.(?:js|mjs)$/.test(name));
    for (const name of files) {
        const source = fs.readFileSync(path.join(webRoot, name), 'utf8');
        const declarations = [...source.matchAll(/\bfunction\s+([A-Za-z_$][\w$]*)\s*\(/g)].map(match => match[1]);
        const duplicates = declarations.filter((value, index) => declarations.indexOf(value) !== index);
        assert.deepEqual([...new Set(duplicates)], [], name);
    }
});

test('legacy hidden controls and their bindings are removed', () => {
    const root = path.resolve(__dirname, '..');
    const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'web', 'app.mjs'), 'utf8');
    const css = fs.readFileSync(path.join(root, 'web', 'style.css'), 'utf8');
    assert.doesNotMatch(html, /legacy-hide|id="(?:project-select|save-select|project-new|save-new|save-rename|save-delete|save-export|save-import|cards-btn)"/);
    assert.doesNotMatch(app, /oldProjSel|oldSaveSel|oldProjectSelect|oldSelect/);
    assert.doesNotMatch(css, /\.legacy-hide/);
});
