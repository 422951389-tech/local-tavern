import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const python = path.join(projectRoot, '.venv', 'Scripts', 'python.exe');
const edgeCandidates = [
    path.join(process.env['ProgramFiles(x86)'] || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    path.join(process.env.ProgramFiles || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
];
const edgeExecutable = edgeCandidates.find(existsSync);
const powerShellExecutable = [
    path.join(process.env.LOCALAPPDATA || '', 'Microsoft', 'WindowsApps', 'pwsh.exe'),
    path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
].find(existsSync);
const messageId = '11111111-1111-4111-8111-111111111111';

if (!existsSync(python)) throw new Error(`缺少 Python 虚拟环境：${python}`);
if (!edgeExecutable) throw new Error('未找到 Microsoft Edge');
if (typeof WebSocket !== 'function') throw new Error('浏览器 smoke 需要 Node.js 22+ 的 WebSocket');

function freePort() {
    return new Promise((resolve, reject) => {
        const server = createServer();
        server.once('error', reject);
        server.listen(0, '127.0.0.1', () => {
            const { port } = server.address();
            server.close(error => (error ? reject(error) : resolve(port)));
        });
    });
}

function delay(milliseconds) {
    return new Promise(resolve => setTimeout(resolve, milliseconds));
}

async function waitUntil(callback, label, timeoutMs = 15_000) {
    const deadline = Date.now() + timeoutMs;
    let lastError = null;
    while (Date.now() < deadline) {
        try {
            const value = await callback();
            if (value) return value;
        } catch (error) {
            lastError = error;
        }
        await delay(75);
    }
    throw new Error(`${label} 超时${lastError ? `：${lastError.message}` : ''}`);
}

class CdpClient {
    constructor(socket) {
        this.socket = socket;
        this.serial = 0;
        this.pending = new Map();
        this.listeners = new Map();
        this.eventErrors = [];
        socket.addEventListener('message', event => {
            const message = JSON.parse(String(event.data));
            if (message.id) {
                const pending = this.pending.get(message.id);
                if (!pending) return;
                this.pending.delete(message.id);
                if (message.error) pending.reject(new Error(message.error.message));
                else pending.resolve(message.result || {});
                return;
            }
            for (const listener of this.listeners.get(message.method) || []) {
                Promise.resolve(listener(message.params || {})).catch(error => this.eventErrors.push(error));
            }
        });
    }

    static connect(url) {
        return new Promise((resolve, reject) => {
            const socket = new WebSocket(url);
            socket.addEventListener('open', () => resolve(new CdpClient(socket)), { once: true });
            socket.addEventListener('error', () => reject(new Error('CDP WebSocket 连接失败')), { once: true });
        });
    }

    on(method, listener) {
        if (!this.listeners.has(method)) this.listeners.set(method, []);
        this.listeners.get(method).push(listener);
    }

    send(method, params = {}) {
        const id = ++this.serial;
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
            this.socket.send(JSON.stringify({ id, method, params }));
        });
    }

    close() {
        this.socket.close();
    }
}

async function evaluate(client, expression) {
    const response = await client.send('Runtime.evaluate', {
        expression,
        awaitPromise: true,
        returnByValue: true,
        userGesture: true,
    });
    if (response.exceptionDetails) {
        throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    }
    return response.result?.value;
}

async function waitForExpression(client, expression, label, timeoutMs = 15_000) {
    return waitUntil(() => evaluate(client, expression), label, timeoutMs);
}

async function pressEnter(client) {
    const params = {
        key: 'Enter',
        code: 'Enter',
        windowsVirtualKeyCode: 13,
        nativeVirtualKeyCode: 13,
    };
    await client.send('Input.dispatchKeyEvent', {
        ...params,
        type: 'keyDown',
        text: '\r',
        unmodifiedText: '\r',
    });
    await client.send('Input.dispatchKeyEvent', { ...params, type: 'keyUp' });
}

async function stopChild(child) {
    if (!child) return;
    if (process.platform === 'win32' && Number.isInteger(child.pid)) {
        spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
            windowsHide: true,
            stdio: 'ignore',
        });
        await delay(500);
        return;
    }
    if (child.exitCode !== null) return;
    child.kill();
    await Promise.race([
        new Promise(resolve => child.once('exit', resolve)),
        delay(2_000),
    ]);
    if (child.exitCode === null) child.kill('SIGKILL');
}

async function removeTempRoot(tempRoot) {
    for (let attempt = 0; attempt < 40; attempt += 1) {
        try {
            rmSync(tempRoot, { recursive: true, force: true });
            return true;
        } catch (error) {
            if (!['EPERM', 'EBUSY', 'ENOTEMPTY'].includes(error.code)) throw error;
            await delay(250);
        }
    }
    return false;
}

async function stopMatchingEdgeProcesses(tempRoot) {
    if (process.platform !== 'win32' || !powerShellExecutable) return;
    const escaped = tempRoot.replaceAll("'", "''");
    const command = [
        `$needle='${escaped}'`,
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'msedge*' -and $_.CommandLine -like ('*' + $needle + '*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
    ].join('; ');
    spawnSync(powerShellExecutable, ['-NoProfile', '-Command', command], {
        windowsHide: true,
        stdio: 'ignore',
    });
    await delay(750);
}

const seedScript = String.raw`
import asyncio
from core.character_loader import ensure_project
from core.session_manager import mutate_session

PROJECT = "默认项目"
SAVE = "默认存档"
MESSAGES = [
    {"id": "11111111-1111-4111-8111-111111111111", "role": "user", "content": "阿尔法在雨夜向贝塔交付了密信。", "pinned": False, "in_prompt": True},
    {"id": "22222222-2222-4222-8222-222222222222", "role": "assistant", "content": "贝塔收下密信，并答应共同守护城门。", "pinned": False, "in_prompt": True},
]

def seed(session, context):
    del context
    session["current_model"] = "browser-smoke"
    session["characters_state"] = {
        "alpha": {"name": "阿尔法", "mood": "警觉", "affinity": 12},
        "beta": {"name": "贝塔", "mood": "坚定", "affinity": 88},
    }
    session["message_history"] = MESSAGES
    session["relationship_edges"] = []
    return True

async def main():
    ensure_project(PROJECT)
    result = await mutate_session(PROJECT, SAVE, 0, seed)
    assert result.session["revision"] == 1

asyncio.run(main())
`;

const tempRoot = mkdtempSync(path.join(tmpdir(), 'local-tavern-rel-browser-'));
const appPort = await freePort();
const debugPort = await freePort();
const appUrl = `http://127.0.0.1:${appPort}`;
const environment = {
    ...process.env,
    TAVERN_BASE_DIR: tempRoot,
    TAVERN_DATA_DIR: path.join(tempRoot, 'data'),
    TAVERN_SETTINGS_PATH: path.join(tempRoot, 'data', 'settings.json'),
    TAVERN_PROJECTS_DIR: path.join(tempRoot, 'data', 'projects'),
    TAVERN_RECOVERY_DIR: path.join(tempRoot, 'data', '.recovery'),
    TAVERN_MIGRATIONS_DIR: path.join(tempRoot, 'data', '.migrations'),
    TAVERN_BACKUP_DIR: path.join(tempRoot, 'backups'),
    TAVERN_LOG_DIR: path.join(tempRoot, 'logs'),
    TAVERN_LOG_FILE: path.join(tempRoot, 'logs', 'tavern.log'),
    TAVERN_PID_PATH: path.join(tempRoot, 'tavern.pid'),
    TAVERN_STOP_REQUEST_PATH: path.join(tempRoot, 'tavern.stop.pid'),
    TAVERN_WEB_DIR: path.join(projectRoot, 'web'),
    TAVERN_PROMPTS_DIR: path.join(projectRoot, 'prompts'),
    TAVERN_OLLAMA_HOST: 'http://127.0.0.1:9',
    TAVERN_PORT: String(appPort),
};

let serverProcess = null;
let edgeProcess = null;
let client = null;
const serverOutput = [];

async function getSession() {
    const response = await fetch(`${appUrl}/api/session?project=${encodeURIComponent('默认项目')}&save=${encodeURIComponent('默认存档')}`);
    assert.equal(response.status, 200);
    const body = await response.json();
    return body.session || body;
}

async function putRelationship(revision, relationType, strength) {
    const response = await fetch(`${appUrl}/api/session/relationships`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            project: '默认项目',
            save: '默认存档',
            expected_revision: revision,
            edge: {
                source_character_id: 'alpha',
                target_character_id: 'beta',
                relation_type: relationType,
                strength,
                evidence_message_ids: [messageId],
            },
        }),
    });
    const body = await response.json();
    assert.equal(response.status, 200, JSON.stringify(body));
    return body;
}

async function waitForApplication() {
    await waitForExpression(
        client,
        `(() => {
            const tab = document.getElementById('tab-relations');
            return document.readyState === 'complete'
                && tab && !tab.disabled
                && document.querySelectorAll('.msg[data-message-id]').length === 2;
        })()`,
        '应用初始化',
    );
}

async function openRelationshipsWithKeyboard() {
    const focused = await evaluate(client, `(() => {
        const tab = document.getElementById('tab-relations');
        tab.focus();
        return document.activeElement === tab;
    })()`);
    assert.equal(focused, true);
    await pressEnter(client);
    await waitForExpression(
        client,
        `!document.getElementById('modal-backdrop').classList.contains('hidden')
            && Boolean(document.querySelector('.relationship-editor'))`,
        '关系编辑器打开',
    );
}

try {
    const seeded = spawnSync(python, ['-'], {
        cwd: projectRoot,
        env: environment,
        input: seedScript,
        encoding: 'utf8',
    });
    assert.equal(seeded.status, 0, seeded.stderr || seeded.stdout);

    serverProcess = spawn(python, [
        '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1',
        '--port', String(appPort), '--log-level', 'warning',
    ], {
        cwd: projectRoot,
        env: environment,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    for (const stream of [serverProcess.stdout, serverProcess.stderr]) {
        stream.on('data', chunk => serverOutput.push(String(chunk)));
    }
    await waitUntil(async () => {
        const response = await fetch(`${appUrl}/api/projects`);
        return response.ok;
    }, '服务启动', 20_000);

    edgeProcess = spawn(edgeExecutable, [
        '--headless=new',
        `--remote-debugging-port=${debugPort}`,
        `--user-data-dir=${path.join(tempRoot, 'edge-profile')}`,
        '--disable-extensions',
        '--disable-background-networking',
        '--disable-background-mode',
        '--disable-gpu',
        '--no-first-run',
        '--no-default-browser-check',
        'about:blank',
    ], { windowsHide: true, stdio: 'ignore' });

    const page = await waitUntil(async () => {
        const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
        const pages = await response.json();
        return pages.find(candidate => candidate.type === 'page' && candidate.webSocketDebuggerUrl);
    }, 'Edge CDP', 20_000);
    client = await CdpClient.connect(page.webSocketDebuggerUrl);
    const runtimeExceptions = [];
    const dialogs = [];
    client.on('Runtime.exceptionThrown', event => runtimeExceptions.push(event.exceptionDetails?.text || 'runtime exception'));
    client.on('Page.javascriptDialogOpening', async event => {
        dialogs.push(event.message);
        await client.send('Page.handleJavaScriptDialog', { accept: false });
    });
    await client.send('Runtime.enable');
    await client.send('Page.enable');
    await client.send('Emulation.setDeviceMetricsOverride', {
        width: 375,
        height: 812,
        deviceScaleFactor: 1,
        mobile: true,
    });
    await client.send('Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-reduced-motion', value: 'reduce' }],
    });
    await client.send('Page.navigate', { url: appUrl });
    await waitForApplication();
    assert.deepEqual(dialogs, []);

    await openRelationshipsWithKeyboard();
    const emptyState = await evaluate(client, `({
        emptyCount: [...document.querySelectorAll('.relationship-empty')]
            .filter(node => node.textContent.trim() === '未记录').length,
        horizontalOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    })`);
    assert.equal(emptyState.emptyCount >= 2, true);
    assert.equal(emptyState.horizontalOverflow <= 0, true);

    let delayedWrite = true;
    await client.send('Fetch.enable', {
        patterns: [{ urlPattern: '*api/session/relationships', requestStage: 'Request' }],
    });
    client.on('Fetch.requestPaused', async event => {
        if (delayedWrite && event.request.method === 'PUT') {
            delayedWrite = false;
            await delay(300);
        }
        await client.send('Fetch.continueRequest', { requestId: event.requestId });
    });

    await evaluate(client, `(() => {
        const change = (selector, value) => {
            const control = document.querySelector(selector);
            control.value = value;
            control.dispatchEvent(new Event('change', { bubbles: true }));
        };
        change('#relationship-source', 'alpha');
        change('#relationship-target', 'beta');
        change('#relationship-type', '盟友');
        change('#relationship-strength', '80');
        document.querySelector('.relationship-evidence-checkbox').click();
        const save = document.querySelector('.relationship-save-button');
        save.click();
        save.click();
        document.getElementById('tab-relations').click();
        return true;
    })()`);
    await waitForExpression(
        client,
        `document.querySelector('.relationship-status')?.textContent.includes('关系已记录')`,
        '关系保存',
    );
    const afterCreate = await getSession();
    assert.equal(afterCreate.revision, 2);
    assert.equal(afterCreate.relationship_edges.length, 1);
    assert.equal(afterCreate.relationship_edges[0].relation_type, '盟友');
    assert.equal(await evaluate(client, `document.getElementById('modal-close').disabled`), false);

    const metrics = await evaluate(client, `(() => {
        const controls = [...document.querySelectorAll(
            '.relationship-evidence-button,.relationship-edit-button,.relationship-delete-button,.relationship-save-button'
        )];
        return {
            viewport: [document.documentElement.clientWidth, window.innerHeight],
            horizontalOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            graph: Boolean(document.querySelector('svg.relationship-graph')),
            cardText: document.querySelector('.relationship-card')?.textContent || '',
            minControlHeight: Math.min(...controls.map(control => control.getBoundingClientRect().height)),
            semanticList: document.querySelector('.relationship-list')?.tagName || '',
        };
    })()`);
    assert.deepEqual(metrics.viewport, [375, 812]);
    assert.equal(metrics.horizontalOverflow <= 0, true);
    assert.equal(metrics.graph, true);
    assert.match(metrics.cardText, /阿尔法 → 贝塔/);
    assert.match(metrics.cardText, /盟友/);
    assert.match(metrics.cardText, /80\/100/);
    assert.equal(metrics.minControlHeight >= 44, true);
    assert.equal(metrics.semanticList, 'UL');

    await evaluate(client, `(() => {
        const target = document.querySelector('.msg[data-message-id="${messageId}"]');
        target.scrollIntoView = options => { window.__relationshipScrollOptions = options; };
        document.querySelector('.relationship-evidence-button').focus();
        return true;
    })()`);
    await pressEnter(client);
    await waitForExpression(
        client,
        `document.getElementById('modal-backdrop').classList.contains('hidden')`,
        '证据定位关闭 Modal',
    );
    const located = await evaluate(client, `({
        messageId: document.activeElement?.dataset?.messageId || '',
        behavior: window.__relationshipScrollOptions?.behavior || '',
    })`);
    assert.deepEqual(located, { messageId, behavior: 'auto' });

    await openRelationshipsWithKeyboard();
    await evaluate(client, `(() => {
        document.querySelector('.msg[data-message-id="${messageId}"]').remove();
        document.querySelector('.relationship-evidence-button').click();
        return true;
    })()`);
    await waitForExpression(
        client,
        `document.querySelector('.relationship-status')?.textContent.includes('无法在当前对话中定位')`,
        '缺失 DOM 消息防御',
    );
    assert.equal(await evaluate(client, `document.getElementById('modal-backdrop').classList.contains('hidden')`), false);
    await evaluate(client, `document.getElementById('modal-close').click()`);

    await client.send('Page.reload', { ignoreCache: true });
    await waitForApplication();
    await putRelationship(2, '盟友', 65);
    await openRelationshipsWithKeyboard();
    await evaluate(client, `document.querySelector('.relationship-evidence-button').click()`);
    await waitForExpression(
        client,
        `document.querySelector('.relationship-status')?.textContent.includes('当前存档已更新')`,
        'revision 变化防御',
    );
    assert.equal(await evaluate(client, `document.getElementById('modal-backdrop').classList.contains('hidden')`), false);
    await evaluate(client, `document.getElementById('modal-close').click()`);

    await client.send('Page.reload', { ignoreCache: true });
    await waitForApplication();
    await openRelationshipsWithKeyboard();
    await evaluate(client, `(() => {
        document.querySelector('.relationship-edit-button').click();
        document.querySelector('#relationship-type').value = '挚友';
        document.querySelector('#relationship-strength').value = '75';
        const save = document.querySelector('.relationship-save-button');
        save.click();
        save.click();
        return true;
    })()`);
    await waitForExpression(
        client,
        `document.querySelector('.relationship-status')?.textContent.includes('关系已更新')`,
        '关系编辑',
    );
    const afterEdit = await getSession();
    assert.equal(afterEdit.revision, 4);
    assert.equal(afterEdit.relationship_edges[0].relation_type, '挚友');
    assert.equal(afterEdit.relationship_edges[0].strength, 75);

    await evaluate(client, `(() => {
        window.confirm = () => true;
        const remove = document.querySelector('.relationship-delete-button');
        remove.click();
        remove.click();
        return true;
    })()`);
    await waitForExpression(
        client,
        `document.querySelector('.relationship-status')?.textContent.includes('关系已删除')`,
        '关系删除',
    );
    const afterDelete = await getSession();
    assert.equal(afterDelete.revision, 5);
    assert.deepEqual(afterDelete.relationship_edges, []);
    assert.equal(
        await evaluate(client, `[...document.querySelectorAll('.relationship-empty')]
            .some(node => node.textContent.trim() === '未记录')`),
        true,
    );

    await client.send('Fetch.disable');
    assert.deepEqual(client.eventErrors, []);
    assert.deepEqual(runtimeExceptions, []);
    console.log(JSON.stringify({
        status: 'passed',
        viewport: metrics.viewport,
        horizontalOverflow: metrics.horizontalOverflow,
        minControlHeight: metrics.minControlHeight,
        finalRevision: afterDelete.revision,
        edgeExecutable,
    }));
} catch (error) {
    if (client) {
        try {
            const pageDiagnostic = await evaluate(client, `({
                readyState: document.readyState,
                url: location.href,
                title: document.title,
                relationTab: Boolean(document.getElementById('tab-relations')),
                relationTabDisabled: document.getElementById('tab-relations')?.disabled,
                messageCount: document.querySelectorAll('.msg[data-message-id]').length,
                projectName: document.getElementById('project-name')?.textContent || '',
                bodyText: document.body?.innerText?.slice(0, 600) || '',
            })`);
            console.error(`PAGE_DIAGNOSTIC ${JSON.stringify(pageDiagnostic)}`);
        } catch (diagnosticError) {
            console.error(`PAGE_DIAGNOSTIC_FAILED ${diagnosticError.message}`);
        }
    }
    const diagnostic = serverOutput.join('').slice(-4_000);
    if (diagnostic) console.error(diagnostic);
    throw error;
} finally {
    if (client) client.close();
    await stopChild(edgeProcess);
    await stopMatchingEdgeProcesses(tempRoot);
    await stopChild(serverProcess);
    const resolvedTemp = path.resolve(tempRoot);
    const resolvedSystemTemp = path.resolve(tmpdir());
    if (resolvedTemp.startsWith(`${resolvedSystemTemp}${path.sep}`)
        && path.basename(resolvedTemp).startsWith('local-tavern-rel-browser-')) {
        const removed = await removeTempRoot(resolvedTemp);
        if (!removed) console.error(`TEMP_CLEANUP_PENDING ${resolvedTemp}`);
    }
}
