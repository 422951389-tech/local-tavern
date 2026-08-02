import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import {
    existsSync,
    mkdirSync,
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
const screenshotPath = process.env.TAVERN_E2E_SCREENSHOT
    ? path.resolve(process.env.TAVERN_E2E_SCREENSHOT)
    : null;
const performanceOutputPath = process.env.TAVERN_E2E_PERFORMANCE_OUTPUT
    ? path.resolve(process.env.TAVERN_E2E_PERFORMANCE_OUTPUT)
    : null;
function screenshotSibling(suffix) {
    if (!screenshotPath) return null;
    const parsed = path.parse(screenshotPath);
    return path.join(parsed.dir, `${parsed.name}-${suffix}${parsed.ext || '.png'}`);
}
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
    TAVERN_PROVIDER_DATA_DIR: path.join(tempRoot, 'provider-data'),
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

async function startTavernServer() {
    const child = spawn(python, [
        '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1',
        '--port', String(appPort), '--log-level', 'warning',
    ], {
        cwd: projectRoot,
        env: environment,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    collectProcessOutput(child, 'tavern');
    await waitUntil(async () => {
        const response = await fetch(`${appUrl}/api/projects`);
        return response.ok;
    }, '本地酒馆启动');
    return child;
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

async function openSaveManager(page) {
    await page.locator('#tab-saves').click();
    await page.locator('.save-manager').waitFor({ state: 'visible' });
}

async function switchSave(page, save) {
    await openSaveManager(page);
    await page.locator('.save-manager-card').filter({ hasText: save })
        .getByRole('button', { name: '切换到这个存档' }).click();
    await page.locator('#current-session-label').filter({ hasText: save }).waitFor();
    await closeModalAfterAudit(page);
}

async function switchProject(page, project) {
    await page.locator('#project-btn').click();
    await page.locator('#project-dropdown').waitFor({ state: 'visible' });
    await page.locator(`.dropdown-item[data-project="${project}"]`).click();
    await page.locator('#project-name').filter({ hasText: project }).waitFor();
}

async function clickResponsiveTool(page, selector, moreButtonSelector, menuSelector) {
    const tool = page.locator(selector);
    if (await tool.isVisible()) {
        await tool.click();
        return;
    }
    await page.locator(moreButtonSelector).click();
    await page.locator(menuSelector).waitFor({ state: 'visible' });
    await tool.click();
}

async function clickTopbarTool(page, selector) {
    return clickResponsiveTool(page, selector, '#topbar-more-btn', '#topbar-more-menu');
}

async function clickNavTool(page, selector) {
    return clickResponsiveTool(page, selector, '#nav-more-btn', '#nav-more-menu');
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

async function assertWorldWorkspaceLayout(page, name) {
    const layout = await page.evaluate(() => {
        const modal = document.querySelector('#modal');
        const body = document.querySelector('#modal-body');
        const workspace = document.querySelector('.world-workspace');
        const rect = element => element?.getBoundingClientRect();
        const modalRect = rect(modal);
        const bodyRect = rect(body);
        const workspaceRect = rect(workspace);
        return {
            pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            bodyOverflow: body ? body.scrollWidth - body.clientWidth : null,
            workspaceOverflow: workspace ? workspace.scrollWidth - workspace.clientWidth : null,
            unusedBottom: modalRect && bodyRect ? Math.round((modalRect.bottom - bodyRect.bottom) * 10) / 10 : null,
            workspaceFillsBody: Boolean(
                bodyRect && workspaceRect
                && Math.abs(bodyRect.height - workspaceRect.height) <= 1
            ),
        };
    });
    assert.equal(layout.pageOverflow <= 0, true, `${name}: 页面横向溢出`);
    assert.equal(layout.bodyOverflow <= 0, true, `${name}: 工作台容器横向溢出`);
    assert.equal(layout.workspaceOverflow <= 0, true, `${name}: 世界工作台横向溢出`);
    assert.equal(layout.unusedBottom <= 1, true, `${name}: 工作台底部存在无效空白`);
    assert.equal(layout.workspaceFillsBody, true, `${name}: 工作台没有填满可用高度`);
}

async function auditReadOnlyModals(page) {
    await clickTopbarTool(page, '#model-params-btn');
    await auditOpenModal(page, 'model_params');
    await closeModalAfterAudit(page);

    await page.locator('#provider-settings-btn').click();
    await page.locator('.provider-settings').waitFor({ state: 'visible' });
    await page.locator('.provider-list-add').click();
    const providerPreset = page.locator('#provider-new-preset');
    await providerPreset.selectOption('anthropic');
    assert.equal(await page.locator('#provider-new-kind').inputValue(), 'anthropic');
    assert.equal(await page.locator('#provider-new-url').inputValue(), 'https://api.anthropic.com');
    assert.equal(await page.locator('#provider-new-key').getAttribute('type'), 'password');
    assert.match(
        await page.locator('.provider-cloud-warning').textContent(),
        /角色卡|世界书|对话历史/,
    );
    await auditOpenModal(page, 'provider_settings_anthropic', {
        statusSelectors: ['.provider-live'],
    });
    await closeModalAfterAudit(page);

    await openSaveManager(page);
    assert.match(await page.locator('.save-manager').textContent(), /新建存档.*导入存档.*当前存档.*存档名称.*保存名称.*导出备份/s);
    await auditOpenModal(page, 'save_manager', { statusSelectors: ['.save-manager-status'] });
    if (screenshotPath) {
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.screenshot({ path: screenshotSibling('module-saves-1440'), animations: 'disabled' });
        await page.setViewportSize({ width: 375, height: 812 });
    }
    await closeModalAfterAudit(page);

    await clickTopbarTool(page, '#history-btn');
    await page.waitForFunction(() => document.querySelector('#history-status')?.textContent !== '加载中…');
    await auditOpenModal(page, 'history', { statusSelectors: ['#history-status'] });
    await closeModalAfterAudit(page);

    await clickNavTool(page, '#tab-chars');
    await page.locator('#ce-status').waitFor({ state: 'attached' });
    await auditOpenModal(page, 'character_cards', { statusSelectors: ['#ce-status'] });
    if (screenshotPath) {
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.screenshot({ path: screenshotSibling('module-characters-1440'), animations: 'disabled' });
        await page.setViewportSize({ width: 375, height: 812 });
    }
    await closeModalAfterAudit(page);

    await clickNavTool(page, '#tab-relations');
    await page.locator('.relationship-editor').waitFor({ state: 'visible' });
    await auditOpenModal(page, 'relationships');
    if (screenshotPath) {
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.screenshot({ path: screenshotSibling('module-relationships-1440'), animations: 'disabled' });
        await page.setViewportSize({ width: 375, height: 812 });
    }
    await closeModalAfterAudit(page);

    await clickNavTool(page, '#tab-user');
    await page.locator('.card-editor-user').waitFor({ state: 'visible' });
    await auditOpenModal(page, 'user_profile', { statusSelectors: ['#ce-status'] });
    if (screenshotPath) {
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.screenshot({ path: screenshotSibling('module-user-1440'), animations: 'disabled' });
        await page.setViewportSize({ width: 375, height: 812 });
    }
    await closeModalAfterAudit(page);
}

async function exerciseProductTools(page) {
    await clickTopbarTool(page, '#diagnostics-center-btn');
    await page.locator('.diagnostics-snapshot').waitFor();
    assert.match(await page.locator('.diagnostics-center').textContent(), /整体状态.*健康检查.*不包含.*API Key/s);
    if (screenshotPath) await page.screenshot({ path: screenshotSibling('diagnostics'), animations: 'disabled' });
    await page.locator('.diagnostics-center').getByRole('button', { name: '预览故障报告' }).click();
    await page.locator('.fault-report-preview').waitFor({ state: 'visible' });
    assert.match(await page.locator('.fault-report-preview').textContent(), /故障报告预览.*没有对话、设定正文和凭据/s);
    const downloadPromise = page.waitForEvent('download');
    await page.locator('.fault-report-preview').getByRole('button', { name: '导出故障报告（JSON）' }).click();
    const download = await downloadPromise;
    assert.match(download.suggestedFilename(), /^local-tavern-fault-report-\d{4}-\d{2}-\d{2}\.json$/);
    await closeModalAfterAudit(page);

    await page.locator('#world-context-bar').click();
    await page.locator('.world-workspace').waitFor();
    assert.match(await page.locator('.world-workspace').textContent(), /世界设定.*当前世界变化.*设定关联/s);
    await page.locator('.world-state-workspace').waitFor();
    await page.locator('.world-workspace-mode', { hasText: '世界设定' }).click();
    await page.locator('.worldbook-editor').waitFor();
    assert.match(await page.locator('.worldbook-editor').textContent(), /整个项目中共用.*基本内容.*详细设定.*什么时候参考.*谁知道这件事.*关联角色与其他设定.*专业设置/s);
    if (await page.locator('.worldbook-entry-row').count() === 0) {
        const worldbook = page.locator('.worldbook-editor');
        await worldbook.getByLabel('设定名称').fill('琉璃宫');
        await worldbook.getByLabel('一句话介绍（可选）').fill('帝都权力中心');
        await worldbook.getByLabel('设定类型', { exact: true }).selectOption('location');
        await worldbook.getByLabel('详细内容').fill('琉璃宫是帝都的权力中心。');
        await worldbook.getByRole('button', { name: '保存设定' }).click();
        await page.locator('.worldbook-entry-row').filter({ hasText: '琉璃宫' }).waitFor();

        await worldbook.getByRole('button', { name: '新建设定' }).click();
        await worldbook.getByLabel('设定名称').fill('花园');
        await worldbook.getByLabel('一句话介绍（可选）').fill('琉璃宫内的会面地点');
        await worldbook.getByLabel('设定类型', { exact: true }).selectOption('location');
        await worldbook.getByLabel('详细内容').fill('花园位于琉璃宫内。');
        await worldbook.getByText('关联角色与其他设定', { exact: true }).click();
        await worldbook.getByLabel('搜索关联世界设定').fill('琉璃宫');
        await worldbook.locator('.worldbook-field-linked_entry_ids .worldbook-entity-choice').filter({ hasText: '琉璃宫' }).locator('input').check();
        await worldbook.getByRole('button', { name: '保存设定' }).click();
        await page.locator('.worldbook-entry-row').filter({ hasText: '花园' }).waitFor();
    }
    await page.locator('.worldbook-entry-row').filter({ hasText: '花园' }).getByRole('button').first().click();
    const gardenLinkState = await page.evaluate(() => ({
        inspector: document.querySelector('.worldbook-selection-inspector')?.textContent || '',
        checked: [...document.querySelectorAll('.worldbook-field-linked_entry_ids input[type="checkbox"]:checked')]
            .map(input => input.value),
    }));
    assert.match(gardenLinkState.inspector, /世界设定 1/, JSON.stringify(gardenLinkState));
    assert.equal(gardenLinkState.checked.length, 1, JSON.stringify(gardenLinkState));
    await page.locator('.worldbook-editor').getByText('关联角色与其他设定', { exact: true }).click();
    await page.getByRole('button', { name: '＋ 新建关联设定' }).waitFor();
    await page.locator('.world-workspace-mode', { hasText: '设定关联' }).click();
    await page.locator('.world-relations-workspace').waitFor();
    await page.locator('.world-relation-node').first().waitFor();
    await page.locator('.world-workspace-mode', { hasText: '当前世界变化' }).click();
    await page.locator('.world-state-sidebar').getByRole('button', { name: '＋ 记录变化' }).click();
    await page.locator('.world-change-editor').waitFor();
    await page.locator('.world-change-editor').getByLabel('变化标题').fill('浏览器世界变化');
    await page.locator('.world-change-editor').getByLabel('影响与结果').fill('用于验证新增、编辑、状态流转和删除。');
    await page.locator('.world-change-editor').getByLabel('类型').selectOption('event');
    const relatedChoice = page.locator('.world-change-editor .world-picker').filter({ hasText: '关联世界设定' }).locator('input[type="checkbox"]').first();
    if (await relatedChoice.count()) await relatedChoice.check();
    await page.locator('.world-change-editor').getByRole('button', { name: '记录并应用' }).click();
    const browserChange = page.locator('.world-change-timeline-card').filter({ hasText: '浏览器世界变化' });
    await browserChange.waitFor({ state: 'attached' });
    await page.locator('.world-change-editor').getByLabel('状态').selectOption('resolved');
    await page.locator('.world-change-editor').getByRole('button', { name: '保存并应用' }).click();
    await page.waitForFunction(() => document.querySelector('.world-change-timeline-card.active .world-change-status')?.textContent === '已解决');
    await assertWorldWorkspaceLayout(page, 'world_state_375');
    if (screenshotPath) {
        await page.screenshot({ path: screenshotSibling('world_context-375'), animations: 'disabled' });
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.locator('.world-workspace-mode', { hasText: '世界设定' }).click();
        await page.locator('.worldbook-editor').waitFor();
        await assertWorldWorkspaceLayout(page, 'world_library_1440');
        await page.screenshot({ path: screenshotSibling('world_library-1440'), animations: 'disabled' });
        await page.locator('.world-workspace-mode', { hasText: '设定关联' }).click();
        await page.locator('.world-relations-workspace').waitFor();
        await assertWorldWorkspaceLayout(page, 'world_relations_1440');
        await page.screenshot({ path: screenshotSibling('world_relations-1440'), animations: 'disabled' });
        await page.setViewportSize({ width: 768, height: 900 });
        await assertWorldWorkspaceLayout(page, 'world_relations_768');
        await page.screenshot({ path: screenshotSibling('world_relations-768'), animations: 'disabled' });
        await page.locator('.world-workspace-mode', { hasText: '当前世界变化' }).click();
        await page.locator('.world-state-workspace').waitFor();
        await browserChange.click();
        await page.locator('.world-change-editor').getByRole('button', { name: '删除记录' }).waitFor();
        await assertWorldWorkspaceLayout(page, 'world_state_768');
        await page.screenshot({ path: screenshotSibling('world_state-768'), animations: 'disabled' });
        await page.setViewportSize({ width: 375, height: 760 });
    }
    await page.locator('.world-change-editor').getByRole('button', { name: '删除记录' }).click();
    await browserChange.waitFor({ state: 'detached' });
    await closeModalAfterAudit(page);

    await clickTopbarTool(page, '#backup-center-btn');
    await page.waitForFunction(() => {
        const status = document.querySelector('.backup-center .product-status');
        return status && !status.textContent.includes('正在读取');
    });
    await page.locator('.backup-center .product-compact-input').fill('浏览器端验证');
    await page.locator('.backup-center').getByRole('button', { name: '立即备份' }).click();
    const backupCard = page.locator('.backup-card').filter({ hasText: '浏览器端验证' }).first();
    await backupCard.waitFor();
    const modalToastGuard = await page.evaluate(() => {
        const backdrop = document.querySelector('#modal-backdrop');
        const title = document.querySelector('#modal-title');
        const toast = document.querySelector('#toast-msg');
        return {
            modalOpen: Boolean(backdrop && !backdrop.classList.contains('hidden')),
            title: title?.textContent || '',
            toastShown: Boolean(toast?.classList.contains('show')),
            toastOpacity: toast ? getComputedStyle(toast).opacity : '0',
        };
    });
    assert.deepEqual(modalToastGuard, {
        modalOpen: true,
        title: '备份中心',
        toastShown: false,
        toastOpacity: '0',
    });
    if (screenshotPath) await page.screenshot({ path: screenshotSibling('modal-toast-guard'), animations: 'disabled' });
    await backupCard.getByRole('button', { name: '预检' }).click();
    await backupCard.locator('.product-card-status').filter({ hasText: '预检完成' }).waitFor();
    await backupCard.getByRole('button', { name: '恢复演练' }).click();
    await backupCard.locator('.product-card-status').filter({ hasText: /演练通过|演练结果/ }).waitFor();
    await backupCard.getByRole('button', { name: '恢复', exact: true }).click();
    await page.locator('#modal-title').filter({ hasText: '确认恢复备份' }).waitFor();
    assert.match(await page.locator('.backup-restore-confirmation').textContent(), /新增.*替换.*移除.*恢复前会自动创建保护备份/s);
    if (screenshotPath) await page.screenshot({ path: screenshotSibling('backup-restore'), animations: 'disabled' });
    await closeModalAfterAudit(page);

    await clickTopbarTool(page, '#memory-notes-btn');
    const form = page.locator('.memory-note-form');
    await form.locator('textarea').nth(0).fill('浏览器长期记忆：测试角色喜欢晴天。');
    await form.locator('select').selectOption('test_character');
    await form.locator('input[type="text"]').fill('验证日');
    await form.locator('textarea').nth(1).fill('喜欢晴天');
    await form.locator('textarea').nth(2).fill('与测试用户共同完成验证');
    await form.getByRole('button', { name: '添加记忆' }).click();
    const note = page.locator('.memory-note-card').filter({ hasText: '浏览器长期记忆' });
    await note.waitFor();
    assert.match(await note.textContent(), /测试角色.*喜欢晴天.*共同完成验证/s);
    if (screenshotPath) await page.screenshot({ path: screenshotSibling('memory-notes'), animations: 'disabled' });
    await note.getByRole('button', { name: '编辑' }).click();
    await form.locator('textarea').nth(0).fill('浏览器长期记忆：测试角色喜欢晴朗午后。');
    await form.getByRole('button', { name: '保存修改' }).click();
    await page.locator('.memory-note-card').filter({ hasText: '晴朗午后' }).waitFor();
    await page.locator('.memory-note-card').filter({ hasText: '晴朗午后' })
        .getByRole('button', { name: '删除' }).click();
    await page.locator('.memory-note-items .product-empty').waitFor();
    await closeModalAfterAudit(page);
}

function firstCommandLine(command, arguments_) {
    const completed = spawnSync(command, arguments_, {
        cwd: projectRoot,
        encoding: 'utf8',
        windowsHide: true,
    });
    if (completed.status !== 0) return 'unavailable';
    const line = String(completed.stdout || completed.stderr || '').trim().split(/\r?\n/)[0];
    return line || 'unavailable';
}

function sourceCommit() {
    const value = firstCommandLine('git', ['rev-parse', '--verify', 'HEAD']).trim();
    return /^[0-9a-f]{40}$/i.test(value) ? value : 'unavailable';
}

function writePerformanceDocument(targetPath, document) {
    mkdirSync(path.dirname(targetPath), { recursive: true });
    const temporary = path.join(
        path.dirname(targetPath),
        `.${path.basename(targetPath)}.${process.pid}.tmp`,
    );
    writeFileSync(temporary, `${JSON.stringify(document, null, 2)}\n`, 'utf8');
    renameSync(temporary, targetPath);
}

async function measureBrowserPerformance(browserInstance, browserVersion) {
    const performanceErrors = [];
    const performanceContext = await browserInstance.newContext({
        viewport: { width: 1440, height: 900 },
        reducedMotion: 'reduce',
        locale: 'zh-CN',
    });
    await performanceContext.addInitScript(key => {
        try { localStorage.setItem(key, 'complete'); } catch (_error) {}
    }, 'local-tavern.onboarding.v1');
    await performanceContext.route('**/*', route => {
        const url = new URL(route.request().url());
        if (['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) route.continue();
        else route.abort('blockedbyclient');
    });
    const performancePage = await performanceContext.newPage();
    performancePage.on('pageerror', error => performanceErrors.push(error.message));

    try {
        const firstPaintSamples = [];
        const firstScreenSamples = [];
        for (let index = 0; index < 4; index += 1) {
            await performancePage.goto(`${appUrl}/?performance_run=${index}`, {
                waitUntil: 'domcontentloaded',
            });
            await performancePage.waitForFunction(() => {
                const initializationError = document.querySelector('#init-error');
                const errorVisible = initializationError
                    && !initializationError.classList.contains('hidden');
                const model = document.querySelector('#model-select');
                return document.readyState === 'complete'
                    && !errorVisible
                    && document.querySelectorAll('.msg[data-message-id]').length >= 2
                    && model && model.value && model.value !== 'loading'
                    && (!document.fonts || document.fonts.status === 'loaded')
                    && performance.getEntriesByName('first-contentful-paint').length > 0;
            });
            const timing = await performancePage.evaluate(() => ({
                firstContentfulPaint: performance.getEntriesByName('first-contentful-paint')[0].startTime,
                firstScreenReady: performance.now(),
            }));
            if (index > 0) {
                firstPaintSamples.push(Number(timing.firstContentfulPaint.toFixed(3)));
                firstScreenSamples.push(Number(timing.firstScreenReady.toFixed(3)));
            }
        }

        const renderSamples = [];
        for (let index = 0; index < 4; index += 1) {
            const result = await performancePage.evaluate(async messageCount => {
                const { createMessageElement, mountMessageHistory } = await import('/static/conversation-view.mjs');
                const history = Array.from({ length: messageCount }, (_unused, messageIndex) => ({
                    id: `00000000-0000-4000-8000-${String(messageIndex).padStart(12, '0')}`,
                    role: messageIndex % 2 === 0 ? 'user' : 'assistant',
                    content: `性能渲染消息 ${messageIndex}：固定长度的剧情与对话文本。`,
                    in_prompt: true,
                    status: 'completed',
                }));
                const host = document.createElement('section');
                host.style.cssText = 'position:absolute;left:-100000px;top:0;width:800px;contain:layout style;';
                host.setAttribute('aria-hidden', 'true');
                document.body.appendChild(host);
                const started = performance.now();
                const mounted = mountMessageHistory(document, host, history, message => (
                    createMessageElement(document, {
                        role: message.role,
                        content: message.content,
                        message,
                        timeText: '',
                    }).container
                ));
                const height = host.offsetHeight;
                const elapsed = performance.now() - started;
                const rendered = host.querySelectorAll('.msg[data-message-id]').length;
                host.remove();
                return { elapsed, mounted, rendered, height };
            }, 1000);
            assert.equal(result.mounted, 1000, '性能探针必须挂载 1000 条消息');
            assert.equal(result.rendered, 1000, '性能探针 DOM 必须包含 1000 条消息');
            assert.equal(result.height > 0, true, '性能探针必须触发布局计算');
            if (index > 0) renderSamples.push(Number(result.elapsed.toFixed(3)));
        }
        assert.deepEqual(performanceErrors, []);
        return {
            schema_version: 1,
            source: 'tests/browser_e2e.mjs:playwright-browser-probe',
            environment: {
                os: `${process.platform}-${process.arch}`,
                python: firstCommandLine(python, ['--version']),
                node: process.version,
                browser: `Chromium ${browserVersion}`,
                source_commit: sourceCommit(),
            },
            warmup_runs: 1,
            metrics: {
                browser_first_contentful_paint_ms: {
                    unit: 'ms',
                    samples: firstPaintSamples,
                },
                browser_first_screen_ready_ms: {
                    unit: 'ms',
                    samples: firstScreenSamples,
                },
                browser_render_1000_messages_ms: {
                    unit: 'ms',
                    samples: renderSamples,
                    message_count: 1000,
                },
            },
        };
    } finally {
        await performanceContext.close();
    }
}

async function auditViewports(page) {
    const failures = [];
    const layouts = [];
    for (const width of [375, 768, 899, 1024, 1366, 1424, 1440]) {
        await page.setViewportSize({ width, height: width === 375 ? 812 : 900 });
        const expectedShellMode = width >= 1440 ? 'full' : (width >= 900 ? 'rail' : 'single');
        await page.waitForFunction(expectedMode => {
            const shell = document.querySelector('.workspace-shell');
            if (!shell) return false;
            const columns = getComputedStyle(shell).gridTemplateColumns
                .split(/\s+/).filter(Boolean).map(Number.parseFloat);
            if (expectedMode === 'single') return columns.length === 1;
            if (columns.length !== 3) return false;
            return expectedMode === 'full' ? columns[0] >= 240 : columns[0] <= 100;
        }, expectedShellMode);
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
        const layout = await page.evaluate(() => {
            const viewportWidth = document.documentElement.clientWidth;
            const compact = viewportWidth <= 899;
            const required = [
                '#project-btn', '#tab-saves', '#provider-settings-btn', '#model-select',
                '#inspector-toggle', '#user-input', '#send-btn',
                ...(compact ? ['#nav-more-btn', '#topbar-more-btn'] : []),
            ];
            const controls = required.map(selector => {
                const element = document.querySelector(selector);
                const rect = element?.getBoundingClientRect();
                const style = element ? getComputedStyle(element) : null;
                return {
                    selector,
                    visible: Boolean(element && style.display !== 'none' && style.visibility !== 'hidden'
                        && rect.width > 0 && rect.height > 0),
                    left: rect ? Math.round(rect.left * 10) / 10 : null,
                    right: rect ? Math.round(rect.right * 10) / 10 : null,
                    width: rect ? Math.round(rect.width * 10) / 10 : null,
                    height: rect ? Math.round(rect.height * 10) / 10 : null,
                };
            });
            const action = document.querySelector('.msg-action-btn');
            const actionGroup = action?.closest('.msg-actions');
            const actionRect = action?.getBoundingClientRect();
            const actionGroupStyle = actionGroup ? getComputedStyle(actionGroup) : null;
            const transitionDurations = getComputedStyle(document.querySelector('#character-panel'))
                .transitionDuration.split(',').map(value => {
                    const normalized = value.trim();
                    return normalized.endsWith('ms')
                        ? Number.parseFloat(normalized)
                        : Number.parseFloat(normalized) * 1000;
                });
            const send = document.querySelector('#send-btn');
            const sendLabel = send?.querySelector('span:last-child');
            const sendRect = send?.getBoundingClientRect();
            const labelRect = sendLabel?.getBoundingClientRect();
            const sendStyle = send ? getComputedStyle(send) : null;
            const paddingRight = Number.parseFloat(sendStyle?.paddingRight || '0') || 0;
            const rightHit = sendRect ? document.elementFromPoint(
                Math.max(sendRect.left + 1, sendRect.right - 3),
                sendRect.top + (sendRect.height / 2),
            ) : null;
            const composerSendAudit = {
                missing: !send || !sendLabel || !sendRect || !labelRect,
                contentOverflow: Boolean(send && send.scrollWidth > send.clientWidth + 1),
                labelOverflow: Boolean(
                    sendRect && labelRect
                    && labelRect.width > 0
                    && labelRect.right > sendRect.right - paddingRight + 0.5
                ),
                rightEdgeOccluded: Boolean(
                    send && !(rightHit === send || send.contains(rightHit))
                ),
                rightHit: rightHit?.id || rightHit?.className || rightHit?.tagName || '',
            };
            const bounds = selector => {
                const rect = document.querySelector(selector)?.getBoundingClientRect();
                return rect ? {
                    left: Math.round(rect.left * 10) / 10,
                    right: Math.round(rect.right * 10) / 10,
                    width: Math.round(rect.width * 10) / 10,
                } : null;
            };
            return {
                width: viewportWidth,
                horizontalOverflow: document.documentElement.scrollWidth - viewportWidth,
                controls,
                invalidControlBounds: controls.filter(control => (
                    !control.visible || control.left < -0.1 || control.right > viewportWidth + 0.1
                )),
                coarseAction: actionRect ? {
                    width: Math.round(actionRect.width * 10) / 10,
                    height: Math.round(actionRect.height * 10) / 10,
                    opacity: actionGroupStyle?.opacity || '',
                    pointerEvents: actionGroupStyle?.pointerEvents || '',
                } : null,
                reducedMotionMaxMs: Math.max(...transitionDurations),
                composerSendAudit,
                composerGeometry: {
                    shellColumns: getComputedStyle(document.querySelector('.workspace-shell'))
                        .gridTemplateColumns,
                    center: bounds('.workspace-center'),
                    inputBar: bounds('#input-bar'),
                    heading: bounds('.composer-heading'),
                    row: bounds('.composer-row'),
                    send: bounds('#send-btn'),
                },
                composerSendClipped: composerSendAudit.missing
                    || composerSendAudit.contentOverflow
                    || composerSendAudit.labelOverflow
                    || composerSendAudit.rightEdgeOccluded,
            };
        });
        layouts.push(layout);
        assert.equal(layout.horizontalOverflow <= 0, true, JSON.stringify(layout));
        assert.deepEqual(layout.invalidControlBounds, [], JSON.stringify(layout));
        assert.equal(Boolean(
            layout.coarseAction
            && layout.coarseAction.width >= 44
            && layout.coarseAction.height >= 44
            && layout.coarseAction.opacity === '1'
            && layout.coarseAction.pointerEvents === 'auto'
        ), true, JSON.stringify(layout));
        assert.equal(layout.reducedMotionMaxMs <= 0.02, true, JSON.stringify(layout));
        assert.equal(layout.composerSendClipped, false, JSON.stringify(layout));
        if (width <= 899) {
            for (const [buttonSelector, menuSelector] of [
                ['#nav-more-btn', '#nav-more-menu'],
                ['#topbar-more-btn', '#topbar-more-menu'],
            ]) {
                await page.locator(buttonSelector).click();
                await page.locator(menuSelector).waitFor({ state: 'visible' });
                const menuBounds = await page.locator(menuSelector).evaluate(menu => {
                    const rect = menu.getBoundingClientRect();
                    const targets = [...menu.querySelectorAll('button')].map(button => {
                        const target = button.getBoundingClientRect();
                        return { width: target.width, height: target.height };
                    });
                    return { left: rect.left, right: rect.right, targets };
                });
                assert.equal(menuBounds.left >= -0.1 && menuBounds.right <= width + 0.1, true, JSON.stringify(menuBounds));
                assert.equal(menuBounds.targets.every(target => target.height >= 44), true, JSON.stringify(menuBounds));
                await page.keyboard.press('Escape');
                await page.locator(menuSelector).waitFor({ state: 'hidden' });
            }
        }
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

    serverProcess = await startTavernServer();

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
        hasTouch: true,
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
    await page.locator('#modal-title').filter({ hasText: '欢迎使用本地酒馆' }).waitFor();
    assert.match(await page.locator('.onboarding').textContent(), /本地 Ollama.*云端 API.*角色卡.*世界书.*API Key/s);
    if (screenshotPath) await page.screenshot({ path: screenshotSibling('onboarding'), animations: 'disabled' });
    await page.locator('.onboarding-card.recommended .primary-btn').click();
    await page.locator('#modal-backdrop').waitFor({ state: 'hidden' });
    assert.equal(
        await page.evaluate(() => localStorage.getItem('local-tavern.onboarding.v1')),
        'complete',
    );
    const legacySession = await currentSession();
    assert.equal(Object.hasOwn(legacySession.message_history[1], 'presentation'), false);
    assert.equal(await page.locator('.msg.assistant').first().locator('.story-module').count(), 1);
    assert.equal(await page.locator('.msg.assistant').first().locator('.characters-module').count(), 1);
    assert.match(
        await page.locator('.msg.assistant').first().locator('.affinity-copy').innerText(),
        /88\/100.*深厚/s,
    );
    assert.equal(
        await page.locator('.msg.assistant').first().locator('.character-state-details').evaluate(element => element.open),
        false,
    );

    // 中文输入法组合期间的 Enter/229 不得触发发送。
    const beforeIme = await currentSession();
    await page.locator('#user-input').fill('输入法组合中的文本');
    const imeDispatch = await page.locator('#user-input').evaluate(input => {
        const composing = new KeyboardEvent('keydown', {
            key: 'Enter', bubbles: true, cancelable: true,
        });
        Object.defineProperty(composing, 'isComposing', { value: true });
        const composingAccepted = input.dispatchEvent(composing);
        const legacy229 = new KeyboardEvent('keydown', {
            key: 'Enter', bubbles: true, cancelable: true,
        });
        Object.defineProperty(legacy229, 'keyCode', { value: 229 });
        const legacyAccepted = input.dispatchEvent(legacy229);
        return { composingAccepted, legacyAccepted, value: input.value };
    });
    await delay(150);
    assert.deepEqual(imeDispatch, {
        composingAccepted: true,
        legacyAccepted: true,
        value: '输入法组合中的文本',
    });
    assert.equal((await currentSession()).message_history.length, beforeIme.message_history.length);
    assert.equal(await page.locator('#user-input').inputValue(), '输入法组合中的文本');
    await page.locator('#user-input').fill('');

    // 窄屏角色检视器具备 Escape、内部关闭、遮罩关闭与焦点归还。
    assert.equal(await page.locator('#inspector-toggle').getAttribute('aria-expanded'), 'false');
    await page.locator('#inspector-toggle').click();
    await page.locator('#character-panel').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#character-panel').getAttribute('role'), 'dialog');
    assert.equal(await page.locator('#character-panel').getAttribute('aria-modal'), 'true');
    assert.equal(await page.locator('#inspector-backdrop').getAttribute('aria-hidden'), 'false');
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => document.querySelector('#inspector-toggle')?.getAttribute('aria-expanded') === 'false');
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'inspector-toggle');
    await page.locator('#inspector-toggle').click();
    await page.locator('#inspector-close').click();
    await page.waitForFunction(() => document.querySelector('#inspector-toggle')?.getAttribute('aria-expanded') === 'false');
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'inspector-toggle');
    await page.locator('#inspector-toggle').click();
    await page.locator('#inspector-backdrop').click({ position: { x: 8, y: 8 } });
    await page.waitForFunction(() => document.querySelector('#inspector-toggle')?.getAttribute('aria-expanded') === 'false');
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'inspector-toggle');

    // 动态 Modal 只打开审计后关闭，不执行创建、保存、重命名或导入写入。
    await auditReadOnlyModals(page);
    await exerciseProductTools(page);

    // 事件流连续失败并耗尽重连后，取消仍须主动同步终态并在当前页面解锁。
    setControl(controlFile, 'slow', { hold_ms: 60_000 });
    const disconnectPageToken = await page.evaluate(() => {
        globalThis.__localTavernDisconnectPageToken = crypto.randomUUID();
        return globalThis.__localTavernDisconnectPageToken;
    });
    const turnEventsPattern = /\/api\/chat\/turns\/[^/]+\/events(?:\?.*)?$/;
    let blockedTurnEventStreams = 0;
    const blockTurnEvents = async route => {
        blockedTurnEventStreams += 1;
        await route.fulfill({
            status: 503,
            contentType: 'application/json',
            body: JSON.stringify({
                error: { code: 'e2e_turn_events_unavailable', message: '受控断流' },
            }),
        });
    };
    await page.route(turnEventsPattern, blockTurnEvents);
    expectedHttpFailure = true;
    await page.locator('#user-input').fill('继续验证');
    await page.locator('#send-btn').click();
    await page.locator('#send-cancel-btn').waitFor({ state: 'visible' });
    await waitUntil(async () => {
        const session = await currentSession();
        return ['pending', 'streaming'].includes(session.message_history.at(-1)?.status);
    }, '服务端 turn 登记');
    await waitUntil(async () => (
        blockedTurnEventStreams >= 6
        && await page.evaluate(() => (
            document.querySelector('#toast-msg')?.textContent?.includes('连接已中断') === true
        ))
    ), 'turn 事件流重连耗尽');
    await page.locator('#send-cancel-btn').click();
    await page.waitForFunction(() => document.querySelector('#app-status')?.textContent.includes('生成已取消'));
    await page.locator('#send-cancel-btn').waitFor({ state: 'hidden' });
    await page.unroute(turnEventsPattern, blockTurnEvents);
    expectedHttpFailure = false;
    assert.equal(blockedTurnEventStreams, 6);
    assert.equal(
        await page.evaluate(() => globalThis.__localTavernDisconnectPageToken),
        disconnectPageToken,
        '取消解锁不得依赖刷新页面',
    );
    assert.equal(await page.locator('#send-btn').isEnabled(), true);
    assert.equal(await page.locator('#user-input').isEnabled(), true);
    const cancelled = await currentSession();
    assert.equal(cancelled.message_history.at(-1).status, 'cancelled');

    // 正常发送与建议直接发送。
    setControl(controlFile, 'normal');
    const beforeNormal = cancelled.message_history.length;
    await page.locator('#user-input').fill('继续验证');
    await page.locator('#send-btn').click();
    await page.waitForFunction(() => document.querySelector('#app-status')?.textContent === '生成完成');
    await page.locator('.suggestion-btn').first().waitFor();
    const narrativeUi = await page.evaluate(() => {
        const assistant = [...document.querySelectorAll('.msg.assistant')].at(-1);
        const story = assistant?.querySelector('.story-module');
        const characters = assistant?.querySelector('.characters-module');
        const affinity = characters?.querySelector('.affinity-summary');
        const meter = affinity?.querySelector('.affinity-meter');
        const states = characters?.querySelector('.character-state-details');
        const streamRect = document.querySelector('#chat-stream')?.getBoundingClientRect();
        const storyHeaderRect = story?.querySelector('.response-module-header')?.getBoundingClientRect();
        return {
            storyBeforeCharacters: Boolean(
                story && characters
                && (story.compareDocumentPosition(characters) & Node.DOCUMENT_POSITION_FOLLOWING)
            ),
            storyLabel: story?.getAttribute('aria-label') || '',
            charactersLabel: characters?.getAttribute('aria-label') || '',
            storyHeaderVisible: Boolean(
                streamRect && storyHeaderRect
                && storyHeaderRect.top >= streamRect.top - 1
                && storyHeaderRect.top < streamRect.bottom
            ),
            storyHeaderPosition: {
                top: Math.round(storyHeaderRect?.top || 0),
                streamTop: Math.round(streamRect?.top || 0),
                streamBottom: Math.round(streamRect?.bottom || 0),
            },
            affinityText: [...(affinity?.querySelectorAll('.affinity-copy > *') || [])]
                .map(element => element.textContent?.trim() || '')
                .filter(Boolean)
                .join(' '),
            meterRole: meter?.getAttribute('role') || '',
            meterNow: meter?.getAttribute('aria-valuenow') || '',
            stateDetailsOpen: states?.open ?? null,
            hasSceneUpdate: Boolean(story?.querySelector('.story-context-update')),
            sceneMetaOpen: document.querySelector('#scene-meta')?.open ?? null,
            sceneDetailsHidden: getComputedStyle(document.querySelector('.scene-details')).display === 'none',
            sceneSummaryText: document.querySelector('.scene-summary')?.textContent?.replace(/\s+/g, ' ').trim() || '',
            sideAffinityText: [...document.querySelectorAll('.roleplay-affinity')]
                .map(element => [...element.querySelectorAll('.affinity-copy > *')]
                    .map(item => item.textContent?.trim() || '')
                    .filter(Boolean)
                    .join(' ')),
        };
    });
    const { sceneSummaryText, storyHeaderVisible, storyHeaderPosition, ...narrativeContract } = narrativeUi;
    assert.doesNotMatch(sceneSummaryText, /测试用户|隔离测试酒馆/);
    assert.match(sceneSummaryText, /验证测试隔离/);
    assert.match(sceneSummaryText, /完成自动化验证/);
    assert.equal(storyHeaderVisible, true, JSON.stringify(storyHeaderPosition));
    assert.deepEqual(narrativeContract, {
        storyBeforeCharacters: true,
        storyLabel: '剧情推进',
        charactersLabel: '角色回应',
        affinityText: '好感度 50/100 熟悉 本轮 +10',
        meterRole: 'meter',
        meterNow: '50',
        stateDetailsOpen: false,
        hasSceneUpdate: true,
        sceneMetaOpen: false,
        sceneDetailsHidden: true,
        sideAffinityText: [
            '好感度 12/100 陌生',
            '好感度 88/100 深厚',
            '好感度 50/100 熟悉 本轮 +10',
        ],
    });

    await page.locator('.msg.assistant').last().locator('.msg-action-btn.details').click();
    await page.locator('#modal-title').filter({ hasText: '上下文与生成详情' }).waitFor();
    assert.equal(await page.locator('#modal-body').evaluate(element => element.scrollTop), 0);
    assert.match(
        await page.locator('.message-generation-details').textContent(),
        /上下文预算.*生成遥测.*不展示提示词正文、API Key/s,
    );
    if (screenshotPath) await page.screenshot({ path: screenshotSibling('generation-details'), animations: 'disabled' });
    await closeModalAfterAudit(page);

    if (screenshotPath) {
        for (const width of [375, 768, 1024, 1440]) {
            await page.setViewportSize({ width, height: width === 375 ? 812 : 900 });
            await page.locator('.story-module').last().locator('.response-module-header').scrollIntoViewIfNeeded();
            await page.screenshot({ path: screenshotSibling(`main-${width}`), animations: 'disabled' });
            if (width === 1440) await page.screenshot({ path: screenshotPath, animations: 'disabled' });
        }
        await page.setViewportSize({ width: 375, height: 812 });
    }

    await page.locator('.character-state-details').last().locator('summary').click();
    await page.locator('.character-state-details').last().locator('.character-state-list').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#scene-meta').evaluate(element => element.open), false);
    await page.locator('#scene-meta > summary').click();
    assert.equal(await page.locator('#scene-meta').evaluate(element => element.open), true);
    await page.locator('#meta-location').waitFor({ state: 'visible' });
    await page.locator('#scene-meta > summary').click();
    assert.equal(await page.locator('#scene-meta').evaluate(element => element.open), false);
    await page.locator('#meta-location').waitFor({ state: 'hidden' });

    const afterNormal = await currentSession();
    assert.equal(afterNormal.message_history.length, beforeNormal + 2);
    assert.equal(afterNormal.characters_state.test_character.affinity, 50);
    const persistedPresentation = afterNormal.message_history.at(-1)?.presentation;
    assert.equal(persistedPresentation?.schema_version, 1);
    assert.equal(persistedPresentation?.characters?.[0]?.affinity, 50);
    assert.equal(persistedPresentation?.characters?.[0]?.mood, '平静');
    assert.equal(Object.hasOwn(persistedPresentation || {}, 'raw'), false);

    // 刷新后必须从持久化/旧版兼容数据恢复相同信息层级，不能退回原始文本墙。
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(expectedCount => (
        document.readyState === 'complete'
        && document.querySelectorAll('.msg[data-message-id]').length === expectedCount
        && document.querySelector('#model-select')?.value === 'fake-model:latest'
    ), afterNormal.message_history.length);
    const refreshedAssistant = page.locator('.msg.assistant').last();
    await refreshedAssistant.locator('.story-module').waitFor();
    assert.equal(await refreshedAssistant.locator('.msg-role').textContent(), '剧情回合');
    assert.equal(await refreshedAssistant.locator('.characters-module').count(), 1);
    assert.match(
        await refreshedAssistant.locator('.affinity-copy').innerText(),
        /50\/100.*熟悉.*本轮 \+10/s,
    );
    assert.match(
        await refreshedAssistant.locator('.story-context-update').innerText(),
        /地点：隔离测试酒馆.*时间：午后.*天气：晴/s,
    );
    assert.equal(
        await refreshedAssistant.locator('.character-state-details').evaluate(element => element.open),
        false,
    );
    assert.match(
        await refreshedAssistant.locator('.character-state-list').textContent(),
        /心情\s*平静/s,
    );
    assert.equal(
        (await refreshedAssistant.locator('.content').innerText()).includes('📍 隔离测试酒馆'),
        false,
    );
    const rawDetails = refreshedAssistant.locator('.response-raw-details');
    assert.equal(await rawDetails.count(), 1);
    assert.equal(await rawDetails.evaluate(element => element.open), false);
    await rawDetails.locator('summary').click();
    assert.match(await rawDetails.locator('.response-raw-content').textContent(), /📍 隔离测试酒馆/);
    await rawDetails.locator('summary').click();
    assert.equal(await rawDetails.evaluate(element => element.open), false);

    // 阅读旧消息时，消息操作触发的权威重载必须保持可见消息锚点。
    const anchorBefore = await page.locator('#chat-stream').evaluate(stream => {
        stream.scrollTop = 0;
        const bounds = stream.getBoundingClientRect();
        const anchor = [...stream.querySelectorAll('.msg[data-message-id]')]
            .find(message => message.getBoundingClientRect().bottom > bounds.top + 1);
        return {
            id: anchor?.dataset.messageId || '',
            offset: anchor ? anchor.getBoundingClientRect().top - bounds.top : 0,
            distance: stream.scrollHeight - stream.clientHeight - stream.scrollTop,
        };
    });
    assert.notEqual(anchorBefore.id, '');
    assert.equal(anchorBefore.distance > 96, true, JSON.stringify(anchorBefore));
    const revisionBeforeAnchorAction = (await currentSession()).revision;
    await page.locator(`.msg[data-message-id="${anchorBefore.id}"] .msg-checkbox`).click();
    await waitUntil(async () => (await currentSession()).revision > revisionBeforeAnchorAction, '消息锚点操作提交');
    await delay(100);
    const anchorAfter = await page.locator('#chat-stream').evaluate(stream => {
        const bounds = stream.getBoundingClientRect();
        const anchor = [...stream.querySelectorAll('.msg[data-message-id]')]
            .find(message => message.getBoundingClientRect().bottom > bounds.top + 1);
        return {
            id: anchor?.dataset.messageId || '',
            offset: anchor ? anchor.getBoundingClientRect().top - bounds.top : 0,
        };
    });
    assert.equal(anchorAfter.id, anchorBefore.id);
    assert.equal(Math.abs(anchorAfter.offset - anchorBefore.offset) <= 3, true, JSON.stringify({ anchorBefore, anchorAfter }));

    const beforeAssistantEditCancel = await currentSession();
    await refreshedAssistant.locator('.msg-action-btn.edit').click();
    const assistantEditor = refreshedAssistant.locator('.message-edit-input');
    assert.match(await assistantEditor.inputValue(), /📍 隔离测试酒馆/);
    assert.doesNotMatch(await assistantEditor.inputValue(), /剧情推进/);
    await assistantEditor.press('Escape');
    await refreshedAssistant.locator('.story-module').waitFor();
    const afterAssistantEditCancel = await currentSession();
    assert.equal(afterAssistantEditCancel.revision, beforeAssistantEditCancel.revision);
    assert.equal(
        afterAssistantEditCancel.message_history.at(-1)?.content,
        beforeAssistantEditCancel.message_history.at(-1)?.content,
    );
    assert.deepEqual(
        afterAssistantEditCancel.message_history.at(-1)?.presentation,
        beforeAssistantEditCancel.message_history.at(-1)?.presentation,
    );

    // 真正重启服务进程后，持久化展示快照仍必须恢复为相同模块。
    await page.goto('about:blank');
    await stopChild(serverProcess);
    serverProcess = null;
    serverProcess = await startTavernServer();
    await page.goto(appUrl, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(expectedCount => (
        document.readyState === 'complete'
        && document.querySelectorAll('.msg[data-message-id]').length === expectedCount
        && document.querySelector('#model-select')?.value === 'fake-model:latest'
    ), afterNormal.message_history.length);
    const restartedAssistant = page.locator('.msg.assistant').last();
    await restartedAssistant.locator('.story-module').waitFor();
    assert.equal(await restartedAssistant.locator('.characters-module').count(), 1);
    assert.match(
        await restartedAssistant.locator('.affinity-copy').innerText(),
        /50\/100.*熟悉.*本轮 \+10/s,
    );
    assert.match(
        await restartedAssistant.locator('.story-context-update').innerText(),
        /地点：隔离测试酒馆.*时间：午后.*天气：晴/s,
    );
    assert.match(
        await restartedAssistant.locator('.character-state-list').textContent(),
        /心情\s*平静/s,
    );
    await page.waitForFunction(() => {
        const send = document.querySelector('#send-btn');
        const cancel = document.querySelector('#send-cancel-btn');
        return Boolean(send && !send.disabled && cancel && getComputedStyle(cancel).display === 'none');
    });
    await page.locator('.suggestion-btn').first().waitFor();

    // 最新 assistant 重生成保留同一消息容器，并提供可切换备选回复。
    const beforeRegeneration = await currentSession();
    const replyMessageId = beforeRegeneration.message_history.at(-1).id;
    await page.locator(`.msg[data-message-id="${replyMessageId}"] .msg-action-btn.regenerate`).click();
    const regeneratedSession = await waitUntil(async () => {
        const session = await currentSession();
        const reply = session.message_history.find(message => message.id === replyMessageId);
        return reply?.status === 'completed' && reply.reply_alternatives?.length === 1
            && session.revision > beforeRegeneration.revision
            ? session
            : null;
    }, '重生成备选回复终态');
    const replyControl = page.locator(`.msg[data-message-id="${replyMessageId}"] .reply-alternative-control`);
    await replyControl.waitFor();
    assert.match(await replyControl.locator('.reply-alternative-label').textContent(), /^回复 [12]\/2$/);
    if (screenshotPath) {
        await replyControl.scrollIntoViewIfNeeded();
        await page.screenshot({ path: screenshotSibling('reply-alternatives'), animations: 'disabled' });
    }
    const variantBeforeSwitch = regeneratedSession.message_history.find(message => message.id === replyMessageId).reply_variant_id;
    const alternativeId = await replyControl.locator('option').evaluateAll((options, activeId) => (
        options.find(option => option.value !== activeId)?.value || ''
    ), variantBeforeSwitch);
    assert.notEqual(alternativeId, '');
    await replyControl.locator('select').selectOption(alternativeId);
    const switchedSession = await waitUntil(async () => {
        const session = await currentSession();
        const reply = session.message_history.find(message => message.id === replyMessageId);
        return reply?.reply_variant_id === alternativeId ? session : null;
    }, '切换备选回复');
    assert.equal(switchedSession.message_history.find(message => message.id === replyMessageId).id, replyMessageId);
    assert.equal(switchedSession.message_history.length, beforeRegeneration.message_history.length);

    const suggestionText = await page.locator('.suggestion-btn').first().textContent();
    const beforeSuggestion = (await currentSession()).message_history.length;
    const readingPosition = await page.locator('#chat-stream').evaluate(stream => {
        stream.scrollTop = 0;
        return stream.scrollHeight - stream.clientHeight - stream.scrollTop;
    });
    assert.equal(readingPosition > 96, true);
    await page.locator('.suggestion-btn').first().click();
    await page.waitForFunction(expected => document.querySelector('#user-input')?.value === expected, suggestionText);
    await delay(150);
    assert.equal((await currentSession()).message_history.length, beforeSuggestion, '点击建议不得立即发送');
    await page.locator('#send-btn').click();
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

    // 旧回复仍可切换内容，但后续剧情和当前状态不得回滚；旧回复也不得再次重生成。
    const beforeHistoricalSwitch = await currentSession();
    const historicalReply = beforeHistoricalSwitch.message_history.find(message => message.id === replyMessageId);
    const historicalAlternativeId = historicalReply.reply_alternatives[0].id;
    const historicalControl = page.locator(`.msg[data-message-id="${replyMessageId}"] .reply-alternative-control`);
    await historicalControl.locator('select').selectOption(historicalAlternativeId);
    const afterHistoricalSwitch = await waitUntil(async () => {
        const session = await currentSession();
        const reply = session.message_history.find(message => message.id === replyMessageId);
        return reply?.reply_variant_id === historicalAlternativeId ? session : null;
    }, '带后续历史的备选回复切换');
    assert.deepEqual(afterHistoricalSwitch.scene_meta, beforeHistoricalSwitch.scene_meta);
    assert.deepEqual(afterHistoricalSwitch.characters_state, beforeHistoricalSwitch.characters_state);
    assert.deepEqual(
        afterHistoricalSwitch.message_history.map(message => message.id),
        beforeHistoricalSwitch.message_history.map(message => message.id),
    );
    await page.waitForFunction(() => document.querySelector('#app-status')?.textContent
        === '已切换回复；后续剧情与当前角色状态保持不变');
    const revisionBeforeRejectedRegeneration = afterHistoricalSwitch.revision;
    expectedHttpFailure = true;
    await page.locator(`.msg[data-message-id="${replyMessageId}"] .msg-action-btn.regenerate`).click();
    await page.waitForFunction(() => document.querySelector('#app-status')?.textContent
        === '只能为最新回复生成备选，不会删除或重排任何后续内容');
    expectedHttpFailure = false;
    assert.equal((await currentSession()).revision, revisionBeforeRejectedRegeneration);

    const preservedReading = await page.locator('#chat-stream').evaluate(stream => ({
        distance: stream.scrollHeight - stream.clientHeight - stream.scrollTop,
        latestVisible: !document.querySelector('#chat-latest-btn')?.classList.contains('hidden'),
        latestText: document.querySelector('#chat-latest-btn')?.textContent || '',
    }));
    assert.equal(preservedReading.distance > 96, true, JSON.stringify(preservedReading));
    assert.equal(preservedReading.latestVisible, true, JSON.stringify(preservedReading));
    assert.match(preservedReading.latestText, /回到最新/);
    await page.locator('#chat-latest-btn').click();
    await page.waitForFunction(() => {
        const stream = document.querySelector('#chat-stream');
        return stream && stream.scrollHeight - stream.clientHeight - stream.scrollTop <= 2
            && document.querySelector('#chat-latest-btn')?.classList.contains('hidden');
    });
    assert.equal(
        await page.locator('.msg.assistant').last().locator('.story-context-update').count(),
        0,
        '地点、时间与天气未变化时不得重复占用对话正文',
    );
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
    await clickTopbarTool(page, '#history-btn');
    await page.locator('.history-preview').first().click();
    await page.locator('.snapshot-preview .snapshot-filename').filter({ hasText: snapshot.filename }).waitFor();
    await page.locator('#modal-close').click();
    await clickTopbarTool(page, '#history-btn');
    await page.locator(`.history-restore[data-fn="${snapshot.filename}"]`).click();
    await page.locator('#modal-backdrop').waitFor({ state: 'hidden' });

    // 存档与项目切换。
    await page.locator('#user-input').fill('默认存档草稿');
    await switchSave(page, '切换存档');
    assert.equal((await currentSession('默认项目', '切换存档')).session_id, '切换存档');
    assert.match(await page.locator('#current-session-label').textContent(), /默认项目 \/ 切换存档/);
    assert.equal(await page.locator('#user-input').inputValue(), '');
    await page.locator('#user-input').fill('切换存档草稿');
    const secondSaveAssistant = page.locator(
        '.msg[data-message-id="44444444-4444-4444-8444-444444444444"]',
    );
    await secondSaveAssistant.locator('.story-module').waitFor();
    assert.equal(await page.locator('.msg[data-message-id]').count(), 2);
    assert.match(await secondSaveAssistant.locator('.story-module').innerText(), /切换存档主线/);
    assert.match(await secondSaveAssistant.locator('.affinity-copy').innerText(), /27\/100.*初识/s);
    assert.equal(await page.locator('.suggestion-btn').first().textContent(), '切换存档建议');

    await switchSave(page, '默认存档');
    assert.match(await page.locator('#current-session-label').textContent(), /默认项目 \/ 默认存档/);
    assert.equal(await page.locator('#user-input').inputValue(), '默认存档草稿');
    const defaultLegacyAssistant = page.locator(
        '.msg[data-message-id="22222222-2222-4222-8222-222222222222"]',
    );
    await defaultLegacyAssistant.locator('.story-module').waitFor();
    assert.match(await defaultLegacyAssistant.locator('.story-module').innerText(), /守护城门/);

    await switchSave(page, '切换存档');
    assert.equal(await page.locator('#user-input').inputValue(), '切换存档草稿');
    await page.locator('#user-input').fill('');
    await switchSave(page, '默认存档');
    assert.equal(await page.locator('#user-input').inputValue(), '默认存档草稿');

    await page.locator('#user-input').fill('默认项目草稿');
    await switchProject(page, '切换项目');
    assert.equal(await page.locator('#project-name').textContent(), '切换项目');
    assert.match(await page.locator('#current-session-label').textContent(), /切换项目 \/ 默认存档/);
    assert.equal(await page.locator('#user-input').inputValue(), '');
    await page.locator('#user-input').fill('切换项目草稿');
    const secondProjectAssistant = page.locator(
        '.msg[data-message-id="66666666-6666-4666-8666-666666666666"]',
    );
    await secondProjectAssistant.locator('.story-module').waitFor();
    assert.equal(await page.locator('.msg[data-message-id]').count(), 2);
    assert.match(await secondProjectAssistant.locator('.story-module').innerText(), /切换项目主线/);
    assert.match(await secondProjectAssistant.locator('.affinity-copy').innerText(), /73\/100.*亲近/s);
    assert.equal(await page.locator('.suggestion-btn').first().textContent(), '切换项目建议');

    await switchProject(page, '默认项目');
    assert.equal(await page.locator('#user-input').inputValue(), '默认项目草稿');
    await page.locator('#user-input').fill('');
    await defaultLegacyAssistant.locator('.story-module').waitFor();
    assert.match(await defaultLegacyAssistant.locator('.story-module').innerText(), /守护城门/);

    // 恶意导入不得生成可执行 DOM，错误只以文本呈现。
    await page.evaluate(() => { window.__tavernE2eXss = 0; });
    await openSaveManager(page);
    expectedHttpFailure = true;
    await page.locator('#save-manager-import-file').setInputFiles({
        name: 'malicious.json',
        mimeType: 'application/json',
        buffer: Buffer.from('<img src=x onerror="window.__tavernE2eXss=1">', 'utf8'),
    });
    await page.waitForFunction(() => document.querySelector('.save-manager-status')?.textContent.includes('操作失败'));
    expectedHttpFailure = false;
    const importSafety = await page.evaluate(() => ({
        status: document.querySelector('.save-manager-status')?.textContent || '',
        executableNodes: document.querySelectorAll('#modal-body script,#modal-body img').length,
        xss: window.__tavernE2eXss,
    }));
    assert.match(importSafety.status, /^操作失败：/);
    assert.equal(importSafety.executableNodes, 0);
    assert.equal(importSafety.xss, 0);
    await page.locator('#modal-close').click();

    // 初始化失败必须提供可恢复页面：可打开模型来源，也可在依赖恢复后重试。
    const failurePage = await context.newPage();
    let failProjectBootstrap = true;
    await failurePage.route('**/api/projects', async route => {
        if (failProjectBootstrap) {
            await route.fulfill({
                status: 503,
                contentType: 'application/json',
                body: JSON.stringify({ detail: 'E2E 初始化故障' }),
            });
        } else {
            await route.continue();
        }
    });
    await failurePage.goto(appUrl, { waitUntil: 'domcontentloaded' });
    await failurePage.locator('#init-error').waitFor({ state: 'visible' });
    assert.equal(await failurePage.locator('#init-error').getAttribute('role'), 'alert');
    assert.equal(await failurePage.locator('#init-error').getAttribute('aria-live'), 'assertive');
    assert.match(await failurePage.locator('#init-error-message').textContent(), /初始化失败/);
    await failurePage.locator('#init-provider-btn').click();
    await failurePage.locator('.provider-settings').waitFor({ state: 'visible' });
    await failurePage.locator('#modal-close').click();
    await failurePage.locator('#modal-backdrop').waitFor({ state: 'hidden' });
    failProjectBootstrap = false;
    await failurePage.locator('#init-retry-btn').click();
    await failurePage.locator('#init-error').waitFor({ state: 'hidden' });
    await failurePage.waitForFunction(() => (
        document.querySelector('#project-name')?.textContent === '默认项目'
        && document.querySelector('#current-session-label')?.textContent.includes('默认存档')
    ));
    await failurePage.close();

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
    const performanceDocument = await measureBrowserPerformance(browser, browserVersion);
    if (performanceOutputPath) writePerformanceDocument(performanceOutputPath, performanceDocument);

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
            'provider_settings_anthropic', 'narrative_information_hierarchy',
            'ime_guard', 'session_draft_isolation', 'scroll_anchor_and_latest',
            'mobile_inspector_focus', 'initialization_recovery', 'responsive_more_menus',
            'first_use_privacy_onboarding', 'backup_dry_run_drill_confirmation',
            'diagnostics_support_bundle', 'memory_note_crud', 'modal_toast_guard',
            'message_generation_details',
            'reply_alternative_switch', 'historical_reply_state_preservation',
            'historical_regeneration_guard',
            'disconnect_exhaustion_cancel_unlock',
        ],
        axe_serious_critical: skipAxe ? null : viewportAudit.failures.length,
        audited_views: auditedViews,
        viewport_layouts: viewportAudit.layouts,
        layout,
        browser_performance: performanceDocument,
        performance_output: performanceOutputPath,
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
