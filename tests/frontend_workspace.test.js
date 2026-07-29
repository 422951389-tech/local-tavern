const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..');
const WEB = path.join(ROOT, 'web');

function provider(overrides = {}) {
    return {
        provider_id: 'cloud-main',
        kind: 'openai_compatible',
        name: 'Cloud Main',
        base_url: 'https://api.example.com/v1',
        models: ['model-a', 'model-b'],
        preset: 'custom',
        credential_required: true,
        has_credential: false,
        capabilities: {
            streaming_chat: true,
            summarize: true,
            request_thinking: false,
            stream_thinking: true,
        },
        context_limit: 32768,
        ...overrides,
    };
}

test('沉浸式工作台保留三栏、抽屉、响应式与无障碍契约', () => {
    const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
    const css = fs.readFileSync(path.join(WEB, 'styles', 'workspace.css'), 'utf8');
    assert.match(html, /id="workspace-shell" class="workspace-shell"/);
    assert.match(html, /id="left-nav"/);
    assert.match(html, /class="workspace-center"/);
    assert.match(html, /id="character-panel"/);
    assert.match(html, /id="provider-settings-btn"/);
    assert.match(html, /id="inspector-toggle"[\s\S]*aria-expanded="true"/);
    assert.match(html, /id="inspector-close"/);
    assert.match(html, /id="inspector-backdrop"[\s\S]*aria-hidden="true"/);
    assert.match(html, /id="nav-more-btn"[\s\S]*>更多</);
    assert.match(html, /id="topbar-more-btn"[\s\S]*>更多</);
    assert.match(html, /id="current-session-label"[\s\S]*aria-live="polite"/);
    assert.match(html, /id="chat-latest-btn"[\s\S]*aria-live="polite"/);
    assert.match(html, /id="init-error"[\s\S]*role="alert"[\s\S]*aria-live="assertive"/);
    assert.match(html, /data-icon="provider"/);
    assert.match(html, /<details id="scene-meta" class="scene-meta"/);
    assert.match(html, /<summary class="scene-summary">/);
    assert.doesNotMatch(html, /<details id="scene-meta"[^>]*\sopen(?:\s|>)/);
    assert.match(html, /static\/styles\/workspace\.css/);
    assert.doesNotMatch(html, /[\u{1F300}-\u{1FAFF}]/u);

    assert.match(css, /grid-template-columns:\s*248px minmax\(720px, 1fr\) minmax\(320px, 380px\)/);
    assert.match(css, /\.workspace-shell\.inspector-collapsed\s*\{[\s\S]*grid-template-columns:\s*248px minmax\(720px, 1fr\) 0/);
    assert.match(css, /\.chat-stream > \.msg\s*\{[\s\S]*max-width:\s*800px/);
    assert.match(css, /@media \(min-width: 900px\) and \(max-width: 1365px\)/);
    assert.match(css, /@media \(max-width: 899px\)/);
    assert.match(css, /@media \(max-width: 600px\)/);
    assert.match(css, /@media \(hover: none\), \(pointer: coarse\)[\s\S]*\.msg-action-btn\s*\{[\s\S]*min-width:\s*44px[\s\S]*min-height:\s*44px/);
    assert.match(css, /min-height:\s*44px/);
    assert.match(css, /:focus-visible/);
    assert.match(css, /prefers-reduced-motion:\s*reduce/);
    assert.match(css, /\.provider-entry\[data-status="missing-provider"\]/);
    assert.match(css, /#model-select\[data-status="missing-model"\]/);
    assert.match(css, /\.provider-recovery-note\s*\{/);
    assert.match(css, /\.story-module\s*\{[\s\S]*border-left:\s*3px solid var\(--workspace-story\)/);
    assert.match(css, /\.characters-module\s*\{[\s\S]*border-left:\s*3px solid var\(--workspace-character\)/);
    assert.match(css, /\.affinity-meter\s*\{[\s\S]*height:\s*6px/);
    assert.match(css, /\.character-state-details\s*\{/);
    assert.match(css, /\.scene-meta:not\(\[open\]\) > \.scene-details\s*\{[\s\S]*display:\s*none/);
    assert.doesNotMatch(css, /backdrop-filter|font-face|https?:\/\//i);
});

test('输入、草稿、滚动与消息编辑采用显式可靠性契约', () => {
    const app = fs.readFileSync(path.join(WEB, 'app.mjs'), 'utf8');
    const editor = fs.readFileSync(path.join(WEB, 'message-editor.mjs'), 'utf8');
    const suggestions = app.match(/function renderSuggestions\(suggestions\)\s*\{[\s\S]*?\n\}/)?.[0] || '';
    assert.match(app, /COMPOSER_DRAFT_PREFIX/);
    assert.match(app, /function sessionIdentity\(ref\)/);
    assert.match(app, /restoreComposerDraft\(ref\)/);
    assert.match(app, /function captureChatScroll\(stream\)/);
    assert.match(app, /function restoreChatScroll\(stream, snapshot\)/);
    assert.match(app, /if \(e\.isComposing \|\| e\.keyCode === 229\) return/);
    assert.match(suggestions, /persistComposerDraft\(committedSessionRef, s\)/);
    assert.doesNotMatch(suggestions, /sendMessage\(/);
    assert.match(editor, /message-edit-save/);
    assert.match(editor, /message-edit-cancel/);
    assert.doesNotMatch(editor, /addEventListener\('blur'/);
});

test('备份、诊断、长期记忆、生成详情、首次引导与备选回复完成前端接线', () => {
    const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(WEB, 'app.mjs'), 'utf8');
    const workspaceCss = fs.readFileSync(path.join(WEB, 'styles', 'workspace.css'), 'utf8');
    const backup = fs.readFileSync(path.join(WEB, 'backup-controller.mjs'), 'utf8');
    const diagnostics = fs.readFileSync(path.join(WEB, 'diagnostics-controller.mjs'), 'utf8');
    const memory = fs.readFileSync(path.join(WEB, 'memory-controller.mjs'), 'utf8');
    const onboarding = fs.readFileSync(path.join(WEB, 'onboarding-controller.mjs'), 'utf8');
    const memoryCss = fs.readFileSync(path.join(WEB, 'styles', 'memory.css'), 'utf8');
    const onboardingCss = fs.readFileSync(path.join(WEB, 'styles', 'onboarding.css'), 'utf8');
    const modal = fs.readFileSync(path.join(WEB, 'modal.mjs'), 'utf8');
    for (const id of ['memory-notes-btn', 'backup-center-btn', 'diagnostics-center-btn']) {
        assert.match(html, new RegExp(`id="${id}"`));
    }
    for (const stylesheet of ['backup.css', 'diagnostics.css', 'memory.css', 'onboarding.css']) {
        assert.match(html, new RegExp(`static/styles/${stylesheet.replace('.', '\\.')}`));
    }
    assert.match(app, /createBackupService\(apiClient\)/);
    assert.match(app, /createDiagnosticsService\(apiClient\)/);
    assert.match(app, /createMemoryNotesService\(apiClient\)/);
    assert.match(app, /createReplyAlternativeService\(apiClient\)/);
    assert.match(app, /createBackupController\(\{/);
    assert.match(app, /createDiagnosticsController\(\{/);
    assert.match(app, /createMemoryController\(\{/);
    assert.match(app, /createOnboardingController\(\{/);
    assert.match(app, /const showModal = config => \{[\s\S]*dismissToast\(\)[\s\S]*modalController\.show\(config\)/);
    assert.match(app, /if \(modalIsOpen\(\)\) \{[\s\S]*dismissToast\(\);[\s\S]*return;/);
    assert.match(backup, /function showRestoreConfirmation\(backup, dryRun\)/);
    assert.match(backup, /service\.dryRun\(backup\.backupId\)[\s\S]*showRestoreConfirmation/);
    assert.match(diagnostics, /诊断内容经过字段白名单重建/);
    assert.match(memory, /版本冲突，已刷新当前存档；你的草稿仍保留在表单中/);
    assert.match(app, /local-tavern\.onboarding\.v1/);
    assert.match(onboarding, /关闭此窗口不会记为完成/);
    assert.match(app, /stateApplied[\s\S]*后续剧情与当前角色状态保持不变/);
    assert.match(app, /regeneration_would_rewrite_history[\s\S]*不会删除或重排任何后续内容/);
    assert.match(workspaceCss, /\.reply-alternative-control\s*\{/);
    assert.match(workspaceCss, /\.product-privacy-note\s*\{/);
    assert.match(memoryCss, /\.memory-notes-center\s*\{/);
    assert.match(onboardingCss, /\.onboarding-options\s*\{/);
    assert.match(modal, /body\.scrollTop = 0/);
});

test('浏览器 E2E 输出 performance_budget 可直接校验的三类性能样本', () => {
    const browserE2e = fs.readFileSync(path.join(ROOT, 'tests', 'browser_e2e.mjs'), 'utf8');
    assert.match(browserE2e, /TAVERN_E2E_PERFORMANCE_OUTPUT/);
    assert.match(browserE2e, /warmup_runs:\s*1/);
    assert.match(browserE2e, /browser_first_contentful_paint_ms/);
    assert.match(browserE2e, /browser_first_screen_ready_ms/);
    assert.match(browserE2e, /browser_render_1000_messages_ms/);
    assert.match(browserE2e, /message_count:\s*1000/);
    assert.match(browserE2e, /for \(let index = 0; index < 4; index \+= 1\)/);
    assert.match(browserE2e, /createMessageElement, mountMessageHistory/);
    assert.match(browserE2e, /\[375, 768, 899, 1024, 1440\]/);
    assert.match(browserE2e, /screenshotSibling\(`main-\$\{width\}`\)/);
    assert.match(browserE2e, /environment:\s*\{[\s\S]*os:[\s\S]*python:[\s\S]*node:[\s\S]*browser:[\s\S]*source_commit:/);
});

test('Provider public_config、预设、配置和 context_limit 使用严格边界', async () => {
    const module = await import(pathToFileURL(path.join(WEB, 'provider-settings.mjs')).href);
    const ollama = provider({
        provider_id: 'ollama',
        kind: 'ollama',
        name: 'Ollama',
        base_url: 'http://127.0.0.1:11434',
        models: [],
        preset: 'custom',
        credential_required: false,
        has_credential: true,
    });
    const listed = module.normalizeProvidersResponse({ providers: [ollama, provider()] });
    assert.deepEqual(listed.providers.map(item => item.id), ['ollama', 'cloud-main']);
    assert.equal(listed.providers[1].contextLimit, 32768);
    assert.equal('apiKey' in listed.providers[1], false);

    assert.throws(
        () => module.normalizeProvider({ ...provider(), api_key: 'must-not-leak' }),
        /未知字段/,
    );
    assert.throws(
        () => module.normalizeProvider({ ...provider(), context_limit: 4095 }),
        /上下文窗口/,
    );
    assert.throws(
        () => module.normalizeProviderConfig({
            preset: 'custom',
            kind: 'openai_compatible',
            base_url: 'https://user:secret@example.com/v1',
            models: [],
            context_limit: 32768,
        }),
        /内嵌凭据/,
    );
    assert.throws(
        () => module.normalizeProviderConfig({
            preset: 'custom',
            kind: 'anthropic',
            base_url: 'http://api.example.com',
            models: ['claude-test'],
            context_limit: 32768,
        }),
        /HTTPS/,
    );
    assert.throws(
        () => module.normalizeProvider(provider({ base_url: 'http://api.example.com/v1' })),
        /HTTPS/,
    );
    assert.throws(
        () => module.normalizeProviderConfig({
            preset: 'custom',
            kind: 'openai_compatible',
            base_url: 'https://api.example.com/v1?api_key=secret',
            models: [],
            context_limit: 32768,
        }),
        /查询参数或片段/,
    );
    assert.deepEqual(module.normalizeProviderConfig({
        preset: 'anthropic',
        name: 'Anthropic',
        models: ['claude-test'],
        context_limit: 200000,
    }), {
        preset: 'anthropic',
        context_limit: 200000,
        name: 'Anthropic',
        models: ['claude-test'],
    });

    const configured = module.normalizeProvider(provider({ has_credential: true }));
    const presets = module.normalizePresetsResponse({ presets: [
        { id: 'openai', name: 'OpenAI', kind: 'openai_compatible', base_url: 'https://api.openai.com/v1', context_limit: 32768 },
        { id: 'deepseek', name: 'DeepSeek', kind: 'openai_compatible', base_url: 'https://api.deepseek.com/v1', context_limit: 32768 },
        { id: 'custom', name: '自定义', kind: 'openai_compatible', base_url: null, context_limit: 32768 },
    ] });
    assert.equal(module.requiresCredentialRebind(configured, module.normalizeProviderConfig({
        preset: 'custom',
        kind: 'openai_compatible',
        base_url: 'https://API.EXAMPLE.com/v1/',
        models: [],
        context_limit: 32768,
    }), presets), false);
    assert.equal(module.requiresCredentialRebind(configured, module.normalizeProviderConfig({
        preset: 'deepseek',
        models: [],
        context_limit: 32768,
    }), presets), true);
});

test('Provider 运行状态明确区分来源缺失、模型失效和可用状态', async () => {
    const module = await import(pathToFileURL(path.join(WEB, 'provider-settings.mjs')).href);
    const initial = module.normalizeProvidersResponse({ providers: [provider()] });

    assert.deepEqual(
        module.describeProviderSelection(initial, 'deleted-cloud', 'model-a'),
        {
            status: 'missing_provider',
            providerId: 'deleted-cloud',
            currentModel: 'model-a',
            models: [],
            selectedModel: '',
        },
    );
    assert.equal(
        module.describeProviderSelection(initial, 'cloud-main', 'retired-model').status,
        'missing_model',
    );
    assert.equal(
        module.describeProviderSelection(initial, 'cloud-main', 'model-a').status,
        'ready',
    );
    assert.equal(
        module.describeProviderSelection(initial, 'cloud-main', '', []).status,
        'no_models',
    );

    const changed = module.normalizeProvidersResponse({ providers: [provider({ models: ['model-c'] })] });
    assert.equal(module.activeProviderEntryChanged(initial, changed, 'cloud-main'), true);
    assert.equal(module.activeProviderEntryChanged(initial, initial, 'cloud-main'), false);
    assert.equal(module.activeProviderEntryChanged(initial, { providers: [] }, 'cloud-main'), true);
});

test('Provider service 集中使用锁定端点和 JSON payload', async () => {
    const module = await import(pathToFileURL(path.join(WEB, 'provider-settings.mjs')).href);
    const calls = [];
    const cloud = provider();
    const client = {
        async get(url) {
            calls.push(['GET', url]);
            if (url === '/api/providers/presets') {
                return { presets: [
                    { id: 'openai', name: 'OpenAI', kind: 'openai_compatible', base_url: 'https://api.openai.com/v1', context_limit: 32768 },
                    { id: 'custom', name: '自定义', kind: 'openai_compatible', base_url: null, context_limit: 32768 },
                ] };
            }
            if (url === '/api/providers') return { providers: [cloud] };
            if (url === '/api/providers/cloud-main') return { provider: cloud };
            if (url === '/api/models?provider=cloud-main') return { provider: 'cloud-main', models: cloud.models };
            throw new Error(`unexpected GET ${url}`);
        },
        async put(url, body) {
            calls.push(['PUT', url, body]);
            if (url.endsWith('/credential')) {
                return { credential: { provider_id: 'cloud-main', configured: true } };
            }
            return { provider: { ...cloud, context_limit: body.context_limit } };
        },
        async post(url, body) {
            calls.push(['POST', url, body]);
            return { test: {
                ok: true,
                provider_id: 'cloud-main',
                models: cloud.models,
                model_count: cloud.models.length,
                capabilities: cloud.capabilities,
            } };
        },
        async delete(url) {
            calls.push(['DELETE', url]);
            if (url.endsWith('/credential')) {
                return { credential: { provider_id: 'cloud-main', configured: false } };
            }
            return { deleted: true, provider_id: 'cloud-main' };
        },
    };
    const service = module.createProviderService(client);
    await service.presets();
    await service.list();
    await service.get('cloud-main');
    await service.models('cloud-main');
    await service.save('cloud-main', {
        preset: 'custom',
        name: 'Cloud Main',
        kind: 'openai_compatible',
        base_url: 'https://api.example.com/v1',
        models: cloud.models,
        context_limit: 65536,
    });
    await service.setCredential('cloud-main', 'secret-value');
    await service.test('cloud-main');
    await service.deleteCredential('cloud-main');
    await service.remove('cloud-main');

    assert.deepEqual(calls.map(call => `${call[0]} ${call[1]}`), [
        'GET /api/providers/presets',
        'GET /api/providers',
        'GET /api/providers/cloud-main',
        'GET /api/models?provider=cloud-main',
        'PUT /api/providers/cloud-main',
        'PUT /api/providers/cloud-main/credential',
        'POST /api/providers/cloud-main/test',
        'DELETE /api/providers/cloud-main/credential',
        'DELETE /api/providers/cloud-main',
    ]);
    assert.deepEqual(calls[4][2], {
        preset: 'custom',
        context_limit: 65536,
        name: 'Cloud Main',
        models: ['model-a', 'model-b'],
        kind: 'openai_compatible',
        base_url: 'https://api.example.com/v1',
    });
    assert.deepEqual(calls[5][2], { api_key: 'secret-value' });
});

test('消息思考过程内嵌，应用按 session provider 加载并原子切换', async () => {
    const app = fs.readFileSync(path.join(WEB, 'app.mjs'), 'utf8');
    const conversation = fs.readFileSync(path.join(WEB, 'conversation-view.mjs'), 'utf8');
    const providerSettings = fs.readFileSync(path.join(WEB, 'provider-settings.mjs'), 'utf8');
    const icons = fs.readFileSync(path.join(WEB, 'icons.mjs'), 'utf8');
    for (const source of [app, conversation, providerSettings, icons]) {
        assert.doesNotMatch(source, /\.innerHTML|\.outerHTML|insertAdjacentHTML/);
    }
    assert.match(conversation, /createElement\('details'\)/);
    assert.match(conversation, /className = 'message-thinking'/);
    assert.match(app, /updateMessageThinking\(snapshot\.targetEl/);
    assert.match(app, /provider:\s*\(state\.session && state\.session\.current_provider\)/);
    assert.match(app, /provider:\s*state\.activeProvider,[\s\S]*model,/);
    assert.match(app, /createProviderSettingsView\(\{/);
    assert.match(app, /await Promise\.all\(\[loadProviderCatalog\(\), loadProjects\(\), loadSettings\(\)\]\)/);
    assert.match(app, /function handleProviderSnapshotChanged\(snapshot\)/);
    assert.match(app, /state\.modelsProvider = null;[\s\S]*syncModelsFromSession\(state\.session, \{ force: true \}\)/);
    assert.match(app, /forceStatus: 'missing_provider'/);
    assert.match(app, /当前模型已不可用/);
    assert.match(
        app,
        /captureResponsePresentationBaseline\(\s*state\.session,\s*parsedEvent,\s*state\.activeTurn && state\.activeTurn\.turnId,\s*\)/,
    );
    assert.match(app, /function sceneFactText\(value, fallback = ''\)/);
    assert.match(app, /if \(storyModule\) contentEl\.appendChild\(storyModule\);[\s\S]*if \(characterModule\) contentEl\.appendChild\(characterModule\);/);
    assert.match(app, /requestAnimationFrame\(\(\) => \{[\s\S]*messageElement\.scrollIntoView\(\{ block: 'start', behavior: 'auto' \}\)/);
    assert.match(app, /sceneChanges\.map\(item => `\$\{item\.label\}：\$\{item\.value\}`\)/);
    assert.match(conversation, /role', 'meter'/);
    assert.match(conversation, /关系阶段/);
    assert.doesNotMatch(app, /current_provider\s*\|\|\s*['"]ollama['"]/);
    assert.match(providerSettings, /notifyChanged\(provider\.id, 'models'\)/);
    assert.match(providerSettings, /云端服务地址必须使用 HTTPS/);
    assert.doesNotMatch(app, /Promise\.all\(\[loadModels\(\), loadProjects\(\)/);
    assert.doesNotMatch(app, /[\u{1F300}-\u{1FAFF}]/u);
});
