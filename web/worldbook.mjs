const ACTIVATIONS = Object.freeze(['always', 'keywords', 'manual']);
const ACTIVATION_SET = new Set(ACTIVATIONS);
const CANONICAL_ENTRY_FIELDS = new Set([
    'id', 'title', 'enabled', 'activation', 'keywords', 'priority', 'content', 'custom',
]);
const PRESERVED_ENTRY_FIELDS = Symbol('worldbook-preserved-entry-fields');

function isObject(value) {
    return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function defineDataField(target, key, value) {
    Object.defineProperty(target, key, {
        value,
        enumerable: true,
        configurable: true,
        writable: true,
    });
}

function preservedEntryFields(value) {
    const result = {};
    const inherited = value && value[PRESERVED_ENTRY_FIELDS];
    if (isObject(inherited)) {
        for (const [key, fieldValue] of Object.entries(inherited)) {
            defineDataField(result, key, fieldValue);
        }
    }
    if (isObject(value)) {
        for (const [key, fieldValue] of Object.entries(value)) {
            if (!CANONICAL_ENTRY_FIELDS.has(key)) defineDataField(result, key, fieldValue);
        }
    }
    return result;
}

function attachedPreservedEntryFields(value) {
    const result = {};
    const fields = value && value[PRESERVED_ENTRY_FIELDS];
    if (!isObject(fields)) return result;
    for (const [key, fieldValue] of Object.entries(fields)) {
        defineDataField(result, key, fieldValue);
    }
    return result;
}

function attachPreservedEntryFields(target, fields) {
    Object.defineProperty(target, PRESERVED_ENTRY_FIELDS, {
        value: Object.freeze(preservedEntryFields(fields)),
        enumerable: false,
        configurable: false,
        writable: false,
    });
    return target;
}

function requireString(value, label, { allowEmpty = true } = {}) {
    if (typeof value !== 'string') throw new TypeError(`${label}必须是字符串`);
    const normalized = value.trim();
    if (!allowEmpty && !normalized) throw new TypeError(`${label}不能为空`);
    return normalized;
}

function normalizeKeywords(value, { strict = false } = {}) {
    if (value === undefined || value === null) return [];
    if (!Array.isArray(value)) {
        if (strict) throw new TypeError('关键词必须是字符串数组');
        return [];
    }
    const result = [];
    const seen = new Set();
    for (const item of value) {
        if (typeof item !== 'string') {
            if (strict) throw new TypeError('关键词必须全部是字符串');
            continue;
        }
        const keyword = item.trim();
        const canonical = keyword.normalize('NFKC').toLocaleLowerCase();
        if (!keyword || seen.has(canonical)) continue;
        seen.add(canonical);
        result.push(keyword);
    }
    return result;
}

function normalizeCustom(value, { strict = false } = {}) {
    if (value === undefined || value === null) return {};
    if (!isObject(value)) {
        if (strict) throw new TypeError('自定义字段必须是对象');
        return {};
    }
    const result = {};
    for (const [rawKey, rawValue] of Object.entries(value)) {
        const key = String(rawKey || '').trim();
        if (!key) continue;
        result[key] = rawValue;
    }
    return result;
}

function priorityValue(value, { strict = false } = {}) {
    if (value === undefined || value === null || value === '') return 0;
    if (typeof value === 'boolean') {
        if (strict) throw new TypeError('优先级必须是整数');
        return 0;
    }
    const number = typeof value === 'number' ? value : Number(String(value).trim());
    if (!Number.isSafeInteger(number)) {
        if (strict) throw new TypeError('优先级必须是安全整数');
        return 0;
    }
    return number;
}

export class WorldbookValidationError extends TypeError {
    constructor(message, field = '') {
        super(message);
        this.name = 'WorldbookValidationError';
        this.field = field;
    }
}

const DEFAULT_SCHEMA = Object.freeze({
    id: 'worldbook',
    label: '世界书',
    groups: Object.freeze([{ fields: Object.freeze([
        { key: 'id', label: '稳定 ID（文件名）', type: 'text', required: true, fixed: true },
        { key: 'title', label: '标题', type: 'text' },
        { key: 'enabled', label: '启用此条目', type: 'checkbox' },
        { key: 'activation', label: '触发方式', type: 'select', options: [
            { value: 'always', label: '常驻（always）' },
            { value: 'keywords', label: '关键词（keywords）' },
            { value: 'manual', label: '手动（manual）' },
        ] },
        { key: 'keywords', label: '触发关键词（每行一个）', type: 'textarea', rows: 4, maxItems: 64 },
        { key: 'priority', label: '优先级', type: 'number', min: -1_000_000, max: 1_000_000 },
        { key: 'content', label: '设定内容', type: 'textarea', rows: 8 },
    ]) }]),
    customGroup: Object.freeze({ key: '_custom', label: '自定义字段' }),
    activationValues: ACTIVATIONS,
});

export function normalizeWorldbookSchema(value) {
    if (!isObject(value)) throw new TypeError('世界书 Schema 响应无效');
    const fields = Array.isArray(value.groups)
        ? value.groups.flatMap(group => (
            isObject(group) && Array.isArray(group.fields) ? group.fields : []
        ))
        : isObject(value.fields) ? Object.values(value.fields) : null;
    if (!fields) throw new TypeError('世界书 Schema 响应无效');
    const byKey = new Map();
    for (const raw of fields) {
        if (!isObject(raw) || typeof raw.key !== 'string' || !raw.key.trim()) continue;
        const field = {
            key: raw.key.trim(),
            label: typeof raw.label === 'string' && raw.label.trim() ? raw.label.trim() : raw.key.trim(),
            type: typeof raw.type === 'string' ? raw.type : 'text',
            required: raw.required === true,
            fixed: raw.fixed === true,
            array: raw.array === true,
            rows: Number.isSafeInteger(raw.rows) && raw.rows > 0 ? raw.rows : undefined,
            min: Number.isSafeInteger(raw.min) ? raw.min : undefined,
            max: Number.isSafeInteger(raw.max) ? raw.max : undefined,
            maxItems: Number.isSafeInteger(raw.maxItems) && raw.maxItems >= 0 ? raw.maxItems : undefined,
            hint: typeof raw.hint === 'string' ? raw.hint : '',
            options: Array.isArray(raw.options) ? raw.options.flatMap(option => (
                isObject(option) && typeof option.value === 'string' && typeof option.label === 'string'
                    ? [{ value: option.value, label: option.label }]
                    : []
            )) : [],
        };
        byKey.set(field.key, field);
    }
    for (const key of ['id', 'title', 'enabled', 'activation', 'keywords', 'priority', 'content']) {
        if (!byKey.has(key)) throw new TypeError(`世界书 Schema 缺少字段：${key}`);
    }
    const activationField = byKey.get('activation');
    const optionValues = activationField.options.map(option => option.value);
    if (!ACTIVATIONS.every(value => optionValues.includes(value))) {
        throw new TypeError('世界书 Schema 缺少 activation 选项');
    }
    return Object.freeze({
        id: typeof value.id === 'string' ? value.id : 'worldbook',
        label: typeof value.label === 'string' ? value.label : '世界书',
        fields: Object.freeze(Object.fromEntries([...byKey].map(([key, field]) => [key, Object.freeze(field)]))),
        customGroup: Object.freeze(isObject(value.customGroup) ? {
            key: typeof value.customGroup.key === 'string' ? value.customGroup.key : '_custom',
            label: typeof value.customGroup.label === 'string' ? value.customGroup.label : '自定义字段',
        } : { key: '_custom', label: '自定义字段' }),
    });
}

function defaultNormalizedSchema() {
    return normalizeWorldbookSchema(DEFAULT_SCHEMA);
}

export function normalizeWorldbookEntry(value) {
    if (!isObject(value)) throw new TypeError('世界书条目必须是对象');
    const id = requireString(value.id, '世界书 ID', { allowEmpty: false });
    if (value.enabled !== undefined && typeof value.enabled !== 'boolean') {
        throw new TypeError(`世界书「${id}」的 enabled 必须是布尔值`);
    }
    const activation = value.activation === undefined
        ? 'always'
        : requireString(value.activation, `世界书「${id}」的 activation`, { allowEmpty: false });
    if (!ACTIVATION_SET.has(activation)) {
        throw new TypeError(`世界书「${id}」的 activation 无效`);
    }
    if (value.content !== undefined && typeof value.content !== 'string') {
        throw new TypeError(`世界书「${id}」的 content 必须是字符串`);
    }
    if (value.title !== undefined && typeof value.title !== 'string') {
        throw new TypeError(`世界书「${id}」的 title 必须是字符串`);
    }
    const normalized = {
        id,
        enabled: value.enabled !== false,
        activation,
        keywords: Object.freeze(normalizeKeywords(value.keywords, { strict: true })),
        priority: priorityValue(value.priority, { strict: true }),
        title: value.title || '',
        content: value.content || '',
        custom: Object.freeze(normalizeCustom(value.custom, { strict: true })),
    };
    attachPreservedEntryFields(normalized, value);
    return Object.freeze(normalized);
}

export function normalizeWorldbookEntries(value) {
    if (!Array.isArray(value)) throw new TypeError('世界书列表必须是数组');
    const entries = value.map(normalizeWorldbookEntry);
    const seen = new Set();
    for (const entry of entries) {
        if (seen.has(entry.id)) throw new TypeError(`世界书 ID 重复：${entry.id}`);
        seen.add(entry.id);
    }
    return entries;
}

export function normalizeManualWorldbookIds(value, { strict = false } = {}) {
    if (value === undefined || value === null) return [];
    if (!Array.isArray(value)) {
        if (strict) throw new TypeError('manual 世界书 ID 必须是数组');
        return [];
    }
    const result = new Set();
    for (const item of value) {
        if (typeof item !== 'string') {
            if (strict) throw new TypeError('manual 世界书 ID 必须全部是字符串');
            continue;
        }
        const id = item.trim();
        if (id) result.add(id);
    }
    return [...result].sort();
}

function draftFromEntry(entry) {
    const draft = {
        originalId: entry ? entry.id : null,
        id: entry ? entry.id : '',
        enabled: entry ? entry.enabled : true,
        activation: entry ? entry.activation : 'always',
        keywordsText: entry ? entry.keywords.join('\n') : '',
        priority: entry ? String(entry.priority) : '0',
        title: entry ? entry.title : '',
        content: entry ? entry.content : '',
        customRows: Object.entries(entry ? entry.custom : {}).map(([key, value]) => ({
            key,
            value: typeof value === 'string' ? value : JSON.stringify(value),
            originalKey: key,
            originalValue: value,
            dirty: false,
        })),
        error: '',
        errorField: '',
        status: '',
    };
    attachPreservedEntryFields(draft, entry || {});
    return draft;
}

function serializeCustomRows(rows) {
    const custom = {};
    for (const row of rows || []) {
        const key = String(row && row.key || '').trim();
        const value = String(row && row.value != null ? row.value : '').trim();
        if (!key && !value) continue;
        if (!key) throw new WorldbookValidationError('自定义字段名不能为空', 'custom');
        if (Object.prototype.hasOwnProperty.call(custom, key)) {
            throw new WorldbookValidationError(`自定义字段名重复：${key}`, 'custom');
        }
        custom[key] = row && row.dirty !== true && row.originalKey === key
            ? row.originalValue
            : value;
    }
    return custom;
}

export function serializeWorldbookDraft(draft, options = {}) {
    if (!isObject(draft)) throw new WorldbookValidationError('世界书草稿必须是对象');
    let id;
    try {
        id = requireString(draft.id, '世界书 ID', { allowEmpty: false });
    } catch (error) {
        throw new WorldbookValidationError(error.message, 'id');
    }
    if (typeof draft.enabled !== 'boolean') {
        throw new WorldbookValidationError('全局可用状态必须是布尔值', 'enabled');
    }
    const activation = String(draft.activation || '');
    if (!ACTIVATION_SET.has(activation)) {
        throw new WorldbookValidationError('触发方式必须是 always、keywords 或 manual', 'activation');
    }
    const schema = options.schema ? normalizeWorldbookSchema(options.schema) : defaultNormalizedSchema();
    let priority;
    try {
        priority = priorityValue(draft.priority, { strict: true });
    } catch (error) {
        throw new WorldbookValidationError(error.message, 'priority');
    }
    if (typeof draft.content !== 'string') {
        throw new WorldbookValidationError('设定内容必须是字符串', 'content');
    }
    if (typeof draft.title !== 'string') {
        throw new WorldbookValidationError('标题必须是字符串', 'title');
    }
    const keywords = normalizeKeywords(String(draft.keywordsText || '').split(/\r?\n/), { strict: true });
    const keywordField = schema.fields.keywords;
    if (activation === 'keywords' && keywords.length === 0) {
        throw new WorldbookValidationError('关键词触发方式至少需要一个关键词', 'keywords');
    }
    if (keywordField.maxItems !== undefined && keywords.length > keywordField.maxItems) {
        throw new WorldbookValidationError(`关键词不能超过 ${keywordField.maxItems} 条`, 'keywords');
    }
    const priorityField = schema.fields.priority;
    if ((priorityField.min !== undefined && priority < priorityField.min)
        || (priorityField.max !== undefined && priority > priorityField.max)) {
        throw new WorldbookValidationError(
            `优先级必须在 ${priorityField.min} 到 ${priorityField.max} 之间`,
            'priority',
        );
    }
    const custom = serializeCustomRows(draft.customRows);
    const result = {};
    for (const [key, value] of Object.entries(attachedPreservedEntryFields(draft))) {
        defineDataField(result, key, value);
    }
    Object.assign(result, {
        id,
        enabled: draft.enabled,
        activation,
        keywords,
        priority,
        title: draft.title,
        content: draft.content,
    });
    if (Object.keys(custom).length > 0) result.custom = custom;
    return result;
}

function diagnosticArray(value) {
    if (!isObject(value)) return [];
    for (const key of ['worldbook', 'worldbook_entries', 'worldbook_matches', 'worldbook_diagnostics']) {
        if (Array.isArray(value[key])) return value[key];
    }
    if (Array.isArray(value.sources)) {
        return value.sources.filter(item => isObject(item) && item.source === 'worldbook');
    }
    return [];
}

function diagnosticStrings(value) {
    if (!Array.isArray(value)) return [];
    return [...new Set(value.filter(item => typeof item === 'string').map(item => item.trim()).filter(Boolean))];
}

function diagnosticSources(value) {
    if (!Array.isArray(value)) return [];
    const result = [];
    const seen = new Set();
    for (const raw of value) {
        let label = '';
        if (typeof raw === 'string') {
            label = raw.trim();
        } else if (isObject(raw)) {
            const scope = typeof raw.scope === 'string' ? raw.scope.trim() : '';
            const ref = typeof raw.ref === 'string' ? raw.ref.trim() : '';
            const distance = Number.isSafeInteger(raw.turn_distance) && raw.turn_distance >= 0
                ? `，距当前 ${raw.turn_distance} 轮`
                : '';
            if (scope || ref) label = `${scope || 'context'}:${ref || 'unknown'}${distance}`;
        }
        if (!label || seen.has(label)) continue;
        seen.add(label);
        result.push(label);
    }
    return result;
}

export function sanitizeWorldbookDiagnostics(value) {
    return diagnosticArray(value).flatMap(raw => {
        if (!isObject(raw) || typeof raw.id !== 'string' || !raw.id.trim()) return [];
        const activation = ACTIVATION_SET.has(raw.activation) ? raw.activation : '';
        const priority = Number.isSafeInteger(raw.priority) ? raw.priority : 0;
        const matchCount = Number.isSafeInteger(raw.hit_count) && raw.hit_count >= 0
            ? raw.hit_count
            : Number.isSafeInteger(raw.match_count) && raw.match_count >= 0
                ? raw.match_count
            : diagnosticStrings(raw.matched_keywords || raw.keywords).length;
        const recency = Number.isSafeInteger(raw.recency_distance) && raw.recency_distance >= 0
            ? raw.recency_distance
            : Number.isSafeInteger(raw.recency) && raw.recency >= 0 ? raw.recency : null;
        return [{
            id: raw.id.trim(),
            activation,
            enabled: raw.enabled !== false,
            activated: raw.activated === true,
            priority,
            matchCount,
            recency,
            matchedKeywords: diagnosticStrings(raw.matched_keywords || raw.keywords),
            matchedSources: diagnosticSources(raw.matched_sources || raw.sources),
            rank: Number.isSafeInteger(raw.rank) && raw.rank > 0 ? raw.rank : null,
            trigger: typeof raw.trigger === 'string' ? raw.trigger.trim() : '',
            kept: raw.kept === true,
            reason: typeof raw.reason === 'string' ? raw.reason.trim() : '',
        }];
    });
}

export function createWorldbookService(client, sessionWrite, endpoints) {
    if (!client || typeof client.get !== 'function' || typeof client.put !== 'function'
        || typeof client.delete !== 'function') {
        throw new TypeError('世界书服务需要 ApiClient');
    }
    if (typeof sessionWrite !== 'function') throw new TypeError('世界书服务需要 sessionWrite');
    if (!endpoints || !endpoints.worldbook || !endpoints.worldbookSchema || !endpoints.worldbookSave
        || !endpoints.worldbookDelete || !endpoints.worldbookManual) {
        throw new TypeError('世界书服务缺少 endpoints');
    }
    let cachedSchema = null;

    async function schema() {
        const body = await client.get(endpoints.worldbookSchema, {
            schema: value => Boolean(value && Array.isArray(value.groups)) || '世界书 Schema 响应无效',
        });
        cachedSchema = normalizeWorldbookSchema(body);
        return cachedSchema;
    }

    async function list(project) {
        const body = await client.get(`${endpoints.worldbook}?project=${encodeURIComponent(project)}`, {
            schema: value => Array.isArray(value && value.entries) || '世界书列表响应无效',
        });
        return normalizeWorldbookEntries(body.entries);
    }

    async function save(project, entryId, data) {
        const normalized = serializeWorldbookDraft(
            draftFromEntry(normalizeWorldbookEntry(data)),
            { schema: cachedSchema || DEFAULT_SCHEMA },
        );
        if (normalized.id !== entryId) throw new TypeError('保存 URL 与世界书 ID 不一致');
        return client.put(
            `${endpoints.worldbookSave(entryId)}?project=${encodeURIComponent(project)}`,
            { data: normalized },
            { schema: value => Boolean(value && value.saved === true && value.id === entryId) || '世界书保存响应无效' },
        );
    }

    async function remove(project, entryId) {
        return client.delete(
            `${endpoints.worldbookDelete(entryId)}?project=${encodeURIComponent(project)}`,
            { schema: value => Boolean(value && value.deleted === true) || '世界书删除响应无效' },
        );
    }

    async function saveManual(ref, entryIds) {
        if (!ref || typeof ref.project !== 'string' || typeof ref.save !== 'string') {
            throw new TypeError('manual 世界书保存缺少 SessionRef');
        }
        return sessionWrite(endpoints.worldbookManual, 'PATCH', {
            project: ref.project,
            save: ref.save,
            entry_ids: normalizeManualWorldbookIds(entryIds, { strict: true }),
        }, '保存当前存档的手动世界书');
    }

    return Object.freeze({ schema, list, save, remove, saveManual });
}

function createElement(documentRef, tag, className = '', text = '') {
    const element = documentRef.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
}

function setLiveMessage(element, message) {
    element.textContent = String(message || '');
    element.hidden = !element.textContent;
}

function errorText(error) {
    if (error instanceof Error && error.message) return error.message;
    if (typeof error === 'string' && error.trim()) return error.trim();
    return '操作失败，请重试';
}

function sameStringSet(left, right) {
    const a = [...left].sort();
    const b = [...right].sort();
    return a.length === b.length && a.every((value, index) => value === b[index]);
}

function activationLabel(activation) {
    if (activation === 'keywords') return '关键词';
    if (activation === 'manual') return '手动';
    return '常驻';
}

function diagnosticReason(record) {
    const parts = [];
    if (record.activation) parts.push(activationLabel(record.activation));
    if (record.matchedKeywords.length) parts.push(`命中：${record.matchedKeywords.join('、')}`);
    if (record.matchedSources.length) parts.push(`来源：${record.matchedSources.join('、')}`);
    parts.push(`优先级 ${record.priority}`);
    if (record.matchCount) parts.push(`命中数 ${record.matchCount}`);
    if (record.recency !== null) parts.push(`最近性 ${record.recency}`);
    if (record.rank !== null) parts.push(`排序 ${record.rank}`);
    if (record.trigger) parts.push(`触发 ${record.trigger}`);
    if (record.reason) parts.push(record.reason);
    return parts.join('；');
}

export function createWorldbookEditor(options) {
    const documentRef = options && options.documentRef;
    if (!documentRef || typeof documentRef.createElement !== 'function') {
        throw new TypeError('世界书编辑器缺少 document');
    }
    for (const callback of ['onSaveEntry', 'onDeleteEntry', 'onSaveManual']) {
        if (typeof options[callback] !== 'function') throw new TypeError(`世界书编辑器缺少 ${callback}`);
    }
    const schema = options.schema ? normalizeWorldbookSchema(options.schema) : defaultNormalizedSchema();
    const fieldSchema = key => schema.fields[key];

    let entries = normalizeWorldbookEntries(options.entries || []);
    let selectedId = entries.length ? entries[0].id : null;
    let newDraft = draftFromEntry(null);
    const drafts = new Map(entries.map(entry => [entry.id, draftFromEntry(entry)]));
    // 保留当前 Session 的完整选择集合。条目删除、禁用或改模式后 ID 会休眠，
    // 不能因编辑其他条目而被前端静默清除。
    let manualBaseline = new Set(normalizeManualWorldbookIds(options.manualIds));
    let manualDraft = new Set(manualBaseline);
    let entryPending = false;
    let manualPending = false;
    let writeDisabled = Boolean(options.disabled);
    let currentFormRefs = null;
    let customCounter = 0;

    const root = createElement(documentRef, 'section', 'worldbook-editor');
    root.setAttribute('aria-label', '世界书编辑器');
    const intro = createElement(
        documentRef,
        'p',
        'worldbook-intro',
        '条目配置对整个项目生效；“本档”选择只属于当前存档。实际注入仍受总 Prompt 预算约束。',
    );
    root.appendChild(intro);

    const diagnostics = sanitizeWorldbookDiagnostics(options.diagnostics);
    const diagnosticsSection = createElement(documentRef, 'section', 'worldbook-diagnostics');
    const diagnosticsTitle = createElement(documentRef, 'h3', 'worldbook-section-title', '上轮世界书命中');
    const diagnosticsId = `worldbook-diagnostics-${Date.now()}`;
    diagnosticsTitle.id = diagnosticsId;
    diagnosticsSection.setAttribute('aria-labelledby', diagnosticsId);
    diagnosticsSection.appendChild(diagnosticsTitle);
    if (diagnostics.length === 0) {
        diagnosticsSection.appendChild(createElement(
            documentRef, 'p', 'worldbook-diagnostics-empty', '当前没有可显示的上轮命中记录。',
        ));
    } else {
        const activated = diagnostics.filter(item => item.activated).length;
        const kept = diagnostics.filter(item => item.kept).length;
        diagnosticsSection.appendChild(createElement(
            documentRef,
            'p',
            'worldbook-diagnostics-summary',
            `检查 ${diagnostics.length} 条，触发 ${activated} 条，注入 ${kept} 条。诊断不包含世界书正文。`,
        ));
        const list = createElement(documentRef, 'ul', 'worldbook-diagnostics-list');
        for (const record of diagnostics) {
            const item = createElement(documentRef, 'li', 'worldbook-diagnostic-item');
            const diagnosticState = record.kept ? 'kept' : record.activated ? 'triggered' : 'excluded';
            const state = createElement(
                documentRef,
                'span',
                `worldbook-diagnostic-state ${diagnosticState}`,
                record.kept ? '已注入' : record.activated ? '已触发' : '未触发',
            );
            const id = createElement(documentRef, 'strong', 'worldbook-diagnostic-id', record.id);
            const reason = createElement(documentRef, 'span', 'worldbook-diagnostic-reason', diagnosticReason(record));
            item.appendChild(state);
            item.appendChild(id);
            item.appendChild(reason);
            list.appendChild(item);
        }
        diagnosticsSection.appendChild(list);
    }
    root.appendChild(diagnosticsSection);

    const layout = createElement(documentRef, 'div', 'worldbook-layout');
    const sidebar = createElement(documentRef, 'aside', 'worldbook-sidebar');
    sidebar.setAttribute('aria-label', '世界书条目列表');
    const sidebarTitle = createElement(documentRef, 'h3', 'worldbook-section-title', '项目共享条目');
    const newButton = createElement(documentRef, 'button', 'modal-btn worldbook-new-button', '＋ 新建条目');
    newButton.type = 'button';
    const listRoot = createElement(documentRef, 'div', 'worldbook-entry-list');
    listRoot.setAttribute('role', 'list');
    const manualActions = createElement(documentRef, 'div', 'worldbook-manual-actions');
    const manualSaveButton = createElement(
        documentRef, 'button', 'modal-btn primary worldbook-manual-save worldbook-write-control', '保存本存档选择',
    );
    manualSaveButton.type = 'button';
    const manualStatus = createElement(documentRef, 'p', 'worldbook-manual-status');
    manualStatus.setAttribute('role', 'status');
    manualStatus.setAttribute('aria-live', 'polite');
    manualStatus.hidden = true;
    const manualError = createElement(documentRef, 'p', 'worldbook-inline-error');
    manualError.setAttribute('role', 'alert');
    manualError.hidden = true;
    manualActions.appendChild(manualSaveButton);
    manualActions.appendChild(manualStatus);
    manualActions.appendChild(manualError);
    sidebar.appendChild(sidebarTitle);
    sidebar.appendChild(newButton);
    sidebar.appendChild(listRoot);
    sidebar.appendChild(manualActions);

    const formRoot = createElement(documentRef, 'div', 'worldbook-form-panel');
    layout.appendChild(sidebar);
    layout.appendChild(formRoot);
    root.appendChild(layout);

    function currentDraft() {
        return selectedId === null ? newDraft : drafts.get(selectedId);
    }

    function manualDirty() {
        return !sameStringSet(manualBaseline, manualDraft);
    }

    function setPendingState() {
        if (typeof options.onPendingChange === 'function') {
            options.onPendingChange(entryPending || manualPending);
        }
        const navigationDisabled = entryPending || manualPending;
        for (const button of root.querySelectorAll('.worldbook-entry-select,.worldbook-new-button')) {
            button.disabled = navigationDisabled;
        }
        if (currentFormRefs) {
            for (const control of currentFormRefs.controls) control.disabled = entryPending;
            currentFormRefs.idInput.readOnly = selectedId !== null;
            currentFormRefs.saveButton.disabled = writeDisabled || entryPending || manualPending;
            if (currentFormRefs.deleteButton) {
                currentFormRefs.deleteButton.disabled = writeDisabled || entryPending || manualPending;
            }
        }
        for (const checkbox of root.querySelectorAll('.worldbook-manual-checkbox')) {
            checkbox.disabled = writeDisabled || entryPending || manualPending;
        }
        manualSaveButton.disabled = writeDisabled || entryPending || manualPending || !manualDirty();
        manualSaveButton.setAttribute('aria-busy', String(manualPending));
        manualSaveButton.textContent = manualPending ? '保存中…' : '保存本存档选择';
    }

    function renderList(focusId = undefined) {
        listRoot.replaceChildren();
        newButton.classList.toggle('active', selectedId === null);
        newButton.setAttribute('aria-current', selectedId === null ? 'true' : 'false');
        for (const entry of entries) {
            const item = createElement(documentRef, 'div', 'worldbook-entry-row');
            item.setAttribute('role', 'listitem');
            const selectButton = createElement(documentRef, 'button', 'worldbook-entry-select');
            selectButton.type = 'button';
            selectButton.dataset.entryId = entry.id;
            const selected = selectedId === entry.id;
            selectButton.classList.toggle('active', selected);
            selectButton.setAttribute('aria-current', selected ? 'true' : 'false');
            selectButton.setAttribute(
                'aria-label',
                `编辑世界书条目 ${entry.title ? `${entry.title}，` : ''}${entry.id}`,
            );
            if (entry.title) {
                selectButton.appendChild(createElement(
                    documentRef, 'span', 'worldbook-entry-title', entry.title,
                ));
            }
            const name = createElement(documentRef, 'span', 'worldbook-entry-id', entry.id);
            const badges = createElement(documentRef, 'span', 'worldbook-entry-badges');
            badges.appendChild(createElement(
                documentRef,
                'span',
                `worldbook-badge ${entry.enabled ? '' : 'disabled'}`,
                entry.enabled ? activationLabel(entry.activation) : '全局关闭',
            ));
            if (entry.activation === 'keywords') {
                badges.appendChild(createElement(
                    documentRef, 'span', 'worldbook-badge', `关键词 ${entry.keywords.length}`,
                ));
            }
            badges.appendChild(createElement(
                documentRef, 'span', 'worldbook-badge', `优先级 ${entry.priority}`,
            ));
            selectButton.appendChild(name);
            selectButton.appendChild(badges);
            selectButton.addEventListener('click', () => {
                selectedId = entry.id;
                renderList(entry.id);
                renderForm();
            });
            item.appendChild(selectButton);

            if (entry.enabled && entry.activation === 'manual') {
                const label = createElement(documentRef, 'label', 'worldbook-manual-toggle');
                const checkbox = createElement(documentRef, 'input', 'worldbook-manual-checkbox worldbook-write-control');
                checkbox.type = 'checkbox';
                checkbox.checked = manualDraft.has(entry.id);
                checkbox.setAttribute('aria-label', `当前存档${checkbox.checked ? '取消选择' : '选择'}世界书 ${entry.id}`);
                const text = createElement(
                    documentRef,
                    'span',
                    'worldbook-manual-label',
                    checkbox.checked ? '本档已选' : '本档未选',
                );
                checkbox.addEventListener('change', () => {
                    if (checkbox.checked) manualDraft.add(entry.id);
                    else manualDraft.delete(entry.id);
                    text.textContent = checkbox.checked ? '本档已选' : '本档未选';
                    checkbox.setAttribute(
                        'aria-label',
                        `当前存档${checkbox.checked ? '取消选择' : '选择'}世界书 ${entry.id}`,
                    );
                    setLiveMessage(manualStatus, manualDirty() ? '本存档选择尚未保存。' : '');
                    setLiveMessage(manualError, '');
                    setPendingState();
                });
                label.appendChild(checkbox);
                label.appendChild(text);
                item.appendChild(label);
            }
            listRoot.appendChild(item);
        }
        setPendingState();
        if (focusId !== undefined) {
            const selector = focusId === null
                ? null
                : [...listRoot.querySelectorAll('.worldbook-entry-select')]
                    .find(button => button.dataset.entryId === focusId);
            const target = focusId === null ? newButton : selector;
            if (target && typeof target.focus === 'function') target.focus();
        }
    }

    function appendLabeledControl(form, config) {
        const row = createElement(documentRef, 'div', `worldbook-field worldbook-field-${config.field}`);
        const id = `worldbook-${config.field}-${++customCounter}`;
        const label = createElement(documentRef, 'label', 'worldbook-field-label', config.label);
        label.htmlFor = id;
        const control = createElement(documentRef, config.tag || 'input', 'worldbook-control');
        control.id = id;
        control.dataset.field = config.field;
        if (config.type) control.type = config.type;
        if (config.rows) control.rows = config.rows;
        if (config.step) control.step = config.step;
        if (config.min !== undefined) control.min = config.min;
        if (config.max !== undefined) control.max = config.max;
        if (config.placeholder) control.placeholder = config.placeholder;
        if (config.readOnly) control.readOnly = true;
        if (config.value !== undefined) control.value = config.value;
        if (config.checked !== undefined) control.checked = config.checked;
        row.appendChild(label);
        row.appendChild(control);
        if (config.hint) {
            const hint = createElement(documentRef, 'p', 'worldbook-field-hint', config.hint);
            hint.id = `${id}-hint`;
            control.setAttribute('aria-describedby', hint.id);
            row.appendChild(hint);
        }
        form.appendChild(row);
        return { row, control };
    }

    function renderCustomRows(container, draft) {
        container.replaceChildren();
        draft.customRows.forEach((record, index) => {
            const row = createElement(documentRef, 'div', 'worldbook-custom-row');
            const keyId = `worldbook-custom-key-${++customCounter}`;
            const keyLabel = createElement(documentRef, 'label', 'worldbook-field-label', '字段名');
            keyLabel.htmlFor = keyId;
            const keyInput = createElement(documentRef, 'input', 'worldbook-control worldbook-custom-key');
            keyInput.type = 'text';
            keyInput.id = keyId;
            keyInput.value = record.key;
            keyInput.addEventListener('input', () => {
                record.key = keyInput.value;
                record.dirty = true;
            });
            const valueId = `worldbook-custom-value-${++customCounter}`;
            const valueLabel = createElement(documentRef, 'label', 'worldbook-field-label', '值');
            valueLabel.htmlFor = valueId;
            const valueInput = createElement(documentRef, 'input', 'worldbook-control worldbook-custom-value');
            valueInput.type = 'text';
            valueInput.id = valueId;
            valueInput.value = record.value;
            valueInput.addEventListener('input', () => {
                record.value = valueInput.value;
                record.dirty = true;
            });
            const removeButton = createElement(documentRef, 'button', 'worldbook-custom-remove', '删除字段');
            removeButton.type = 'button';
            removeButton.setAttribute('aria-label', `删除自定义字段 ${record.key || index + 1}`);
            removeButton.addEventListener('click', () => {
                draft.customRows.splice(index, 1);
                renderCustomRows(container, draft);
            });
            const keyCell = createElement(documentRef, 'div', 'worldbook-custom-cell');
            keyCell.appendChild(keyLabel);
            keyCell.appendChild(keyInput);
            const valueCell = createElement(documentRef, 'div', 'worldbook-custom-cell');
            valueCell.appendChild(valueLabel);
            valueCell.appendChild(valueInput);
            row.appendChild(keyCell);
            row.appendChild(valueCell);
            row.appendChild(removeButton);
            container.appendChild(row);
        });
    }

    function showDraftError(draft, fieldMap, fallbackButton) {
        if (!currentFormRefs) return;
        setLiveMessage(currentFormRefs.error, draft.error);
        for (const control of fieldMap.values()) control.removeAttribute('aria-invalid');
        const target = draft.errorField ? fieldMap.get(draft.errorField) : fallbackButton;
        if (draft.errorField && target) target.setAttribute('aria-invalid', 'true');
        if (draft.error && target && typeof target.focus === 'function') target.focus();
    }

    function replaceEntries(nextEntries, { savedId = null, deletedId = null } = {}) {
        entries = normalizeWorldbookEntries(nextEntries);
        const ids = new Set(entries.map(entry => entry.id));
        for (const key of [...drafts.keys()]) {
            if (!ids.has(key)) drafts.delete(key);
        }
        for (const entry of entries) {
            if (!drafts.has(entry.id) || entry.id === savedId) drafts.set(entry.id, draftFromEntry(entry));
        }
        if (savedId && ids.has(savedId)) selectedId = savedId;
        else if (!ids.has(selectedId)) selectedId = entries.length ? entries[0].id : null;
        renderList();
        renderForm();
    }

    async function saveEntry() {
        if (writeDisabled || entryPending || manualPending) return false;
        const draft = currentDraft();
        draft.error = '';
        draft.errorField = '';
        draft.status = '';
        let data;
        try {
            data = serializeWorldbookDraft(draft, { schema: options.schema || DEFAULT_SCHEMA });
        } catch (error) {
            draft.error = errorText(error);
            draft.errorField = error instanceof WorldbookValidationError ? error.field : '';
            showDraftError(draft, currentFormRefs.fieldMap, currentFormRefs.saveButton);
            return false;
        }
        entryPending = true;
        currentFormRefs.saveButton.textContent = '保存中…';
        setPendingState();
        try {
            const fresh = await options.onSaveEntry(data, {
                isNew: selectedId === null,
                originalId: draft.originalId,
            });
            replaceEntries(fresh, { savedId: data.id });
            const savedDraft = drafts.get(data.id);
            savedDraft.status = '✓ 项目共享条目已保存';
            renderForm();
            return true;
        } catch (error) {
            draft.error = errorText(error);
            draft.errorField = '';
            showDraftError(draft, currentFormRefs.fieldMap, currentFormRefs.saveButton);
            return false;
        } finally {
            entryPending = false;
            if (currentFormRefs) currentFormRefs.saveButton.textContent = '保存条目';
            setPendingState();
        }
    }

    async function deleteEntry() {
        if (selectedId === null || writeDisabled || entryPending || manualPending) return false;
        const entryId = selectedId;
        const accepted = typeof options.confirmDelete === 'function'
            ? options.confirmDelete(entryId)
            : true;
        if (!accepted) return false;
        const draft = currentDraft();
        draft.error = '';
        entryPending = true;
        setPendingState();
        try {
            const fresh = await options.onDeleteEntry(entryId);
            replaceEntries(fresh, { deletedId: entryId });
            return true;
        } catch (error) {
            draft.error = errorText(error);
            draft.errorField = '';
            showDraftError(draft, currentFormRefs.fieldMap, currentFormRefs.deleteButton);
            return false;
        } finally {
            entryPending = false;
            setPendingState();
        }
    }

    async function commitManualSelection() {
        if (writeDisabled || entryPending || manualPending || !manualDirty()) return false;
        const focusTarget = documentRef.activeElement;
        setLiveMessage(manualError, '');
        setLiveMessage(manualStatus, '保存中…');
        manualPending = true;
        setPendingState();
        try {
            await options.onSaveManual([...manualDraft].sort());
            manualBaseline = new Set(manualDraft);
            setLiveMessage(manualStatus, '✓ 当前存档的手动选择已保存');
            return true;
        } catch (error) {
            setLiveMessage(manualStatus, '');
            setLiveMessage(manualError, errorText(error));
            if (focusTarget && typeof focusTarget.focus === 'function') focusTarget.focus();
            return false;
        } finally {
            manualPending = false;
            setPendingState();
        }
    }

    function renderForm() {
        formRoot.replaceChildren();
        const draft = currentDraft();
        const form = createElement(documentRef, 'form', 'worldbook-form');
        form.noValidate = true;
        const title = createElement(
            documentRef,
            'h3',
            'worldbook-form-title',
            selectedId === null ? '新建项目共享条目' : `编辑：${selectedId}`,
        );
        form.appendChild(title);

        const error = createElement(documentRef, 'p', 'worldbook-inline-error');
        error.setAttribute('role', 'alert');
        error.hidden = !draft.error;
        error.textContent = draft.error;
        const status = createElement(documentRef, 'p', 'worldbook-entry-status');
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        status.hidden = !draft.status;
        status.textContent = draft.status;
        form.appendChild(error);
        form.appendChild(status);

        const idConfig = fieldSchema('id');
        const idField = appendLabeledControl(form, {
            field: 'id', label: idConfig.label, value: draft.id, readOnly: selectedId !== null,
            hint: selectedId === null
                ? '新条目保存后 ID 不可直接修改。'
                : '已有条目的 ID 已锁定，防止产生重复条目或悬空的存档选择。',
        });
        idField.control.required = true;
        idField.control.addEventListener('input', () => { draft.id = idField.control.value; });

        const titleConfig = fieldSchema('title');
        const titleField = appendLabeledControl(form, {
            field: 'title', label: titleConfig.label, value: draft.title,
            hint: titleConfig.hint || '用于列表辨识；稳定 ID 仍是服务端关联依据。',
        });
        titleField.control.addEventListener('input', () => { draft.title = titleField.control.value; });

        const enabledConfig = fieldSchema('enabled');
        const enabledRow = createElement(documentRef, 'div', 'worldbook-field worldbook-checkbox-field');
        const enabledLabel = createElement(documentRef, 'label', 'worldbook-checkbox-label');
        const enabledInput = createElement(documentRef, 'input', 'worldbook-control worldbook-enabled');
        enabledInput.type = 'checkbox';
        enabledInput.checked = draft.enabled;
        const enabledText = createElement(documentRef, 'span', '', enabledConfig.label);
        const enabledHint = createElement(
            documentRef, 'span', 'worldbook-field-hint', '关闭后所有存档都不会注入此条目。',
        );
        enabledInput.addEventListener('change', () => { draft.enabled = enabledInput.checked; });
        enabledLabel.appendChild(enabledInput);
        enabledLabel.appendChild(enabledText);
        enabledLabel.appendChild(enabledHint);
        enabledRow.appendChild(enabledLabel);
        form.appendChild(enabledRow);

        const activationConfig = fieldSchema('activation');
        const activationField = appendLabeledControl(form, {
            field: 'activation', label: activationConfig.label, tag: 'select',
            hint: activationConfig.hint
                || '常驻每轮成为候选；关键词命中明确上下文时成为候选；手动仅在当前存档选中时成为候选。',
        });
        for (const config of activationConfig.options) {
            const option = createElement(documentRef, 'option', '', config.label);
            option.value = config.value;
            activationField.control.appendChild(option);
        }
        activationField.control.value = draft.activation;

        const keywordsConfig = fieldSchema('keywords');
        const keywordsField = appendLabeledControl(form, {
            field: 'keywords', label: keywordsConfig.label, tag: 'textarea', rows: keywordsConfig.rows || 4,
            value: draft.keywordsText,
            hint: `${keywordsConfig.hint ? `${keywordsConfig.hint}；` : ''}空行和重复项在保存时移除；切换触发方式不会清空草稿。`,
        });
        keywordsField.row.classList.add('worldbook-keywords-field');
        keywordsField.control.addEventListener('input', () => { draft.keywordsText = keywordsField.control.value; });

        const manualHint = createElement(
            documentRef,
            'p',
            'worldbook-manual-entry-hint',
            '保存为 manual 后，可在左侧用“本档已选/未选”单独控制当前存档。',
        );
        form.appendChild(manualHint);

        function syncActivationFields() {
            draft.activation = activationField.control.value;
            keywordsField.row.hidden = draft.activation !== 'keywords';
            keywordsField.row.setAttribute('aria-hidden', String(keywordsField.row.hidden));
            manualHint.hidden = draft.activation !== 'manual';
        }
        activationField.control.addEventListener('change', syncActivationFields);
        syncActivationFields();

        const priorityConfig = fieldSchema('priority');
        const priorityField = appendLabeledControl(form, {
            field: 'priority', label: priorityConfig.label, type: 'number', step: '1', value: draft.priority,
            min: priorityConfig.min, max: priorityConfig.max,
            hint: '整数越大越先进入候选排序；是否最终注入仍由命中与总预算决定。',
        });
        priorityField.control.addEventListener('input', () => { draft.priority = priorityField.control.value; });

        const contentConfig = fieldSchema('content');
        const contentField = appendLabeledControl(form, {
            field: 'content', label: contentConfig.label, tag: 'textarea', rows: contentConfig.rows || 8, value: draft.content,
            hint: '正文只进入模型上下文，不会写入前端命中诊断。',
        });
        contentField.row.classList.add('worldbook-content-field');
        contentField.control.addEventListener('input', () => { draft.content = contentField.control.value; });

        const customFieldset = createElement(documentRef, 'fieldset', 'worldbook-custom-fieldset');
        const customLegend = createElement(
            documentRef, 'legend', 'worldbook-section-title', schema.customGroup.label,
        );
        const customRows = createElement(documentRef, 'div', 'worldbook-custom-rows');
        renderCustomRows(customRows, draft);
        const addCustomButton = createElement(
            documentRef, 'button', 'modal-btn worldbook-custom-add', '＋ 添加自定义字段',
        );
        addCustomButton.type = 'button';
        addCustomButton.addEventListener('click', () => {
            draft.customRows.push({ key: '', value: '', originalKey: null, originalValue: '', dirty: true });
            renderCustomRows(customRows, draft);
            const inputs = customRows.querySelectorAll('.worldbook-custom-key');
            const target = inputs[inputs.length - 1];
            if (target && typeof target.focus === 'function') target.focus();
        });
        customFieldset.appendChild(customLegend);
        customFieldset.appendChild(customRows);
        customFieldset.appendChild(addCustomButton);
        form.appendChild(customFieldset);

        const actions = createElement(documentRef, 'div', 'worldbook-entry-actions');
        let deleteButton = null;
        if (selectedId !== null) {
            deleteButton = createElement(
                documentRef, 'button', 'modal-btn danger worldbook-entry-delete worldbook-write-control', '删除条目',
            );
            deleteButton.type = 'button';
            deleteButton.addEventListener('click', () => { void deleteEntry(); });
            actions.appendChild(deleteButton);
        }
        const saveButton = createElement(
            documentRef, 'button', 'modal-btn primary worldbook-entry-save worldbook-write-control', '保存条目',
        );
        saveButton.type = 'submit';
        actions.appendChild(saveButton);
        form.appendChild(actions);
        form.addEventListener('submit', event => {
            event.preventDefault();
            void saveEntry();
        });

        const fieldMap = new Map([
            ['id', idField.control],
            ['title', titleField.control],
            ['enabled', enabledInput],
            ['activation', activationField.control],
            ['keywords', keywordsField.control],
            ['priority', priorityField.control],
            ['content', contentField.control],
            ['custom', addCustomButton],
        ]);
        currentFormRefs = {
            form,
            error,
            status,
            idInput: idField.control,
            saveButton,
            deleteButton,
            fieldMap,
            controls: [
                idField.control,
                titleField.control,
                enabledInput,
                activationField.control,
                keywordsField.control,
                priorityField.control,
                contentField.control,
                addCustomButton,
            ],
        };
        formRoot.appendChild(form);
        if (draft.errorField) {
            const target = fieldMap.get(draft.errorField);
            if (target) target.setAttribute('aria-invalid', 'true');
        }
        setPendingState();
    }

    newButton.addEventListener('click', () => {
        selectedId = null;
        renderList(null);
        renderForm();
    });
    manualSaveButton.addEventListener('click', () => { void commitManualSelection(); });

    renderList();
    renderForm();

    return Object.freeze({
        root,
        setDisabled(value) {
            writeDisabled = Boolean(value);
            setPendingState();
        },
        state() {
            return Object.freeze({
                selectedId,
                manualIds: Object.freeze([...manualDraft].sort()),
                manualDirty: manualDirty(),
                entryPending,
                manualPending,
            });
        },
    });
}

export { ACTIVATIONS as WORLD_ACTIVATIONS };
