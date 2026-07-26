import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import {
    existsSync,
    mkdtempSync,
    readFileSync,
    renameSync,
    rmSync,
    writeFileSync,
} from 'node:fs';
import { createRequire } from 'node:module';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = new Set(process.argv.slice(2));
const knownArgs = new Set(['--system-edge', '--skip-axe']);
for (const argument of args) {
    if (!knownArgs.has(argument)) throw new Error(`未知参数：${argument}`);
}
const useSystemEdge = args.has('--system-edge');
const skipAxe = args.has('--skip-axe');
const releaseMode = !useSystemEdge && !skipAxe;
const EXPECTED_PLAYWRIGHT_VERSION = '1.61.1';
const EXPECTED_AXE_VERSION = '4.12.1';
const EXPECTED_BROWSER_VERSION = '149.0.7827.55';
const moduleOverrideKeys = ['TAVERN_PLAYWRIGHT_MODULE', 'TAVERN_AXE_MODULE'];
if (releaseMode) {
    const configuredOverrides = moduleOverrideKeys.filter(key => process.env[key]);
    if (configuredOverrides.length) {
        throw new Error(`发布门禁止本地模块覆盖：${configuredOverrides.join(', ')}`);
    }
}

function packageManifest(name, configured, resolvedEntry) {
    if (configured) {
        const packageFile = path.join(path.resolve(configured), 'package.json');
        return JSON.parse(readFileSync(packageFile, 'utf8'));
    }
    let directory = path.dirname(resolvedEntry);
    while (true) {
        const packageFile = path.join(directory, 'package.json');
        if (existsSync(packageFile)) {
            const manifest = JSON.parse(readFileSync(packageFile, 'utf8'));
            if (manifest.name === name) return manifest;
        }
        const parent = path.dirname(directory);
        if (parent === directory) break;
        directory = parent;
    }
    throw new Error(`无法定位 ${name} 的 package.json`);
}

function loadPackage(name, environmentKey) {
    const configured = process.env[environmentKey];
    const entry = configured ? path.resolve(configured) : name;
    try {
        const loaded = require(entry);
        const resolvedEntry = require.resolve(entry);
        const version = packageManifest(name, configured, resolvedEntry).version;
        return { loaded, version, entry: resolvedEntry };
    } catch (error) {
        throw new Error(`缺少 ${name}（可用 ${environmentKey} 指定本地包目录）：${error.message}`);
    }
}

const playwrightPackage = loadPackage('playwright', 'TAVERN_PLAYWRIGHT_MODULE');
if (playwrightPackage.version !== EXPECTED_PLAYWRIGHT_VERSION) {
    throw new Error(`Playwright 版本必须为 ${EXPECTED_PLAYWRIGHT_VERSION}`);
}
const { chromium } = playwrightPackage.loaded;
if (!chromium) throw new Error('playwright 包未导出 chromium');
let AxeBuilder = null;
let axeVersion = null;
if (!skipAxe) {
    const axePackage = loadPackage('@axe-core/playwright', 'TAVERN_AXE_MODULE');
    AxeBuilder = axePackage.loaded.default || axePackage.loaded.AxeBuilder;
    axeVersion = axePackage.version;
    if (axeVersion !== EXPECTED_AXE_VERSION) {
        throw new Error(`axe Playwright 版本必须为 ${EXPECTED_AXE_VERSION}`);
    }
    if (typeof AxeBuilder !== 'function') throw new Error('@axe-core/playwright 未导出 AxeBuilder');
}

function firstExisting(paths) {
    return paths.find(candidate => candidate && existsSync(candidate));
}

const python = firstExisting([
    process.env.TAVERN_TEST_PYTHON && path.resolve(process.env.TAVERN_TEST_PYTHON),
    path.join(projectRoot, '.venv-dev', 'Scripts', 'python.exe'),
    path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
]);
if (!python) throw new Error('未找到 .venv-dev 或 .venv 的 Python');

const edgeExecutable = firstExisting([
    path.join(process.env['ProgramFiles(x86)'] || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    path.join(process.env.ProgramFiles || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
]);
if (useSystemEdge && !edgeExecutable) throw new Error('--system-edge 需要本机 Microsoft Edge');

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

async function waitUntil(callback, label, timeoutMs = 20_000) {
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

async function stopChild(child) {
    if (!child || child.exitCode !== null) return;
    if (process.platform === 'win32' && Number.isInteger(child.pid)) {
        spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
            windowsHide: true,
            stdio: 'ignore',
        });
        await delay(300);
        return;
    }
    child.kill();
    await Promise.race([
        new Promise(resolve => child.once('exit', resolve)),
        delay(2_000),
    ]);
    if (child.exitCode === null) child.kill('SIGKILL');
}

async function removeTempRoot(tempRoot) {
    const resolved = path.resolve(tempRoot);
    const systemTemp = path.resolve(tmpdir());
    if (!resolved.startsWith(`${systemTemp}${path.sep}`)
        || !path.basename(resolved).startsWith('local-tavern-e2e-')) {
        throw new Error(`拒绝清理非 E2E 临时目录：${resolved}`);
    }
    for (let attempt = 0; attempt < 40; attempt += 1) {
        try {
            rmSync(resolved, { recursive: true, force: true });
            return true;
        } catch (error) {
            if (!['EPERM', 'EBUSY', 'ENOTEMPTY'].includes(error.code)) throw error;
            await delay(250);
        }
    }
    return false;
}

function setControl(controlFile, scenario, values = {}) {
    const temporary = `${controlFile}.tmp`;
    writeFileSync(temporary, JSON.stringify({ scenario, ...values }), 'utf8');
    renameSync(temporary, controlFile);
}

async function jsonResponse(url, options = {}) {
    const response = await fetch(url, options);
    const body = await response.json();
    assert.equal(response.ok, true, `${response.status} ${JSON.stringify(body)}`);
    return body;
}

const tempRoot = mkdtempSync(path.join(tmpdir(), 'local-tavern-e2e-'));
const controlFile = path.join(tempRoot, 'ollama-control.json');
const appPort = await freePort();
const ollamaPort = await freePort();
const appUrl = `http://127.0.0.1:${appPort}`;
const environment = {
    ...process.env,
    PYTHONUTF8: '1',
    TAVERN_E2E_SEED: '1',
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
    TAVERN_OLLAMA_HOST: `http://127.0.0.1:${ollamaPort}`,
    TAVERN_BACKUP_SCHEDULE_ENABLED: 'false',
    TAVERN_HOST: '127.0.0.1',
    TAVERN_PORT: String(appPort),
};

let fakeProcess = null;
let serverProcess = null;
let browser = null;
let context = null;
const processOutput = [];
const pageErrors = [];
const consoleErrors = [];
const auditedViews = [];
let expectedHttpFailure = false;

function collectProcessOutput(child, label) {
    for (const stream of [child.stdout, child.stderr]) {
        stream.on('data', chunk => processOutput.push(`[${label}] ${String(chunk)}`));
    }
}

async function currentSession(project = '默认项目', save = '默认存档') {
    const body = await jsonResponse(
        `${appUrl}/api/session?project=${encodeURIComponent(project)}&save=${encodeURIComponent(save)}`,
    );
    return body.session || body;
}

async function createSnapshot() {
    const session = await currentSession();
    return jsonResponse(
        `${appUrl}/api/session?project=${encodeURIComponent('默认项目')}&save=${encodeURIComponent('默认存档')}`,
        {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'snapshot', expected_revision: session.revision }),
        },
    );
}

async function openSaveDropdown(page) {
    await page.locator('#tab-saves').click();
    await page.locator('#save-dropdown').waitFor({ state: 'visible' });
}

async function switchSave(page, save) {
    await openSaveDropdown(page);
    await page.locator(`.dropdown-item[data-save="${save}"]`).click();
    await page.waitForFunction(target => (
        document.querySelector(`.dropdown-item[data-save="${target}"]`)?.classList.contains('active')
    ), save);
}

async function switchProject(page, project) {
    await page.locator('#project-btn').click();
    await page.locator('#project-dropdown').waitFor({ state: 'visible' });
    await page.locator(`.dropdown-item[data-project="${project}"]`).click();
    await page.locator('#project-name').filter({ hasText: project }).waitFor();
}

const AXE_TAGS = Object.freeze(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']);

async function axeSeriousCritical(page, scope = null) {
    if (skipAxe) return null;
    let builder = new AxeBuilder({ page }).withTags(AXE_TAGS);
    if (scope) builder = builder.include(scope);
    const result = await builder.analyze();
    return result.violations
        .filter(item => ['serious', 'critical'].includes(item.impact))
        .map(item => ({
            id: item.id,
            impact: item.impact,
            nodes: item.nodes.length,
            help: item.help,
        }));
}

async function auditOpenModal(page, name, { statusSelectors = [] } = {}) {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.locator('#modal-backdrop').waitFor({ state: 'visible' });
    const layout = await page.evaluate(({ selectors }) => {
        const backdrop = document.querySelector('#modal-backdrop');
        const dialog = document.querySelector('#modal');
        const body = document.querySelector('#modal-body');
        const titleId = dialog?.getAttribute('aria-labelledby') || '';
        const title = titleId ? document.getElementById(titleId) : null;
        const labels = [...(body?.querySelectorAll('label') || [])];
        const visible = element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
                && rect.width > 0 && rect.height > 0;
        };
        const unlabeledControls = [...(body?.querySelectorAll('input, textarea, select') || [])]
            .filter(visible)
            .filter(control => !control.id || !labels.some(label => label.htmlFor === control.id))
            .map(control => ({ tag: control.tagName, id: control.id, type: control.type || '' }));
        const requiredStatuses = ['#modal-error', ...selectors].map(selector => ({
            selector,
            element: document.querySelector(selector),
        }));
        const invalidStatuses = requiredStatuses
            .filter(({ element }) => (
                !element
                || !['status', 'alert'].includes(element.getAttribute('role'))
                || !['polite', 'assertive'].includes(element.getAttribute('aria-live'))
            ))
            .map(({ selector, element }) => ({
                selector,
                role: element?.getAttribute('role') || null,
                ariaLive: element?.getAttribute('aria-live') || null,
            }));
        const executableNodes = [...(body?.querySelectorAll('script, img') || [])]
            .map(element => ({ tag: element.tagName, src: element.getAttribute('src') || '' }));
        const viewportWidth = document.documentElement.clientWidth;
        const dialogRect = dialog?.getBoundingClientRect();
        return {
            viewportWidth,
            pageHorizontalOverflow: document.documentElement.scrollWidth - viewportWidth,
            modalBodyHorizontalOverflow: body ? body.scrollWidth - body.clientWidth : null,
            dialogBounds: dialogRect ? {
                left: Math.round(dialogRect.left * 10) / 10,
                right: Math.round(dialogRect.right * 10) / 10,
                width: Math.round(dialogRect.width * 10) / 10,
            } : null,
            backdropAriaHidden: backdrop?.getAttribute('aria-hidden') || null,
            dialogRole: dialog?.getAttribute('role') || null,
            dialogAriaModal: dialog?.getAttribute('aria-modal') || null,
            titleId,
            titleText: title?.textContent?.trim() || '',
            unlabeledControls,
            invalidStatuses,
            executableNodes,
        };
    }, { selectors: statusSelectors });

    assert.equal(layout.backdropAriaHidden, 'false', `${name}: backdrop 必须暴露给辅助技术`);
    assert.equal(layout.dialogRole, 'dialog', `${name}: Modal 缺少 dialog 角色`);
    assert.equal(layout.dialogAriaModal, 'true', `${name}: Modal 缺少 aria-modal`);
    assert.notEqual(layout.titleId, '', `${name}: Modal 缺少标题关联`);
    assert.notEqual(layout.titleText, '', `${name}: Modal 标题为空`);
    assert.deepEqual(layout.unlabeledControls, [], `${name}: 存在没有显式 label/for 的表单控件`);
    assert.deepEqual(layout.invalidStatuses, [], `${name}: 状态区缺少 role/aria-live`);
    assert.deepEqual(layout.executableNodes, [], `${name}: Modal 内出现 img/script 节点`);
    assert.equal(layout.pageHorizontalOverflow <= 0, true, `${name}: 页面在 375px 横向溢出`);
    assert.equal(layout.modalBodyHorizontalOverflow <= 0, true, `${name}: Modal 内容在 375px 横向溢出`);
    assert.equal(Boolean(
        layout.dialogBounds
        && layout.dialogBounds.left >= -0.1
        && layout.dialogBounds.right <= layout.viewportWidth + 0.1
    ), true, `${name}: Modal 边界超出 375px 视口`);

    const severe = await axeSeriousCritical(page, '#modal');
    if (severe) assert.deepEqual(severe, [], `${name}: axe serious/critical 违规`);
    auditedViews.push({
        name,
        kind: 'modal',
        viewport: 375,
        axe_serious_critical: severe === null ? null : severe.length,
        layout,
    });
}

async function closeModalAfterAudit(page) {
    await page.locator('#modal-close').click();
    await page.locator('#modal-backdrop').waitFor({ state: 'hidden' });
}

async function auditReadOnlyModals(page) {
    await page.locator('#model-params-btn').click();
    await auditOpenModal(page, 'model_params');
    await closeModalAfterAudit(page);

    await openSaveDropdown(page);
    await page.locator('#save-new-inline').click();
    await auditOpenModal(page, 'new_save');
    await closeModalAfterAudit(page);

    await openSaveDropdown(page);
    await page.locator('#save-rename-inline').click();
    await auditOpenModal(page, 'rename_save');
    await closeModalAfterAudit(page);

    await openSaveDropdown(page);
    const chooserPromise = page.waitForEvent('filechooser');
    await page.locator('#save-import-inline').click();
    await chooserPromise;
    await auditOpenModal(page, 'import_save', { statusSelectors: ['#save-import-status'] });
    await closeModalAfterAudit(page);

    await page.locator('#history-btn').click();
    await page.waitForFunction(() => document.querySelector('#history-status')?.textContent !== '加载中…');
    await auditOpenModal(page, 'history', { statusSelectors: ['#history-status'] });
    await closeModalAfterAudit(page);

    await page.locator('#tab-chars').click();
    await page.locator('#ce-status').waitFor({ state: 'attached' });
    await auditOpenModal(page, 'character_cards', { statusSelectors: ['#ce-status'] });
    await closeModalAfterAudit(page);
}

async function auditViewports(page) {
    const failures = [];
    const layouts = [];
    for (const width of [375, 600, 768]) {
        await page.setViewportSize({ width, height: width === 375 ? 812 : 900 });
        const layout = await page.evaluate(() => ({
            width: document.documentElement.clientWidth,
            horizontalOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        }));
        layouts.push(layout);
        assert.equal(layout.horizontalOverflow <= 0, true, JSON.stringify(layout));
        const severe = await axeSeriousCritical(page);
        if (severe) {
            for (const violation of severe) {
                failures.push({
                    width,
                    id: violation.id,
                    impact: violation.impact,
                    nodes: violation.nodes,
                    help: violation.help,
                });
            }
        }
    }
    assert.deepEqual(failures, []);
    auditedViews.push({
        name: 'main_page',
        kind: 'page',
        viewports: layouts.map(layout => layout.width),
        axe_serious_critical: skipAxe ? null : failures.length,
        layouts,
    });
    return { failures, layouts };
}

try {
    setControl(controlFile, 'normal');
    const seeded = spawnSync(python, ['-m', 'tests.e2e_seed'], {
        cwd: projectRoot,
        env: environment,
        encoding: 'utf8',
        windowsHide: true,
    });
    assert.equal(seeded.status, 0, seeded.stderr || seeded.stdout);

    fakeProcess = spawn(python, [
        '-m', 'tests.e2e_fake_ollama',
        '--port', String(ollamaPort),
        '--control-file', controlFile,
    ], {
        cwd: projectRoot,
        env: environment,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    collectProcessOutput(fakeProcess, 'fake-ollama');
    await waitUntil(async () => {
        const response = await fetch(`http://127.0.0.1:${ollamaPort}/healthz`);
        return response.ok;
    }, 'fake Ollama 启动');

    serverProcess = spawn(python, [
        '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1',
        '--port', String(appPort), '--log-level', 'warning',
    ], {
        cwd: projectRoot,
        env: environment,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    collectProcessOutput(serverProcess, 'tavern');
    await waitUntil(async () => {
        const response = await fetch(`${appUrl}/api/projects`);
        return response.ok;
    }, '本地酒馆启动');

    browser = await chromium.launch({
        headless: true,
        ...(useSystemEdge ? { executablePath: edgeExecutable } : {}),
        args: ['--disable-background-networking', '--disable-extensions'],
    });
    const browserVersion = browser.version();
    if (releaseMode && browserVersion !== EXPECTED_BROWSER_VERSION) {
        throw new Error(`Chromium 版本必须为 ${EXPECTED_BROWSER_VERSION}，实际为 ${browserVersion}`);
    }
    context = await browser.newContext({
        viewport: { width: 375, height: 812 },
        reducedMotion: 'reduce',
        locale: 'zh-CN',
    });
    await context.route('**/*', route => {
        const url = new URL(route.request().url());
        if (['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
            route.continue();
        } else {
            route.abort('blockedbyclient');
        }
    });
    const page = await context.newPage();
    page.on('pageerror', error => pageErrors.push(error.message));
    page.on('console', message => {
        if (message.type() === 'error' && !expectedHttpFailure) consoleErrors.push(message.text());
    });
    page.on('dialog', dialog => { void dialog.accept(); });
    await page.goto(appUrl, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => (
        document.readyState === 'complete'
        && document.querySelectorAll('.msg[data-message-id]').length === 2
        && document.querySelector('#model-select')?.value === 'fake-model:latest'
    ));

    // 动态 Modal 只打开审计后关闭，不执行创建、保存、重命名或导入写入。
    await auditReadOnlyModals(page);

    // 慢流取消：必须由服务端 turn 终态收口。
    setControl(controlFile, 'slow', { hold_ms: 60_000 });
    await page.locator('#user-input').fill('继续验证');
    await page.locator('#send-btn').click();
    await page.locator('#send-cancel-btn').waitFor({ state: 'visible' });
    await waitUntil(async () => {
        const session = await currentSession();
        return ['pending', 'streaming'].includes(session.message_history.at(-1)?.status);
    }, '服务端 turn 登记');
    await page.locator('#send-cancel-btn').click();
    await page.waitForFunction(() => document.querySelector('#app-status')?.textContent.includes('生成已取消'));
    await page.locator('#send-cancel-btn').waitFor({ state: 'hidden' });
    const cancelled = await currentSession();
    assert.equal(cancelled.message_history.at(-1).status, 'cancelled');

    // 正常发送与建议直接发送。
    setControl(controlFile, 'normal');
    const beforeNormal = cancelled.message_history.length;
    await page.locator('#user-input').fill('继续验证');
    await page.locator('#send-btn').click();
    await page.waitForFunction(() => document.querySelector('#app-status')?.textContent === '生成完成');
    await page.locator('.suggestion-btn').first().waitFor();
    const afterNormal = await currentSession();
    assert.equal(afterNormal.message_history.length, beforeNormal + 2);

    const suggestionText = await page.locator('.suggestion-btn').first().textContent();
    const beforeSuggestion = afterNormal.message_history.length;
    await page.locator('.suggestion-btn').first().click();
    await page.waitForFunction(expected => (
        document.querySelector('#user-input')?.value === ''
        && document.querySelectorAll('.msg.user .content')
            .item(document.querySelectorAll('.msg.user .content').length - 1)?.textContent === expected
    ), suggestionText);
    const afterSuggestion = await waitUntil(async () => {
        const session = await currentSession();
        return session.message_history.length === beforeSuggestion + 2
            && session.message_history.at(-1)?.status === 'completed'
            ? session
            : null;
    }, '建议发送终态');
    assert.equal(afterSuggestion.message_history.length, beforeSuggestion + 2);
    // 服务端已提交不等于前端已完成权威重载；等待写锁真正释放。
    await page.waitForFunction(() => document.querySelector('#app-status')?.textContent === '生成完成');
    await page.waitForFunction(() => {
        const buttons = document.querySelectorAll('.msg.user .msg-action-btn.edit');
        const button = buttons.item(buttons.length - 1);
        return Boolean(button && !button.disabled);
    });

    // 消息编辑使用 Ctrl+Enter 提交。
    const lastUser = page.locator('.msg.user').last();
    await lastUser.locator('.msg-action-btn.edit').focus();
    await lastUser.locator('.msg-action-btn.edit').press('Enter');
    const editor = lastUser.locator('.message-edit-input');
    await editor.fill('继续验证（编辑）');
    await editor.press('Control+Enter');
    await lastUser.locator('.content').filter({ hasText: '继续验证（编辑）' }).waitFor();

    // 快照列表、预览与恢复均走用户界面。
    const snapshot = await createSnapshot();
    assert.equal(snapshot.snapshotted, true);
    await page.locator('#history-btn').click();
    await page.locator('.history-preview').first().click();
    await page.locator('.snapshot-preview .snapshot-filename').filter({ hasText: snapshot.filename }).waitFor();
    await page.locator('#modal-close').click();
    await page.locator('#history-btn').click();
    await page.locator(`.history-restore[data-fn="${snapshot.filename}"]`).click();
    await page.locator('#modal-backdrop').waitFor({ state: 'hidden' });

    // 存档与项目切换。
    await switchSave(page, '切换存档');
    assert.equal((await currentSession('默认项目', '切换存档')).session_id, '切换存档');
    await switchSave(page, '默认存档');
    await switchProject(page, '切换项目');
    assert.equal(await page.locator('#project-name').textContent(), '切换项目');
    await switchProject(page, '默认项目');

    // 恶意导入不得生成可执行 DOM，错误只以文本呈现。
    await page.evaluate(() => { window.__tavernE2eXss = 0; });
    await openSaveDropdown(page);
    const chooserPromise = page.waitForEvent('filechooser');
    await page.locator('#save-import-inline').click();
    const chooser = await chooserPromise;
    expectedHttpFailure = true;
    await chooser.setFiles({
        name: 'malicious.json',
        mimeType: 'application/json',
        buffer: Buffer.from('<img src=x onerror="window.__tavernE2eXss=1">', 'utf8'),
    });
    await page.waitForFunction(() => document.querySelector('#save-import-status')?.textContent.includes('导入失败'));
    expectedHttpFailure = false;
    const importSafety = await page.evaluate(() => ({
        status: document.querySelector('#save-import-status')?.textContent || '',
        executableNodes: document.querySelectorAll('#modal-body script,#modal-body img').length,
        xss: window.__tavernE2eXss,
    }));
    assert.match(importSafety.status, /^导入失败：/);
    assert.equal(importSafety.executableNodes, 0);
    assert.equal(importSafety.xss, 0);
    await page.locator('#modal-close').click();

    const viewportAudit = await auditViewports(page);
    const layout = await page.evaluate(() => ({
        width: document.documentElement.clientWidth,
        horizontalOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        offenders: [document.documentElement, document.body, ...document.querySelectorAll('body *')]
            .filter(element => {
                const style = getComputedStyle(element);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && (rect.right > document.documentElement.clientWidth + 0.1 || rect.left < -0.1);
            })
            .slice(0, 12)
            .map(element => ({
                tag: element.tagName,
                id: element.id,
                className: typeof element.className === 'string' ? element.className : '',
                left: Math.round(element.getBoundingClientRect().left),
                right: Math.round(element.getBoundingClientRect().right),
                width: Math.round(element.getBoundingClientRect().width),
            })),
    }));
    assert.equal(layout.horizontalOverflow <= 0, true, JSON.stringify(layout));
    assert.deepEqual(pageErrors, []);
    assert.deepEqual(consoleErrors, []);

    console.log(JSON.stringify({
        status: 'passed',
        reproducible_browser: !useSystemEdge,
        axe_executed: !skipAxe,
        release_gate: !useSystemEdge && !skipAxe,
        playwright_version: playwrightPackage.version,
        axe_version: axeVersion,
        browser_version: browserVersion,
        functional_flows: [
            'send', 'cancel', 'suggestion', 'project_switch', 'save_switch',
            'message_edit', 'snapshot_preview_restore', 'malicious_import',
        ],
        axe_serious_critical: skipAxe ? null : viewportAudit.failures.length,
        audited_views: auditedViews,
        viewport_layouts: viewportAudit.layouts,
        layout,
    }, null, 2));
} catch (error) {
    const diagnostic = processOutput.join('').slice(-8_000);
    if (diagnostic) console.error(diagnostic);
    console.error(JSON.stringify({ pageErrors, consoleErrors }, null, 2));
    throw error;
} finally {
    expectedHttpFailure = false;
    if (context) await context.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
    await stopChild(serverProcess);
    await stopChild(fakeProcess);
    const removed = await removeTempRoot(tempRoot);
    if (!removed) console.error(`TEMP_CLEANUP_PENDING ${tempRoot}`);
}
