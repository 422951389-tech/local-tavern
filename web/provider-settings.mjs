import { createIcon } from './icons.mjs';

const PROVIDER_ENDPOINTS = Object.freeze({
    presets: '/api/providers/presets',
    providers: '/api/providers',
    models: '/api/models',
});

const PROVIDER_FIELDS = new Set([
    'provider_id', 'kind', 'name', 'base_url', 'models', 'preset',
    'credential_required', 'has_credential', 'capabilities', 'context_limit',
]);
const PRESET_FIELDS = new Set(['id', 'name', 'kind', 'base_url', 'context_limit']);
const PROVIDER_LIST_FIELDS = new Set(['providers']);
const PRESET_LIST_FIELDS = new Set(['presets']);
const PROVIDER_RESPONSE_FIELDS = new Set(['provider']);
const CONFIG_FIELDS = new Set(['preset', 'name', 'kind', 'base_url', 'models', 'context_limit']);
const PRESET_IDS = new Set(['openai', 'deepseek', 'siliconflow', 'anthropic', 'custom']);
const CLOUD_KINDS = new Set(['openai_compatible', 'anthropic']);

function isRecord(value) {
    return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function exactFields(value, allowed, label) {
    if (!isRecord(value)) throw new TypeError(`${label}必须是对象`);
    const extra = Object.keys(value).filter(key => !allowed.has(key));
    if (extra.length) throw new TypeError(`${label}包含未知字段：${extra.join(', ')}`);
}

function cleanText(value, label, { required = false, max = 2048 } = {}) {
    if (value === undefined || value === null) {
        if (required) throw new TypeError(`${label}不能为空`);
        return '';
    }
    if (typeof value !== 'string') throw new TypeError(`${label}必须是字符串`);
    const normalized = value.replace(/[\u0000-\u001f\u007f]/g, '').trim();
    if (required && !normalized) throw new TypeError(`${label}不能为空`);
    if (normalized.length > max) throw new TypeError(`${label}过长`);
    return normalized;
}

function providerId(value) {
    const normalized = cleanText(value, 'Provider ID', { required: true, max: 64 }).toLowerCase();
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(normalized)) {
        throw new TypeError('Provider ID 必须以小写字母开头，且只能包含字母、数字、下划线和连字符');
    }
    return normalized;
}

function cleanBaseUrl(value, { required = false, allowHttp = false } = {}) {
    const normalized = cleanText(value, '服务地址', { required, max: 2048 });
    if (!normalized) return '';
    let parsed;
    try { parsed = new URL(normalized); }
    catch (_error) { throw new TypeError('服务地址必须是完整 URL'); }
    const protocols = allowHttp ? ['http:', 'https:'] : ['https:'];
    if (!protocols.includes(parsed.protocol)) {
        throw new TypeError(allowHttp ? '本机服务地址必须使用 HTTP 或 HTTPS' : '云端服务地址必须使用 HTTPS');
    }
    if (parsed.username || parsed.password) throw new TypeError('服务地址不得内嵌凭据');
    if (parsed.search || parsed.hash) throw new TypeError('服务地址不得包含查询参数或片段');
    return normalized.replace(/\/+$/, '');
}

function uniqueStrings(value, label, maxItems = 500) {
    if (value === undefined) return [];
    if (!Array.isArray(value) || value.length > maxItems) throw new TypeError(`${label}必须是有界数组`);
    const seen = new Set();
    const result = [];
    for (const raw of value) {
        const item = cleanText(raw, label, { required: true, max: 256 });
        if (seen.has(item)) throw new TypeError(`${label}不能重复`);
        seen.add(item);
        result.push(item);
    }
    return result;
}

function contextLimit(value) {
    if (!Number.isSafeInteger(value) || value < 4096 || value > 1_048_576) {
        throw new TypeError('上下文窗口必须是 4096 到 1048576 的整数');
    }
    return value;
}

function normalizeCapabilities(value) {
    if (!isRecord(value)) throw new TypeError('Provider capabilities 必须是对象');
    const result = {};
    for (const [key, enabled] of Object.entries(value)) {
        const name = cleanText(key, '能力名称', { required: true, max: 80 });
        if (typeof enabled !== 'boolean') throw new TypeError('Provider capability 必须是布尔值');
        result[name] = enabled;
    }
    return Object.freeze(result);
}

export function normalizeProvider(value) {
    exactFields(value, PROVIDER_FIELDS, 'Provider');
    const kind = cleanText(value.kind, 'Provider 类型', { required: true, max: 80 });
    if (![...CLOUD_KINDS, 'ollama'].includes(kind)) throw new TypeError('Provider 类型无效');
    const preset = cleanText(value.preset, 'Provider 预设', { required: true, max: 80 });
    if (![...PRESET_IDS].includes(preset)) throw new TypeError('Provider 预设无效');
    if (typeof value.credential_required !== 'boolean' || typeof value.has_credential !== 'boolean') {
        throw new TypeError('Provider 凭据状态必须是布尔值');
    }
    return Object.freeze({
        id: providerId(value.provider_id),
        kind,
        name: cleanText(value.name, 'Provider 名称', { required: true, max: 80 }),
        baseUrl: cleanBaseUrl(value.base_url, { required: true, allowHttp: kind === 'ollama' }),
        models: Object.freeze(uniqueStrings(value.models, '模型列表', 100)),
        preset,
        credentialRequired: value.credential_required,
        hasCredential: value.has_credential,
        capabilities: normalizeCapabilities(value.capabilities),
        contextLimit: contextLimit(value.context_limit),
    });
}

export function normalizeProvidersResponse(value) {
    exactFields(value, PROVIDER_LIST_FIELDS, 'Provider 列表响应');
    if (!Array.isArray(value.providers) || value.providers.length > 100) {
        throw new TypeError('providers 必须是有界数组');
    }
    const providers = value.providers.map(normalizeProvider);
    const ids = providers.map(provider => provider.id);
    if (new Set(ids).size !== ids.length) throw new TypeError('Provider ID 不能重复');
    return Object.freeze({ providers: Object.freeze(providers) });
}

export function normalizeProviderResponse(value) {
    exactFields(value, PROVIDER_RESPONSE_FIELDS, 'Provider 响应');
    return Object.freeze({ provider: normalizeProvider(value.provider) });
}

export function normalizePreset(value) {
    exactFields(value, PRESET_FIELDS, 'Provider 预设');
    const id = cleanText(value.id, '预设 ID', { required: true, max: 80 });
    if (!PRESET_IDS.has(id)) throw new TypeError('Provider 预设 ID 无效');
    const kind = cleanText(value.kind, '预设类型', { required: true, max: 80 });
    if (!CLOUD_KINDS.has(kind)) throw new TypeError('Provider 预设类型无效');
    return Object.freeze({
        id,
        name: cleanText(value.name, '预设名称', { required: true, max: 80 }),
        kind,
        baseUrl: value.base_url === null ? '' : cleanBaseUrl(value.base_url, { required: true }),
        contextLimit: contextLimit(value.context_limit),
    });
}

export function normalizePresetsResponse(value) {
    exactFields(value, PRESET_LIST_FIELDS, 'Provider 预设响应');
    if (!Array.isArray(value.presets) || value.presets.length > 20) {
        throw new TypeError('presets 必须是有界数组');
    }
    const presets = value.presets.map(normalizePreset);
    const ids = presets.map(preset => preset.id);
    if (new Set(ids).size !== ids.length) throw new TypeError('Provider 预设 ID 不能重复');
    return Object.freeze({ presets: Object.freeze(presets) });
}

export function normalizeProviderConfig(value) {
    exactFields(value, CONFIG_FIELDS, 'Provider 配置');
    const preset = cleanText(value.preset, 'Provider 预设', { required: true, max: 80 });
    if (!PRESET_IDS.has(preset)) throw new TypeError('Provider 预设无效');
    const result = { preset };
    result.context_limit = contextLimit(value.context_limit);
    if (Object.prototype.hasOwnProperty.call(value, 'name')) {
        result.name = cleanText(value.name, 'Provider 名称', { required: true, max: 80 });
    }
    if (Object.prototype.hasOwnProperty.call(value, 'models')) {
        result.models = uniqueStrings(value.models, '模型列表', 100);
    }
    if (preset === 'custom') {
        const kind = cleanText(value.kind, 'Provider 类型', { required: true, max: 80 });
        if (!CLOUD_KINDS.has(kind)) throw new TypeError('自定义 Provider 类型无效');
        result.kind = kind;
        result.base_url = cleanBaseUrl(value.base_url, { required: true });
    } else {
        if (Object.prototype.hasOwnProperty.call(value, 'kind')) {
            result.kind = cleanText(value.kind, 'Provider 类型', { required: true, max: 80 });
        }
        if (Object.prototype.hasOwnProperty.call(value, 'base_url')) {
            result.base_url = cleanBaseUrl(value.base_url, { required: true });
        }
    }
    return Object.freeze(result);
}

export function normalizeModelsResponse(value, expectedProvider) {
    const allowed = new Set(['provider', 'models']);
    exactFields(value, allowed, '模型列表响应');
    const expected = providerId(expectedProvider);
    const actual = value.provider === undefined ? expected : providerId(value.provider);
    if (actual !== expected) throw new TypeError('模型列表来源不一致');
    return Object.freeze({ provider: actual, models: Object.freeze(uniqueStrings(value.models, '模型列表', 500)) });
}

function providerPath(id, suffix = '') {
    return `${PROVIDER_ENDPOINTS.providers}/${encodeURIComponent(providerId(id))}${suffix}`;
}

function normalizeCredentialResponse(value, expectedProvider, configured) {
    const allowed = new Set(['credential']);
    exactFields(value, allowed, '凭据响应');
    const credential = value.credential;
    exactFields(credential, new Set(['provider_id', 'configured']), '凭据状态');
    if (providerId(credential.provider_id) !== providerId(expectedProvider)
        || credential.configured !== configured) {
        throw new TypeError('凭据响应与请求不一致');
    }
    return Object.freeze({ providerId: providerId(expectedProvider), configured });
}

function normalizeDeleteResponse(value, expectedProvider) {
    exactFields(value, new Set(['deleted', 'provider_id']), '删除 Provider 响应');
    if (value.deleted !== true || providerId(value.provider_id) !== providerId(expectedProvider)) {
        throw new TypeError('删除 Provider 响应与请求不一致');
    }
    return Object.freeze({ deleted: true, providerId: providerId(expectedProvider) });
}

function normalizeTestResponse(value, expectedProvider) {
    exactFields(value, new Set(['test']), '连接测试响应');
    const test = value.test;
    exactFields(test, new Set(['ok', 'provider_id', 'models', 'model_count', 'capabilities']), '连接测试结果');
    const expected = providerId(expectedProvider);
    const models = uniqueStrings(test.models, '测试模型列表', 500);
    if (test.ok !== true || providerId(test.provider_id) !== expected
        || !Number.isSafeInteger(test.model_count) || test.model_count !== models.length) {
        throw new TypeError('连接测试响应无效');
    }
    return Object.freeze({
        ok: true,
        providerId: expected,
        models: Object.freeze(models),
        modelCount: test.model_count,
        capabilities: normalizeCapabilities(test.capabilities),
    });
}

export function createProviderService(client) {
    if (!client || typeof client.get !== 'function' || typeof client.put !== 'function'
        || typeof client.post !== 'function' || typeof client.delete !== 'function') {
        throw new TypeError('Provider 服务缺少 API client');
    }
    return Object.freeze({
        async presets() {
            return normalizePresetsResponse(await client.get(PROVIDER_ENDPOINTS.presets));
        },
        async list() {
            return normalizeProvidersResponse(await client.get(PROVIDER_ENDPOINTS.providers));
        },
        async get(id) {
            return normalizeProviderResponse(await client.get(providerPath(id)));
        },
        async models(id) {
            const normalizedId = providerId(id);
            const path = `${PROVIDER_ENDPOINTS.models}?provider=${encodeURIComponent(normalizedId)}`;
            return normalizeModelsResponse(await client.get(path), normalizedId);
        },
        async save(id, config) {
            const normalizedId = providerId(id);
            return normalizeProviderResponse(await client.put(
                providerPath(normalizedId),
                normalizeProviderConfig(config),
            ));
        },
        async remove(id) {
            const normalizedId = providerId(id);
            return normalizeDeleteResponse(await client.delete(providerPath(normalizedId)), normalizedId);
        },
        async setCredential(id, apiKey) {
            const normalizedId = providerId(id);
            const key = cleanText(apiKey, 'API Key', { required: true, max: 16_384 });
            return normalizeCredentialResponse(
                await client.put(providerPath(normalizedId, '/credential'), { api_key: key }),
                normalizedId,
                true,
            );
        },
        async deleteCredential(id) {
            const normalizedId = providerId(id);
            return normalizeCredentialResponse(
                await client.delete(providerPath(normalizedId, '/credential')),
                normalizedId,
                false,
            );
        },
        async test(id) {
            const normalizedId = providerId(id);
            return normalizeTestResponse(
                await client.post(providerPath(normalizedId, '/test'), {}),
                normalizedId,
            );
        },
    });
}

function element(documentRef, tag, className = '', text = null) {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (text !== null) node.textContent = String(text);
    return node;
}

function button(documentRef, className, text, iconName = null) {
    const control = element(documentRef, 'button', className);
    control.type = 'button';
    if (iconName) control.appendChild(createIcon(documentRef, iconName, { size: 17 }));
    control.appendChild(element(documentRef, 'span', '', text));
    return control;
}

function textInput(documentRef, type = 'text') {
    const input = element(documentRef, 'input', 'provider-input');
    input.type = type;
    return input;
}

function field(documentRef, id, labelText, input, helpText = '') {
    const wrapper = element(documentRef, 'div', 'provider-field');
    const label = element(documentRef, 'label', 'provider-label', labelText);
    label.htmlFor = id;
    input.id = id;
    wrapper.appendChild(label);
    wrapper.appendChild(input);
    if (helpText) wrapper.appendChild(element(documentRef, 'small', 'provider-help', helpText));
    return wrapper;
}

function modelLines(value) {
    return String(value || '').split(/\r?\n/).map(item => item.trim()).filter(Boolean);
}

function presetById(presets, id) {
    return presets.find(preset => preset.id === id) || null;
}

function canonicalBaseUrl(value) {
    const parsed = new URL(cleanBaseUrl(value, { required: true }));
    const path = parsed.pathname.replace(/\/+$/, '');
    return `${parsed.protocol}//${parsed.host.toLowerCase()}${path}`;
}

export function requiresCredentialRebind(provider, config, presets) {
    if (!provider || !provider.hasCredential) return false;
    const rows = Array.isArray(presets) ? presets : presets?.presets;
    const preset = presetById(rows || [], config.preset);
    if (!preset) throw new TypeError('Provider 预设无效');
    const nextKind = config.kind || preset.kind;
    const nextBaseUrl = config.base_url || preset.baseUrl;
    return provider.kind !== nextKind
        || canonicalBaseUrl(provider.baseUrl) !== canonicalBaseUrl(nextBaseUrl);
}

function providerModels(snapshot, id) {
    return snapshot.providers.find(provider => provider.id === id)?.models || [];
}

export function describeProviderSelection(snapshot, providerValue, currentModelValue = '', modelsOverride = null) {
    const providerKey = typeof providerValue === 'string' ? providerValue.trim().toLowerCase() : '';
    const currentModel = typeof currentModelValue === 'string' ? currentModelValue.trim() : '';
    const provider = Array.isArray(snapshot?.providers)
        ? snapshot.providers.find(item => item.id === providerKey) || null
        : null;
    if (!provider) {
        return Object.freeze({
            status: 'missing_provider',
            providerId: providerKey,
            currentModel,
            models: Object.freeze([]),
            selectedModel: '',
        });
    }
    const models = Object.freeze(uniqueStrings(
        modelsOverride === null ? provider.models : modelsOverride,
        '模型列表',
        500,
    ));
    if (currentModel && !models.includes(currentModel)) {
        return Object.freeze({
            status: 'missing_model',
            providerId: provider.id,
            currentModel,
            models,
            selectedModel: '',
        });
    }
    if (!models.length) {
        return Object.freeze({
            status: 'no_models',
            providerId: provider.id,
            currentModel,
            models,
            selectedModel: '',
        });
    }
    return Object.freeze({
        status: 'ready',
        providerId: provider.id,
        currentModel,
        models,
        selectedModel: currentModel || models[0],
    });
}

export function activeProviderEntryChanged(previous, next, providerValue) {
    const providerKey = typeof providerValue === 'string' ? providerValue.trim().toLowerCase() : '';
    const find = snapshot => Array.isArray(snapshot?.providers)
        ? snapshot.providers.find(item => item.id === providerKey) || null
        : null;
    return JSON.stringify(find(previous)) !== JSON.stringify(find(next));
}

export function createProviderSettingsView({
    documentRef,
    service,
    initial,
    presets,
    currentProvider = null,
    currentModel = '',
    onActivate = null,
    onChanged = null,
    onPendingChange = null,
    confirmCredentialDelete = null,
    confirmProviderDelete = null,
}) {
    if (!documentRef || typeof documentRef.createElement !== 'function' || !service) {
        throw new TypeError('Provider 设置视图参数不完整');
    }
    let snapshot = initial && initial.providers ? initial : normalizeProvidersResponse(initial);
    let presetSnapshot = presets && presets.presets ? presets : normalizePresetsResponse(presets);
    let selectedId = currentProvider && snapshot.providers.some(item => item.id === currentProvider)
        ? currentProvider
        : (snapshot.providers[0]?.id || null);
    let creating = false;
    let pending = false;
    const root = element(documentRef, 'section', 'provider-settings');
    const sidebar = element(documentRef, 'nav', 'provider-list');
    sidebar.setAttribute('aria-label', '模型来源列表');
    const detail = element(documentRef, 'div', 'provider-detail');
    const live = element(documentRef, 'p', 'provider-live');
    live.setAttribute('role', 'status');
    live.setAttribute('aria-live', 'polite');
    root.appendChild(sidebar);
    root.appendChild(detail);

    const setStatus = (message, error = false) => {
        live.textContent = String(message || '');
        live.classList.toggle('error', Boolean(error));
        live.setAttribute('role', error ? 'alert' : 'status');
        live.setAttribute('aria-live', error ? 'assertive' : 'polite');
    };

    const setLocked = (control, locked) => {
        control.dataset.permanentlyDisabled = String(Boolean(locked));
        control.disabled = pending || Boolean(locked);
    };

    const syncPendingControls = () => {
        for (const control of root.querySelectorAll('button, input, select, textarea')) {
            control.disabled = pending || control.dataset.permanentlyDisabled === 'true';
        }
    };

    const setPending = value => {
        pending = Boolean(value);
        root.setAttribute('aria-busy', String(pending));
        syncPendingControls();
        if (typeof onPendingChange === 'function') onPendingChange(pending);
    };

    const run = async (label, action, success) => {
        if (pending) return null;
        setPending(true);
        setStatus(`${label}…`);
        try {
            const result = await action();
            setStatus(success);
            return result;
        } catch (error) {
            setStatus(`${label}失败：${error instanceof Error ? error.message : String(error)}`, true);
            return null;
        } finally {
            setPending(false);
        }
    };

    const notifyChanged = (changedProviderId, reason) => {
        if (typeof onChanged === 'function') {
            onChanged(snapshot, Object.freeze({ providerId: changedProviderId || null, reason }));
        }
    };

    const refreshSnapshot = async (preferredId, change = {}) => {
        snapshot = await service.list();
        if (preferredId && snapshot.providers.some(provider => provider.id === preferredId)) {
            selectedId = preferredId;
        } else if (!snapshot.providers.some(provider => provider.id === selectedId)) {
            selectedId = snapshot.providers[0]?.id || null;
        }
        creating = false;
        notifyChanged(change.providerId || preferredId, change.reason || 'catalog');
        render();
        return snapshot;
    };

    const renderSidebar = () => {
        sidebar.replaceChildren();
        sidebar.appendChild(element(documentRef, 'h3', 'provider-section-title', '模型来源'));
        for (const provider of snapshot.providers) {
            const item = button(
                documentRef,
                'provider-list-item',
                provider.name,
                provider.kind === 'ollama' ? 'provider' : 'cloud',
            );
            item.dataset.providerId = provider.id;
            item.setAttribute('aria-pressed', String(!creating && provider.id === selectedId));
            item.classList.toggle('active', !creating && provider.id === selectedId);
            item.appendChild(element(
                documentRef,
                'small',
                'provider-list-meta',
                `${provider.kind} · ${provider.credentialRequired && !provider.hasCredential ? '待配置密钥' : '可用'}`,
            ));
            item.addEventListener('click', () => {
                if (pending || (!creating && selectedId === provider.id)) return;
                creating = false;
                selectedId = provider.id;
                render();
            });
            sidebar.appendChild(item);
        }
        const add = button(documentRef, 'provider-list-add', '添加模型来源', 'plus');
        add.setAttribute('aria-pressed', String(creating));
        add.classList.toggle('active', creating);
        add.addEventListener('click', () => {
            if (pending) return;
            creating = true;
            render();
        });
        sidebar.appendChild(add);
    };

    const cloudWarning = () => {
        const warning = element(documentRef, 'div', 'provider-cloud-warning');
        warning.setAttribute('role', 'note');
        warning.appendChild(createIcon(documentRef, 'cloud', { size: 20 }));
        warning.appendChild(element(
            documentRef,
            'p',
            '',
            '隐私警告：测试或使用云端模型时，对话上下文、角色设定与世界书内容会发送给所选服务商。请确认你接受其数据处理政策后再继续。',
        ));
        return warning;
    };

    const appendModelOptions = (select, models, selected) => {
        const values = [...models];
        if (selected && !values.includes(selected)) {
            const invalid = element(documentRef, 'option', '', `当前模型已不可用：${selected}`);
            invalid.value = '';
            invalid.disabled = true;
            invalid.selected = true;
            select.appendChild(invalid);
        }
        for (const value of values) {
            const option = element(documentRef, 'option', '', value);
            option.value = value;
            option.selected = value === selected;
            select.appendChild(option);
        }
        if (!values.length && !selected) {
            const option = element(documentRef, 'option', '', '请先填写模型或测试连接');
            option.value = '';
            select.appendChild(option);
        }
    };

    const renderBuiltIn = provider => {
        const heading = element(documentRef, 'div', 'provider-detail-heading');
        const title = element(documentRef, 'div');
        title.appendChild(element(documentRef, 'h3', 'provider-detail-title', provider.name));
        title.appendChild(element(documentRef, 'p', 'provider-detail-kind', `${provider.kind} · ${provider.baseUrl}`));
        heading.appendChild(title);
        heading.appendChild(element(documentRef, 'span', 'provider-badge ready', '本机来源'));
        detail.appendChild(heading);
        const model = element(documentRef, 'select', 'provider-input');
        appendModelOptions(model, provider.models, currentProvider === provider.id ? currentModel : '');
        detail.appendChild(field(documentRef, `provider-${provider.id}-model`, '模型', model));
        const actions = element(documentRef, 'div', 'provider-actions');
        const refresh = button(documentRef, 'modal-btn', '刷新模型', 'regenerate');
        const test = button(documentRef, 'modal-btn', '测试连接', 'network');
        const activate = button(documentRef, 'modal-btn primary', '设为当前来源', 'provider');
        actions.appendChild(refresh);
        actions.appendChild(test);
        actions.appendChild(activate);
        detail.appendChild(actions);
        detail.appendChild(live);

        const applyModels = models => {
            const index = snapshot.providers.findIndex(item => item.id === provider.id);
            const nextProvider = Object.freeze({ ...provider, models: Object.freeze([...models]) });
            snapshot = Object.freeze({
                providers: Object.freeze(snapshot.providers.map((item, itemIndex) => itemIndex === index ? nextProvider : item)),
            });
            notifyChanged(provider.id, 'models');
            render();
        };
        refresh.addEventListener('click', () => void run(
            '刷新模型',
            async () => applyModels((await service.models(provider.id)).models),
            '模型列表已刷新',
        ));
        test.addEventListener('click', () => void run(
            '测试连接',
            async () => applyModels((await service.test(provider.id)).models),
            '连接测试通过',
        ));
        activate.addEventListener('click', () => void run(
            '切换模型来源',
            async () => {
                if (!model.value) throw new TypeError('请先选择模型');
                if (typeof onActivate !== 'function') throw new TypeError('模型切换处理器未配置');
                const result = await onActivate({ provider: provider.id, model: model.value });
                currentProvider = provider.id;
                currentModel = model.value;
                render();
                return result;
            },
            '当前模型来源已切换',
        ));
    };

    const renderCloudForm = provider => {
        const isNew = !provider;
        const defaultPreset = presetSnapshot.presets.find(item => item.id !== 'custom') || presetSnapshot.presets[0];
        const initialPreset = provider?.preset || defaultPreset?.id || 'custom';
        const prefix = `provider-${provider?.id || 'new'}`;

        const heading = element(documentRef, 'div', 'provider-detail-heading');
        const title = element(documentRef, 'div');
        title.appendChild(element(documentRef, 'h3', 'provider-detail-title', isNew ? '添加模型来源' : provider.name));
        title.appendChild(element(documentRef, 'p', 'provider-detail-kind', isNew ? '配置 OpenAI-compatible 或 Anthropic 服务' : provider.kind));
        heading.appendChild(title);
        if (!isNew) {
            const badges = element(documentRef, 'div', 'provider-badges');
            badges.appendChild(element(documentRef, 'span', 'provider-badge ready', '云端来源'));
            badges.appendChild(element(
                documentRef,
                'span',
                `provider-badge ${provider.hasCredential ? 'ready' : 'warning'}`,
                provider.hasCredential ? '密钥已配置' : '未配置密钥',
            ));
            heading.appendChild(badges);
        }
        detail.appendChild(heading);
        detail.appendChild(cloudWarning());

        const form = element(documentRef, 'div', 'provider-form');
        const idInput = textInput(documentRef);
        idInput.value = provider?.id || '';
        idInput.maxLength = 64;
        idInput.placeholder = '例如 my-cloud';
        setLocked(idInput, !isNew);

        const presetSelect = element(documentRef, 'select', 'provider-input');
        for (const item of presetSnapshot.presets) {
            const option = element(documentRef, 'option', '', item.name);
            option.value = item.id;
            option.selected = item.id === initialPreset;
            presetSelect.appendChild(option);
        }
        const nameInput = textInput(documentRef);
        nameInput.maxLength = 80;
        const kindSelect = element(documentRef, 'select', 'provider-input');
        for (const [value, label] of [['openai_compatible', 'OpenAI-compatible'], ['anthropic', 'Anthropic']]) {
            const option = element(documentRef, 'option', '', label);
            option.value = value;
            kindSelect.appendChild(option);
        }
        const baseUrlInput = textInput(documentRef, 'url');
        baseUrlInput.placeholder = 'https://api.example.com/v1';
        baseUrlInput.title = '云端服务地址仅支持 HTTPS，且不得包含凭据、查询参数或片段';
        const modelsInput = element(documentRef, 'textarea', 'provider-input provider-models-input');
        modelsInput.rows = 4;
        modelsInput.placeholder = '每行一个模型名称';
        modelsInput.value = (provider?.models || []).join('\n');
        const contextInput = textInput(documentRef, 'number');
        contextInput.min = '4096';
        contextInput.max = '1048576';
        contextInput.step = '1';
        contextInput.inputMode = 'numeric';
        contextInput.value = String(provider?.contextLimit || 32768);
        const keyInput = textInput(documentRef, 'password');
        keyInput.autocomplete = 'new-password';
        keyInput.placeholder = provider?.hasCredential ? '留空则保留现有密钥' : '输入 API Key';

        const applyPreset = ({ resetName = false } = {}) => {
            const preset = presetById(presetSnapshot.presets, presetSelect.value);
            if (!preset) return;
            if (resetName || !nameInput.value) nameInput.value = preset.name;
            kindSelect.value = preset.kind;
            baseUrlInput.value = preset.baseUrl;
            contextInput.value = String(preset.contextLimit);
            const custom = preset.id === 'custom';
            setLocked(kindSelect, !custom);
            setLocked(baseUrlInput, !custom);
            if (isNew && !idInput.value) idInput.value = custom ? '' : preset.id;
        };
        if (provider) {
            nameInput.value = provider.name;
            kindSelect.value = provider.kind;
            baseUrlInput.value = provider.baseUrl;
            const custom = initialPreset === 'custom';
            setLocked(kindSelect, !custom);
            setLocked(baseUrlInput, !custom);
        } else {
            applyPreset({ resetName: true });
        }
        presetSelect.addEventListener('change', () => {
            if (isNew) idInput.value = presetSelect.value === 'custom' ? '' : presetSelect.value;
            applyPreset({ resetName: true });
        });

        form.appendChild(field(documentRef, `${prefix}-id`, 'Provider ID', idInput, '创建后不可修改'));
        form.appendChild(field(documentRef, `${prefix}-preset`, '服务预设', presetSelect));
        form.appendChild(field(documentRef, `${prefix}-name`, '显示名称', nameInput));
        form.appendChild(field(documentRef, `${prefix}-kind`, '接口类型', kindSelect));
        form.appendChild(field(
            documentRef,
            `${prefix}-url`,
            '服务地址（仅 HTTPS）',
            baseUrlInput,
            '仅允许 HTTPS 公网地址；不得在 URL 中写入 API Key、查询参数或片段',
        ));
        form.appendChild(field(documentRef, `${prefix}-models`, '模型名称', modelsInput, '每行一个；也可在保存后通过连接测试获取'));
        form.appendChild(field(
            documentRef,
            `${prefix}-context`,
            '上下文窗口',
            contextInput,
            '默认 32768；请按所选模型的实际上下文上限填写',
        ));
        form.appendChild(field(documentRef, `${prefix}-key`, '新 API Key（可选）', keyInput, '已保存密钥不会回显；留空不会修改现有密钥'));

        const actions = element(documentRef, 'div', 'provider-actions');
        const save = button(documentRef, 'modal-btn primary', isNew ? '保存并添加' : '保存配置', 'check');
        const test = button(documentRef, 'modal-btn', '测试连接', 'network');
        const activate = button(documentRef, 'modal-btn', '设为当前来源', 'provider');
        const deleteKey = button(documentRef, 'modal-btn danger', '删除密钥', 'key');
        const remove = button(documentRef, 'modal-btn danger', '删除配置', 'trash');
        actions.appendChild(save);
        if (!isNew) {
            actions.appendChild(test);
            actions.appendChild(activate);
            actions.appendChild(deleteKey);
            actions.appendChild(remove);
        }
        form.appendChild(actions);
        detail.appendChild(form);

        let modelSelect = null;
        if (!isNew) {
            const activation = element(documentRef, 'section', 'provider-activation');
            activation.appendChild(element(documentRef, 'h4', 'provider-section-title', '当前模型'));
            modelSelect = element(documentRef, 'select', 'provider-input');
            appendModelOptions(
                modelSelect,
                providerModels(snapshot, provider.id),
                currentProvider === provider.id ? currentModel : '',
            );
            activation.appendChild(field(documentRef, `${prefix}-active-model`, '切换时使用', modelSelect));
            detail.appendChild(activation);
        }
        detail.appendChild(live);

        const readConfig = () => {
            const config = {
                preset: presetSelect.value,
                name: nameInput.value,
                models: modelLines(modelsInput.value),
                context_limit: Number(contextInput.value),
            };
            if (presetSelect.value === 'custom') {
                config.kind = kindSelect.value;
                config.base_url = baseUrlInput.value;
            }
            return normalizeProviderConfig(config);
        };

        save.addEventListener('click', () => void run(
            '保存模型来源',
            async () => {
                const id = providerId(idInput.value);
                const config = readConfig();
                if (!isNew && requiresCredentialRebind(provider, config, presetSnapshot)) {
                    throw new TypeError('修改接口类型或服务地址前，请先删除已保存密钥，再保存新配置与新密钥');
                }
                await service.save(id, config);
                if (keyInput.value.trim()) {
                    await service.setCredential(id, keyInput.value);
                    keyInput.value = '';
                }
                return refreshSnapshot(id, { providerId: id, reason: 'save' });
            },
            '模型来源已保存',
        ));
        if (!isNew) {
            test.addEventListener('click', () => void run(
                '测试连接',
                async () => {
                    const response = await service.test(provider.id);
                    modelsInput.value = response.models.join('\n');
                    const index = snapshot.providers.findIndex(item => item.id === provider.id);
                    snapshot = Object.freeze({
                        providers: Object.freeze(snapshot.providers.map((item, itemIndex) => (
                            itemIndex === index
                                ? Object.freeze({ ...item, models: Object.freeze([...response.models]) })
                                : item
                        ))),
                    });
                    notifyChanged(provider.id, 'models');
                    render();
                    return response;
                },
                '连接测试通过；模型列表已载入，保存配置后持久化',
            ));
            activate.addEventListener('click', () => void run(
                '切换模型来源',
                async () => {
                    if (!modelSelect.value) throw new TypeError('请先填写模型并保存或测试连接');
                    if (typeof onActivate !== 'function') throw new TypeError('模型切换处理器未配置');
                    const result = await onActivate({ provider: provider.id, model: modelSelect.value });
                    currentProvider = provider.id;
                    currentModel = modelSelect.value;
                    render();
                    return result;
                },
                '当前模型来源已切换',
            ));
            setLocked(deleteKey, !provider.hasCredential);
            setLocked(remove, provider.id === currentProvider);
            if (provider.id === currentProvider) {
                remove.title = '请先切换到其他模型来源';
                remove.setAttribute('aria-label', '删除配置（当前来源不可删除）');
            }
            deleteKey.addEventListener('click', () => {
                if (typeof confirmCredentialDelete === 'function' && !confirmCredentialDelete(provider)) return;
                void run(
                    '删除密钥',
                    async () => {
                        await service.deleteCredential(provider.id);
                        return refreshSnapshot(provider.id, {
                            providerId: provider.id,
                            reason: 'credential',
                        });
                    },
                    '密钥已删除',
                );
            });
            remove.addEventListener('click', () => {
                if (typeof confirmProviderDelete === 'function' && !confirmProviderDelete(provider)) return;
                void run(
                    '删除模型来源',
                    async () => {
                        await service.remove(provider.id);
                        return refreshSnapshot(null, { providerId: provider.id, reason: 'delete' });
                    },
                    '模型来源已删除',
                );
            });
        }
    };

    const renderDetail = () => {
        detail.replaceChildren();
        const provider = creating ? null : snapshot.providers.find(item => item.id === selectedId);
        const referencedProvider = currentProvider
            ? snapshot.providers.find(item => item.id === currentProvider) || null
            : null;
        if (currentProvider && !referencedProvider) {
            const recovery = element(documentRef, 'div', 'provider-recovery-note');
            recovery.setAttribute('role', 'alert');
            recovery.appendChild(createIcon(documentRef, 'provider', { size: 20 }));
            recovery.appendChild(element(
                documentRef,
                'p',
                '',
                `当前存档引用的模型来源「${currentProvider}」已缺失。请在下方选择可用来源和模型，再点击“设为当前来源”。`,
            ));
            detail.appendChild(recovery);
        } else if (
            referencedProvider
            && currentModel
            && referencedProvider.models.length
            && !referencedProvider.models.includes(currentModel)
        ) {
            const recovery = element(documentRef, 'div', 'provider-recovery-note');
            recovery.setAttribute('role', 'alert');
            recovery.appendChild(createIcon(documentRef, 'provider', { size: 20 }));
            recovery.appendChild(element(
                documentRef,
                'p',
                '',
                `当前模型「${currentModel}」已不在来源的可用列表中。请选择新模型并重新设为当前来源。`,
            ));
            detail.appendChild(recovery);
        }
        if (creating) renderCloudForm(null);
        else if (!provider) {
            detail.appendChild(element(documentRef, 'p', 'provider-empty', '还没有可配置的模型来源。'));
            detail.appendChild(live);
        } else if (provider.kind === 'ollama') renderBuiltIn(provider);
        else renderCloudForm(provider);
    };

    function render() {
        renderSidebar();
        renderDetail();
        syncPendingControls();
    }

    render();
    return Object.freeze({
        root,
        selectedProvider: () => creating ? null : selectedId,
        snapshot: () => snapshot,
        refresh: () => run(
            '刷新模型来源',
            () => refreshSnapshot(selectedId, { providerId: selectedId, reason: 'refresh' }),
            '模型来源已刷新',
        ),
        setDisabled(value) { setPending(Boolean(value)); },
    });
}

export const providerEndpoints = PROVIDER_ENDPOINTS;
