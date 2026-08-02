const ACTIVATIONS = Object.freeze(['always', 'keywords', 'manual', 'scene']);
const ACTIVATION_SET = new Set(ACTIVATIONS);
const CATEGORIES = Object.freeze(['general', 'location', 'faction', 'rule', 'history', 'culture', 'item', 'secret']);
const VISIBILITIES = Object.freeze(['public', 'discovered', 'hidden']);
const KNOWLEDGE_SCOPES = Object.freeze(['global', 'narrator', 'characters']);
const CATEGORY_LABELS = Object.freeze({
    general: '通用', location: '地点', faction: '势力', rule: '规则', history: '历史',
    culture: '文化', item: '物品', secret: '秘密',
});
const VISIBILITY_LABELS = Object.freeze({ public: '玩家可以知道', discovered: '发现后玩家才知道', hidden: '不向玩家展示' });
const KNOWLEDGE_LABELS = Object.freeze({ global: '所有角色都知道', narrator: '只有叙述者知道', characters: '只有指定角色知道' });
const ACTIVATION_LABELS = Object.freeze({
    always: '每轮都参考',
    keywords: '提到它时参考',
    manual: '只在我启用时参考',
    scene: '相关场景出现时参考',
});
const CANONICAL_ENTRY_FIELDS = new Set([
    'id', 'title', 'summary', 'category', 'enabled', 'activation', 'keywords', 'priority',
    'visibility', 'knowledge_scope', 'known_by_character_ids', 'linked_character_ids',
    'linked_entry_ids', 'location_aliases', 'content', 'custom',
    'knowledgeScope', 'knownByCharacterIds', 'linkedCharacterIds', 'linkedEntryIds',
    'locationAliases',
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

function normalizeStringArray(value, label, { strict = false } = {}) {
    if (value === undefined || value === null) return [];
    if (!Array.isArray(value)) {
        if (strict) throw new TypeError(`${label}必须是字符串数组`);
        return [];
    }
    const result = [];
    const seen = new Set();
    for (const item of value) {
        if (typeof item !== 'string') {
            if (strict) throw new TypeError(`${label}必须全部是字符串`);
            continue;
        }
        const text = item.trim();
        const canonical = text.normalize('NFKC').toLocaleLowerCase();
        if (!text || seen.has(canonical)) continue;
        seen.add(canonical);
        result.push(text);
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
        { key: 'summary', label: '一句话摘要', type: 'text' },
        { key: 'category', label: '设定分类', type: 'select', options: CATEGORIES.map(value => ({ value, label: value })) },
        { key: 'enabled', label: '启用此条目', type: 'checkbox' },
        { key: 'activation', label: '触发方式', type: 'select', options: [
            { value: 'always', label: '常驻（always）' },
            { value: 'keywords', label: '关键词（keywords）' },
            { value: 'manual', label: '手动（manual）' },
            { value: 'scene', label: '场景联动（scene）' },
        ] },
        { key: 'keywords', label: '触发关键词（每行一个）', type: 'textarea', rows: 4, maxItems: 64 },
        { key: 'priority', label: '优先级', type: 'number', min: -1_000_000, max: 1_000_000 },
        { key: 'visibility', label: '玩家可见性', type: 'select', options: VISIBILITIES.map(value => ({ value, label: value })) },
        { key: 'knowledge_scope', label: '知识边界', type: 'select', options: KNOWLEDGE_SCOPES.map(value => ({ value, label: value })) },
        { key: 'known_by_character_ids', label: '知情角色 ID', type: 'textarea', rows: 3, array: true },
        { key: 'linked_character_ids', label: '关联角色 ID', type: 'textarea', rows: 3, array: true },
        { key: 'linked_entry_ids', label: '关联世界条目 ID', type: 'textarea', rows: 3, array: true },
        { key: 'location_aliases', label: '地点别名', type: 'textarea', rows: 3, array: true },
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
    if (value.summary !== undefined && typeof value.summary !== 'string') {
        throw new TypeError(`世界书「${id}」的 summary 必须是字符串`);
    }
    const category = value.category === undefined ? 'general' : requireString(value.category, '设定分类', { allowEmpty: false });
    const visibility = value.visibility === undefined ? 'public' : requireString(value.visibility, '玩家可见性', { allowEmpty: false });
    const knowledgeScopeValue = value.knowledge_scope ?? value.knowledgeScope;
    const knowledgeScope = knowledgeScopeValue === undefined
        ? 'global'
        : requireString(knowledgeScopeValue, '知识边界', { allowEmpty: false });
    if (!CATEGORIES.includes(category)) throw new TypeError(`世界书「${id}」的 category 无效`);
    if (!VISIBILITIES.includes(visibility)) throw new TypeError(`世界书「${id}」的 visibility 无效`);
    if (!KNOWLEDGE_SCOPES.includes(knowledgeScope)) throw new TypeError(`世界书「${id}」的 knowledge_scope 无效`);
    const normalized = {
        id,
        enabled: value.enabled !== false,
        activation,
        keywords: Object.freeze(normalizeKeywords(value.keywords, { strict: true })),
        priority: priorityValue(value.priority, { strict: true }),
        title: value.title || '',
        summary: value.summary || '',
        category,
        visibility,
        knowledgeScope,
        knownByCharacterIds: Object.freeze(normalizeStringArray(
            value.known_by_character_ids ?? value.knownByCharacterIds,
            '知情角色 ID',
            { strict: true },
        )),
        linkedCharacterIds: Object.freeze(normalizeStringArray(
            value.linked_character_ids ?? value.linkedCharacterIds,
            '关联角色 ID',
            { strict: true },
        )),
        linkedEntryIds: Object.freeze(normalizeStringArray(
            value.linked_entry_ids ?? value.linkedEntryIds,
            '关联世界条目 ID',
            { strict: true },
        )),
        locationAliases: Object.freeze(normalizeStringArray(
            value.location_aliases ?? value.locationAliases,
            '地点别名',
            { strict: true },
        )),
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
        activation: entry ? entry.activation : 'keywords',
        keywordsText: entry ? entry.keywords.join('\n') : '',
        priority: entry ? String(entry.priority) : '0',
        title: entry ? entry.title : '',
        summary: entry ? entry.summary : '',
        category: entry ? entry.category : 'general',
        visibility: entry ? entry.visibility : 'public',
        knowledgeScope: entry ? entry.knowledgeScope : 'global',
        knownByCharacterIdsText: entry ? entry.knownByCharacterIds.join('\n') : '',
        linkedCharacterIdsText: entry ? entry.linkedCharacterIds.join('\n') : '',
        linkedEntryIdsText: entry ? entry.linkedEntryIds.join('\n') : '',
        locationAliasesText: entry ? entry.locationAliases.join('\n') : '',
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
        throw new WorldbookValidationError('触发方式必须是 always、keywords、manual 或 scene', 'activation');
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
    if (typeof draft.summary !== 'string') throw new WorldbookValidationError('摘要必须是字符串', 'summary');
    const category = String(draft.category || 'general');
    const visibility = String(draft.visibility || 'public');
    const knowledgeScope = String(draft.knowledgeScope || 'global');
    if (!CATEGORIES.includes(category)) throw new WorldbookValidationError('设定分类无效', 'category');
    if (!VISIBILITIES.includes(visibility)) throw new WorldbookValidationError('玩家可见性无效', 'visibility');
    if (!KNOWLEDGE_SCOPES.includes(knowledgeScope)) throw new WorldbookValidationError('知识边界无效', 'knowledge_scope');
    const keywords = normalizeKeywords(String(draft.keywordsText || '').split(/\r?\n/), { strict: true });
    const knownByCharacterIds = normalizeStringArray(String(draft.knownByCharacterIdsText || '').split(/\r?\n/), '知情角色 ID', { strict: true });
    const linkedCharacterIds = normalizeStringArray(String(draft.linkedCharacterIdsText || '').split(/\r?\n/), '关联角色 ID', { strict: true });
    const linkedEntryIds = normalizeStringArray(String(draft.linkedEntryIdsText || '').split(/\r?\n/), '关联世界条目 ID', { strict: true });
    const locationAliases = normalizeStringArray(String(draft.locationAliasesText || '').split(/\r?\n/), '地点别名', { strict: true });
    const keywordField = schema.fields.keywords;
    if (activation === 'keywords' && keywords.length === 0) {
        throw new WorldbookValidationError('关键词触发方式至少需要一个关键词', 'keywords');
    }
    if (activation === 'scene' && linkedCharacterIds.length === 0 && locationAliases.length === 0) {
        throw new WorldbookValidationError('场景联动至少需要地点别名或关联角色', 'location_aliases');
    }
    if (knowledgeScope === 'characters' && knownByCharacterIds.length === 0) {
        throw new WorldbookValidationError('指定角色知识边界至少需要一个知情角色 ID', 'known_by_character_ids');
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
        summary: draft.summary,
        category,
        visibility,
        knowledge_scope: knowledgeScope,
        known_by_character_ids: knownByCharacterIds,
        linked_character_ids: linkedCharacterIds,
        linked_entry_ids: linkedEntryIds,
        location_aliases: locationAliases,
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
            title: typeof raw.title === 'string' ? raw.title.trim() : '',
            category: CATEGORIES.includes(raw.category) ? raw.category : 'general',
            summary: typeof raw.summary === 'string' ? raw.summary.trim() : '',
            visibility: VISIBILITIES.includes(raw.visibility) ? raw.visibility : 'public',
            knowledgeScope: KNOWLEDGE_SCOPES.includes(raw.knowledge_scope) ? raw.knowledge_scope : 'global',
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
    return ACTIVATION_LABELS[activation] || ACTIVATION_LABELS.always;
}

function diagnosticReason(record) {
    const parts = [];
    if (record.matchedKeywords.length) parts.push(`对话提到了“${record.matchedKeywords.join('、')}”`);
    if (record.matchedSources.length) parts.push(`关联内容：${record.matchedSources.join('、')}`);
    if (!parts.length && record.activation) parts.push(activationLabel(record.activation));
    return parts.join('；') || '本轮没有符合使用条件';
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
    const requestedSelectedId = typeof options.selectedId === 'string' ? options.selectedId : '';
    let selectedId = entries.some(entry => entry.id === requestedSelectedId)
        ? requestedSelectedId
        : (entries.length ? entries[0].id : null);
    let newDraft = draftFromEntry(null);
    const drafts = new Map(entries.map(entry => [entry.id, draftFromEntry(entry)]));
    const characterChoices = Array.isArray(options.characters) ? options.characters.flatMap(item => {
        if (!isObject(item) || typeof item.id !== 'string' || !item.id.trim()) return [];
        const id = item.id.trim();
        const name = typeof item.name === 'string' && item.name.trim() ? item.name.trim() : id;
        return [{ id, label: name }];
    }) : [];
    let listQuery = '';
    let categoryFilter = 'all';

    function draftFingerprint(draft) {
        return JSON.stringify({
            id: draft.id,
            enabled: draft.enabled,
            activation: draft.activation,
            keywordsText: draft.keywordsText,
            priority: draft.priority,
            title: draft.title,
            summary: draft.summary,
            category: draft.category,
            visibility: draft.visibility,
            knowledgeScope: draft.knowledgeScope,
            knownByCharacterIdsText: draft.knownByCharacterIdsText,
            linkedCharacterIdsText: draft.linkedCharacterIdsText,
            linkedEntryIdsText: draft.linkedEntryIdsText,
            locationAliasesText: draft.locationAliasesText,
            content: draft.content,
            customRows: draft.customRows.map(row => ({ key: row.key, value: row.value })),
        });
    }

    const draftBaselines = new Map([...drafts].map(([id, draft]) => [id, draftFingerprint(draft)]));
    let newDraftBaseline = draftFingerprint(newDraft);
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
    root.setAttribute('aria-label', '世界设定编辑器');
    const intro = createElement(
        documentRef,
        'p',
        'worldbook-intro',
        '世界设定在整个项目中共用。选择“只在我启用时参考”的设定，可以为每个存档分别开启。',
    );
    root.appendChild(intro);

    const diagnostics = sanitizeWorldbookDiagnostics(options.diagnostics);
    const diagnosticsSection = createElement(documentRef, 'details', 'worldbook-diagnostics worldbook-inspector');
    const diagnosticsTitle = createElement(documentRef, 'summary', 'worldbook-section-title', '使用情况');
    const diagnosticsId = `worldbook-diagnostics-${Date.now()}`;
    diagnosticsTitle.id = diagnosticsId;
    diagnosticsSection.setAttribute('aria-labelledby', diagnosticsId);
    diagnosticsSection.appendChild(diagnosticsTitle);
    const selectionInspector = createElement(documentRef, 'dl', 'worldbook-selection-inspector');
    const inspectorRows = Object.freeze({
        scope: createElement(documentRef, 'dd'),
        activation: createElement(documentRef, 'dd'),
        knowledge: createElement(documentRef, 'dd'),
        links: createElement(documentRef, 'dd'),
    });
    for (const [key, label] of [['scope', '保存位置'], ['activation', '参考时机'], ['knowledge', '故事人物中谁知道'], ['links', '关联内容']]) {
        selectionInspector.append(createElement(documentRef, 'dt', '', label), inspectorRows[key]);
    }
    diagnosticsSection.appendChild(selectionInspector);
    diagnosticsSection.appendChild(createElement(documentRef, 'h4', 'worldbook-inspector-subtitle', '最近一轮使用记录'));
    if (diagnostics.length === 0) {
        diagnosticsSection.appendChild(createElement(
            documentRef, 'p', 'worldbook-diagnostics-empty', '当前没有上一轮使用记录。',
        ));
    } else {
        const activated = diagnostics.filter(item => item.activated).length;
        const kept = diagnostics.filter(item => item.kept).length;
        diagnosticsSection.appendChild(createElement(
            documentRef,
            'p',
            'worldbook-diagnostics-summary',
            `检查了 ${diagnostics.length} 项设定，${activated} 项符合条件，${kept} 项在本轮使用。这里不展示设定正文。`,
        ));
        const list = createElement(documentRef, 'ul', 'worldbook-diagnostics-list');
        for (const record of diagnostics) {
            const item = createElement(documentRef, 'li', 'worldbook-diagnostic-item');
            const diagnosticState = record.kept ? 'kept' : record.activated ? 'triggered' : 'excluded';
            const state = createElement(
                documentRef,
                'span',
                `worldbook-diagnostic-state ${diagnosticState}`,
                record.kept ? '本轮已使用' : record.activated ? '符合条件' : '本轮未使用',
            );
            const matchingEntry = entries.find(entry => entry.id === record.id);
            const id = createElement(
                documentRef,
                'button',
                'worldbook-diagnostic-id',
                matchingEntry && matchingEntry.title ? matchingEntry.title : '未命名设定',
            );
            id.type = 'button';
            id.addEventListener('click', () => {
                if (!entries.some(entry => entry.id === record.id)) return;
                if (!allowDraftSwitch()) return;
                selectedId = record.id;
                renderList(record.id);
                renderForm();
            });
            const reason = createElement(documentRef, 'span', 'worldbook-diagnostic-reason', diagnosticReason(record));
            item.appendChild(state);
            item.appendChild(id);
            item.appendChild(reason);
            list.appendChild(item);
        }
        diagnosticsSection.appendChild(list);
    }
    const layout = createElement(documentRef, 'div', 'worldbook-layout');
    const sidebar = createElement(documentRef, 'aside', 'worldbook-sidebar');
    sidebar.setAttribute('aria-label', '世界设定列表');
    const sidebarTitle = createElement(documentRef, 'h3', 'worldbook-section-title', '世界设定');
    const newButton = createElement(documentRef, 'button', 'modal-btn primary worldbook-new-button', '新建设定');
    newButton.type = 'button';
    const listTools = createElement(documentRef, 'div', 'worldbook-list-tools');
    const searchLabel = createElement(documentRef, 'label', 'worldbook-visually-hidden', '搜索世界设定');
    const searchInput = createElement(documentRef, 'input', 'worldbook-control worldbook-list-search');
    searchInput.type = 'search';
    searchInput.placeholder = '搜索标题、摘要或关键词';
    searchInput.setAttribute('aria-label', '搜索世界设定');
    const categorySelect = createElement(documentRef, 'select', 'worldbook-control worldbook-list-filter');
    categorySelect.setAttribute('aria-label', '按设定类型筛选');
    const allCategory = createElement(documentRef, 'option', '', '全部类型');
    allCategory.value = 'all';
    categorySelect.appendChild(allCategory);
    for (const category of CATEGORIES) {
        const option = createElement(documentRef, 'option', '', CATEGORY_LABELS[category] || category);
        option.value = category;
        categorySelect.appendChild(option);
    }
    listTools.append(searchLabel, searchInput, categorySelect);
    const listRoot = createElement(documentRef, 'div', 'worldbook-entry-list');
    listRoot.setAttribute('role', 'list');
    const manualActions = createElement(documentRef, 'div', 'worldbook-manual-actions');
    const manualSaveButton = createElement(
        documentRef, 'button', 'modal-btn worldbook-manual-save worldbook-write-control', '保存当前存档设置',
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
    sidebar.appendChild(listTools);
    sidebar.appendChild(listRoot);
    sidebar.appendChild(manualActions);

    const formRoot = createElement(documentRef, 'div', 'worldbook-form-panel');
    layout.appendChild(sidebar);
    layout.appendChild(formRoot);
    root.appendChild(layout);
    root.appendChild(diagnosticsSection);

    function currentDraft() {
        return selectedId === null ? newDraft : drafts.get(selectedId);
    }

    function manualDirty() {
        return !sameStringSet(manualBaseline, manualDraft);
    }

    function entryDirty() {
        const draft = currentDraft();
        const baseline = selectedId === null ? newDraftBaseline : draftBaselines.get(selectedId);
        return Boolean(draft) && draftFingerprint(draft) !== baseline;
    }

    function allowDraftSwitch() {
        if (!entryDirty()) return true;
        if (typeof options.confirmDiscard === 'function') return options.confirmDiscard();
        return globalThis.confirm ? globalThis.confirm('当前设定有未保存内容，确定放弃并切换吗？') : false;
    }

    function activationSentence(draft) {
        if (!draft.enabled) return '已停用，不会进入任何存档的模型上下文。';
        if (draft.activation === 'always') return '每轮作为候选设定参与上下文组装。';
        if (draft.activation === 'manual') return manualDraft.has(draft.id)
            ? '当前存档已手动启用。' : '需要在当前存档中手动启用。';
        if (draft.activation === 'scene') {
            const locations = String(draft.locationAliasesText || '').split(/\r?\n/).filter(Boolean);
            const characters = String(draft.linkedCharacterIdsText || '').split(/\r?\n/).filter(Boolean);
            return `场景出现${locations.length ? `地点“${locations.join('、')}”` : ''}${locations.length && characters.length ? '或' : ''}${characters.length ? `${characters.length} 位关联角色` : ''}时成为候选。`;
        }
        const keywords = String(draft.keywordsText || '').split(/\r?\n/).filter(Boolean);
        return keywords.length ? `对话包含“${keywords.slice(0, 4).join('”或“')}”时成为候选。` : '尚未设置触发关键词。';
    }

    function updateInspector(draft) {
        inspectorRows.scope.textContent = selectedId === null ? '项目设定 · 尚未保存' : '项目设定';
        inspectorRows.activation.textContent = activationSentence(draft);
        inspectorRows.knowledge.textContent = `${VISIBILITY_LABELS[draft.visibility] || draft.visibility} · ${KNOWLEDGE_LABELS[draft.knowledgeScope] || draft.knowledgeScope}`;
        const characterCount = String(draft.linkedCharacterIdsText || '').split(/\r?\n/).filter(Boolean).length;
        const entryCount = String(draft.linkedEntryIdsText || '').split(/\r?\n/).filter(Boolean).length;
        inspectorRows.links.textContent = `角色 ${characterCount} · 世界设定 ${entryCount}`;
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
        for (const checkbox of root.querySelectorAll('.worldbook-entity-choice input')) {
            checkbox.disabled = writeDisabled || entryPending || manualPending;
        }
        manualSaveButton.disabled = writeDisabled || entryPending || manualPending || !manualDirty();
        manualSaveButton.setAttribute('aria-busy', String(manualPending));
        manualSaveButton.textContent = manualPending ? '保存中…' : '保存当前存档设置';
    }

    function renderList(focusId = undefined) {
        listRoot.replaceChildren();
        newButton.classList.toggle('active', selectedId === null);
        newButton.setAttribute('aria-current', selectedId === null ? 'true' : 'false');
        const query = listQuery.trim().toLocaleLowerCase('zh-CN');
        const visibleEntries = entries.filter(entry => {
            if (categoryFilter !== 'all' && entry.category !== categoryFilter) return false;
            if (!query) return true;
            return [entry.title, entry.summary, entry.id, ...entry.keywords]
                .some(value => String(value || '').toLocaleLowerCase('zh-CN').includes(query));
        });
        for (const entry of visibleEntries) {
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
                `编辑世界设定 ${entry.title || '未命名设定'}`,
            );
            selectButton.appendChild(createElement(
                documentRef, 'span', 'worldbook-entry-title', entry.title || '未命名设定',
            ));
            if (entry.summary) selectButton.appendChild(createElement(
                documentRef, 'span', 'worldbook-entry-summary', entry.summary,
            ));
            const badges = createElement(documentRef, 'span', 'worldbook-entry-badges');
            badges.appendChild(createElement(
                documentRef,
                'span',
                `worldbook-badge ${entry.enabled ? '' : 'disabled'}`,
                entry.enabled ? activationLabel(entry.activation) : '全局关闭',
            ));
            badges.appendChild(createElement(
                documentRef, 'span', `worldbook-badge worldbook-category-badge category-${entry.category}`,
                CATEGORY_LABELS[entry.category] || entry.category,
            ));
            selectButton.appendChild(badges);
            selectButton.addEventListener('click', () => {
                if (selectedId !== entry.id && !allowDraftSwitch()) return;
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
                checkbox.setAttribute('aria-label', `当前存档${checkbox.checked ? '停用' : '启用'}设定 ${entry.title || '未命名设定'}`);
                const text = createElement(
                    documentRef,
                    'span',
                    'worldbook-manual-label',
                    checkbox.checked ? '当前存档已启用' : '当前存档未启用',
                );
                checkbox.addEventListener('change', () => {
                    if (checkbox.checked) manualDraft.add(entry.id);
                    else manualDraft.delete(entry.id);
                    text.textContent = checkbox.checked ? '当前存档已启用' : '当前存档未启用';
                    checkbox.setAttribute(
                        'aria-label',
                        `当前存档${checkbox.checked ? '停用' : '启用'}设定 ${entry.title || '未命名设定'}`,
                    );
                    setLiveMessage(manualStatus, manualDirty() ? '当前存档设置尚未保存。' : '');
                    setLiveMessage(manualError, '');
                    setPendingState();
                });
                label.appendChild(checkbox);
                label.appendChild(text);
                item.appendChild(label);
            }
            listRoot.appendChild(item);
        }
        if (visibleEntries.length === 0) {
            listRoot.appendChild(createElement(documentRef, 'p', 'worldbook-list-empty', '没有符合筛选条件的设定。'));
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

    function createFormSection(title, description = '') {
        const section = createElement(documentRef, 'fieldset', 'worldbook-form-section');
        section.appendChild(createElement(documentRef, 'legend', 'worldbook-form-section-title', title));
        if (description) section.appendChild(createElement(documentRef, 'p', 'worldbook-form-section-description', description));
        return section;
    }

    function createCollapsibleFormSection(title, description = '') {
        const section = createElement(documentRef, 'details', 'worldbook-form-section worldbook-collapsible-section');
        section.appendChild(createElement(documentRef, 'summary', 'worldbook-form-section-title', title));
        if (description) section.appendChild(createElement(documentRef, 'p', 'worldbook-form-section-description', description));
        return section;
    }

    function appendEntityPicker(host, config) {
        const row = createElement(documentRef, 'div', `worldbook-field worldbook-entity-field worldbook-field-${config.field}`);
        const label = createElement(documentRef, 'span', 'worldbook-field-label', config.label);
        const search = createElement(documentRef, 'input', 'worldbook-control worldbook-entity-search');
        search.type = 'search';
        search.placeholder = config.placeholder || `搜索${config.label}`;
        search.setAttribute('aria-label', `搜索${config.label}`);
        search.dataset.field = config.field;
        const choicesHost = createElement(documentRef, 'div', 'worldbook-entity-choices');
        const selected = new Set(config.selected || []);
        const available = new Map((config.choices || []).map(choice => [choice.id, choice]));
        for (const id of selected) {
            if (!available.has(id)) available.set(id, { id, label: `已缺失对象（${id}）`, missing: true });
        }

        function renderChoices() {
            choicesHost.replaceChildren();
            const query = search.value.trim().toLocaleLowerCase('zh-CN');
            const visible = [...available.values()].filter(choice => (
                !query || choice.label.toLocaleLowerCase('zh-CN').includes(query)
                || choice.id.toLocaleLowerCase('zh-CN').includes(query)
            ));
            for (const choice of visible) {
                const option = createElement(documentRef, 'label', `worldbook-entity-choice${choice.missing ? ' is-missing' : ''}`);
                const checkbox = createElement(documentRef, 'input', 'worldbook-write-control');
                checkbox.type = 'checkbox';
                checkbox.value = choice.id;
                checkbox.checked = selected.has(choice.id);
                checkbox.addEventListener('change', () => {
                    if (checkbox.checked) selected.add(choice.id);
                    else selected.delete(choice.id);
                    config.onChange([...selected]);
                    updateInspector(currentDraft());
                });
                option.append(checkbox, createElement(documentRef, 'span', '', choice.label));
                choicesHost.appendChild(option);
            }
            if (visible.length === 0) choicesHost.appendChild(createElement(documentRef, 'p', 'worldbook-list-empty', '没有匹配对象。'));
        }
        search.addEventListener('input', renderChoices);
        row.append(label, search, choicesHost);
        if (typeof config.onCreate === 'function') {
            const createButton = createElement(
                documentRef,
                'button',
                'modal-btn worldbook-entity-create',
                config.createLabel || '＋ 新建设定',
            );
            createButton.type = 'button';
            createButton.addEventListener('click', config.onCreate);
            row.appendChild(createButton);
        }
        if (config.hint) row.appendChild(createElement(documentRef, 'p', 'worldbook-field-hint', config.hint));
        host.appendChild(row);
        renderChoices();
        return { row, control: search };
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
            if (!ids.has(key)) {
                drafts.delete(key);
                draftBaselines.delete(key);
            }
        }
        for (const entry of entries) {
            if (!drafts.has(entry.id) || entry.id === savedId) {
                const draft = draftFromEntry(entry);
                drafts.set(entry.id, draft);
                draftBaselines.set(entry.id, draftFingerprint(draft));
            }
        }
        if (savedId) {
            newDraft = draftFromEntry(null);
            newDraftBaseline = draftFingerprint(newDraft);
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
            if (!String(draft.title || '').trim()) {
                throw new WorldbookValidationError('请填写设定名称', 'title');
            }
            if (!String(draft.content || '').trim()) {
                throw new WorldbookValidationError('请填写详细内容', 'content');
            }
            if (selectedId === null && !String(draft.id || '').trim()) {
                const usedIds = new Set(entries.map(entry => entry.id));
                const base = `setting_${Date.now().toString(36)}`;
                let candidate = base;
                let suffix = 2;
                while (usedIds.has(candidate)) candidate = `${base}_${suffix++}`;
                draft.id = candidate;
            }
            if (draft.activation === 'keywords' && !String(draft.keywordsText || '').trim()) {
                draft.keywordsText = String(draft.title || '').trim();
            }
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
            savedDraft.status = '设定已保存';
            renderForm();
            return true;
        } catch (error) {
            draft.error = errorText(error);
            draft.errorField = '';
            showDraftError(draft, currentFormRefs.fieldMap, currentFormRefs.saveButton);
            return false;
        } finally {
            entryPending = false;
            if (currentFormRefs) currentFormRefs.saveButton.textContent = '保存设定';
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
            setLiveMessage(manualStatus, '当前存档设置已保存');
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
            selectedId === null ? '新建设定' : `编辑：${draft.title || '未命名设定'}`,
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

        const basicSection = createFormSection('基本内容', '名称、类型和一句话介绍会显示在设定列表中。');
        const contentSection = createFormSection('详细设定', '写清楚 AI 在故事中需要遵守的事实和规则。');
        const activationSection = createFormSection('什么时候参考', '选择 AI 在什么情况下需要读取这项设定。');
        const knowledgeSection = createFormSection('谁知道这件事', '分别控制玩家和故事人物能否知道。');
        const relationSection = createCollapsibleFormSection('关联角色与其他设定', '需要建立联系时再展开选择。');
        const advancedSection = createCollapsibleFormSection('专业设置', '内部标识、冲突顺序和自定义字段。');
        form.append(basicSection, contentSection, activationSection, knowledgeSection, relationSection, advancedSection);

        const idConfig = fieldSchema('id');
        const idField = appendLabeledControl(advancedSection, {
            field: 'id', label: '内部标识', value: draft.id, readOnly: selectedId !== null,
            hint: selectedId === null
                ? '保存时自动生成，无需填写。'
                : '用于保持已有存档和设定关联，保存后不能修改。',
        });
        idField.control.addEventListener('input', () => { draft.id = idField.control.value; });

        const titleConfig = fieldSchema('title');
        const titleField = appendLabeledControl(basicSection, {
            field: 'title', label: '设定名称', value: draft.title,
            hint: titleConfig.hint || '例如“琉璃宫”“共鸣规则”“今州边庭”。',
        });
        titleField.control.required = true;
        titleField.control.addEventListener('input', () => { draft.title = titleField.control.value; });

        const summaryConfig = fieldSchema('summary');
        const summaryField = appendLabeledControl(basicSection, {
            field: 'summary', label: '一句话介绍（可选）', value: draft.summary,
            hint: summaryConfig.hint || '用一句话概括，方便之后快速查找。',
        });
        summaryField.control.addEventListener('input', () => { draft.summary = summaryField.control.value; });

        const categoryConfig = fieldSchema('category');
        const categoryField = appendLabeledControl(basicSection, {
            field: 'category', label: '设定类型', tag: 'select', hint: '类型只用于整理，不影响内容。',
        });
        for (const config of categoryConfig.options) {
            const option = createElement(documentRef, 'option', '', config.label);
            option.value = config.value;
            categoryField.control.appendChild(option);
        }
        categoryField.control.value = draft.category;
        categoryField.control.addEventListener('change', () => { draft.category = categoryField.control.value; });

        const enabledConfig = fieldSchema('enabled');
        const enabledRow = createElement(documentRef, 'div', 'worldbook-field worldbook-checkbox-field');
        const enabledLabel = createElement(documentRef, 'label', 'worldbook-checkbox-label');
        const enabledInput = createElement(documentRef, 'input', 'worldbook-control worldbook-enabled');
        enabledInput.type = 'checkbox';
        enabledInput.checked = draft.enabled;
        const enabledText = createElement(documentRef, 'span', '', enabledConfig.label);
        const enabledHint = createElement(
            documentRef, 'span', 'worldbook-field-hint', '关闭后所有存档都不会参考这项设定。',
        );
        enabledInput.addEventListener('change', () => { draft.enabled = enabledInput.checked; });
        enabledLabel.appendChild(enabledInput);
        enabledLabel.appendChild(enabledText);
        enabledLabel.appendChild(enabledHint);
        enabledRow.appendChild(enabledLabel);
        basicSection.appendChild(enabledRow);

        const activationConfig = fieldSchema('activation');
        const activationField = appendLabeledControl(activationSection, {
            field: 'activation', label: '参考时机', tag: 'select',
            hint: '“提到它时参考”适合大多数地点、势力、物品和人物背景。',
        });
        for (const config of activationConfig.options) {
            const option = createElement(documentRef, 'option', '', ACTIVATION_LABELS[config.value] || config.label);
            option.value = config.value;
            activationField.control.appendChild(option);
        }
        activationField.control.value = draft.activation;

        const keywordsConfig = fieldSchema('keywords');
        const keywordsField = appendLabeledControl(activationSection, {
            field: 'keywords', label: '相关名称（每行一个）', tag: 'textarea', rows: keywordsConfig.rows || 4,
            value: draft.keywordsText,
            hint: '留空时自动使用设定名称。还可以填写别名、简称或旧称。',
        });
        keywordsField.row.classList.add('worldbook-keywords-field');
        keywordsField.control.addEventListener('input', () => { draft.keywordsText = keywordsField.control.value; });

        const manualHint = createElement(
            documentRef,
            'p',
            'worldbook-manual-entry-hint',
            '保存后，可在左侧为当前存档单独启用或停用。',
        );
        activationSection.appendChild(manualHint);

        function syncActivationFields() {
            draft.activation = activationField.control.value;
            keywordsField.row.hidden = draft.activation !== 'keywords';
            keywordsField.row.setAttribute('aria-hidden', String(keywordsField.row.hidden));
            manualHint.hidden = draft.activation !== 'manual';
        }
        activationField.control.addEventListener('change', syncActivationFields);
        syncActivationFields();

        const priorityConfig = fieldSchema('priority');
        const priorityField = appendLabeledControl(advancedSection, {
            field: 'priority', label: '冲突时优先顺序', type: 'number', step: '1', value: draft.priority,
            min: priorityConfig.min, max: priorityConfig.max,
            hint: '数值越大越优先。没有设定冲突时保持 0 即可。',
        });
        priorityField.control.addEventListener('input', () => { draft.priority = priorityField.control.value; });

        const visibilityConfig = fieldSchema('visibility');
        const visibilityField = appendLabeledControl(knowledgeSection, {
            field: 'visibility', label: '玩家什么时候知道', tag: 'select',
            hint: '选择“发现后”时，会结合当前存档的发现记录显示。',
        });
        for (const config of visibilityConfig.options) {
            const option = createElement(documentRef, 'option', '', VISIBILITY_LABELS[config.value] || config.label);
            option.value = config.value;
            visibilityField.control.appendChild(option);
        }
        visibilityField.control.value = draft.visibility;
        visibilityField.control.addEventListener('change', () => { draft.visibility = visibilityField.control.value; });

        const knowledgeConfig = fieldSchema('knowledge_scope');
        const knowledgeField = appendLabeledControl(knowledgeSection, {
            field: 'knowledge_scope', label: '故事人物中谁知道', tag: 'select',
            hint: '用于避免角色提前说出自己不应该知道的秘密。',
        });
        for (const config of knowledgeConfig.options) {
            const option = createElement(documentRef, 'option', '', KNOWLEDGE_LABELS[config.value] || config.label);
            option.value = config.value;
            knowledgeField.control.appendChild(option);
        }
        knowledgeField.control.value = draft.knowledgeScope;

        const knownConfig = fieldSchema('known_by_character_ids');
        const knownField = appendEntityPicker(knowledgeSection, {
            field: 'known_by_character_ids', label: '知情角色', choices: characterChoices,
            selected: draft.knownByCharacterIdsText.split(/\r?\n/).filter(Boolean),
            hint: knownConfig.hint || '只有选中的角色可以使用这条设定中的知识。',
            onChange(ids) { draft.knownByCharacterIdsText = ids.join('\n'); },
        });

        const linkedCharacterConfig = fieldSchema('linked_character_ids');
        const linkedCharacterField = appendEntityPicker(relationSection, {
            field: 'linked_character_ids', label: '关联角色', choices: characterChoices,
            selected: draft.linkedCharacterIdsText.split(/\r?\n/).filter(Boolean),
            hint: linkedCharacterConfig.hint || '用于场景联动和关系浏览。',
            onChange(ids) { draft.linkedCharacterIdsText = ids.join('\n'); },
        });

        const locationConfig = fieldSchema('location_aliases');
        const locationField = appendLabeledControl(relationSection, {
            field: 'location_aliases', label: locationConfig.label, tag: 'textarea', rows: locationConfig.rows || 3,
            value: draft.locationAliasesText, hint: locationConfig.hint || '每行一个地点名称或常用别名，不需要填写内部 ID。',
        });
        locationField.control.addEventListener('input', () => { draft.locationAliasesText = locationField.control.value; });

        const linkedEntryConfig = fieldSchema('linked_entry_ids');
        const linkedEntryField = appendEntityPicker(relationSection, {
            field: 'linked_entry_ids', label: '关联世界设定',
            choices: entries.filter(entry => entry.id !== draft.id).map(entry => ({
                id: entry.id,
                label: `${entry.title || entry.id} · ${CATEGORY_LABELS[entry.category] || entry.category}`,
            })),
            selected: draft.linkedEntryIdsText.split(/\r?\n/).filter(Boolean),
            hint: linkedEntryConfig.hint || '关联会直接进入“关系图”，点击即可互相跳转。',
            onChange(ids) { draft.linkedEntryIdsText = ids.join('\n'); },
            createLabel: '＋ 新建关联设定',
            onCreate() {
                if (selectedId === null) {
                    idField.control.focus();
                    return;
                }
                if (!allowDraftSwitch()) return;
                selectedId = null;
                renderList(null);
                renderForm();
            },
        });

        function syncKnowledgeFields() {
            draft.knowledgeScope = knowledgeField.control.value;
            knownField.row.hidden = draft.knowledgeScope !== 'characters';
            knownField.row.setAttribute('aria-hidden', String(knownField.row.hidden));
        }
        knowledgeField.control.addEventListener('change', syncKnowledgeFields);
        syncKnowledgeFields();

        const originalSyncActivationFields = syncActivationFields;
        function syncWorldLinkFields() {
            originalSyncActivationFields();
            const sceneMode = draft.activation === 'scene';
            linkedCharacterField.row.classList.toggle('worldbook-scene-link-active', sceneMode);
            locationField.row.classList.toggle('worldbook-scene-link-active', sceneMode);
        }
        activationField.control.removeEventListener('change', syncActivationFields);
        activationField.control.addEventListener('change', syncWorldLinkFields);
        syncWorldLinkFields();

        const contentConfig = fieldSchema('content');
        const contentField = appendLabeledControl(contentSection, {
            field: 'content', label: '详细内容', tag: 'textarea', rows: contentConfig.rows || 8, value: draft.content,
            hint: '直接写事实和规则即可，不需要写提示词格式。',
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
        advancedSection.appendChild(customFieldset);

        const actions = createElement(documentRef, 'div', 'worldbook-entry-actions');
        let deleteButton = null;
        if (selectedId !== null) {
            deleteButton = createElement(
                documentRef, 'button', 'modal-btn danger worldbook-entry-delete worldbook-write-control', '删除设定',
            );
            deleteButton.type = 'button';
            deleteButton.addEventListener('click', () => { void deleteEntry(); });
            actions.appendChild(deleteButton);
        }
        const saveButton = createElement(
            documentRef, 'button', 'modal-btn primary worldbook-entry-save worldbook-write-control', '保存设定',
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
            ['summary', summaryField.control],
            ['category', categoryField.control],
            ['enabled', enabledInput],
            ['activation', activationField.control],
            ['keywords', keywordsField.control],
            ['priority', priorityField.control],
            ['visibility', visibilityField.control],
            ['knowledge_scope', knowledgeField.control],
            ['known_by_character_ids', knownField.control],
            ['linked_character_ids', linkedCharacterField.control],
            ['linked_entry_ids', linkedEntryField.control],
            ['location_aliases', locationField.control],
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
                summaryField.control,
                categoryField.control,
                enabledInput,
                activationField.control,
                keywordsField.control,
                priorityField.control,
                visibilityField.control,
                knowledgeField.control,
                knownField.control,
                linkedCharacterField.control,
                linkedEntryField.control,
                locationField.control,
                contentField.control,
                addCustomButton,
            ],
        };
        function markDraftDirty(event) {
            if (event.target && event.target.classList.contains('worldbook-entity-search')) return;
            draft.status = '未保存修改';
            setLiveMessage(status, draft.status);
            updateInspector(draft);
        }
        form.addEventListener('input', markDraftDirty);
        form.addEventListener('change', markDraftDirty);
        form.addEventListener('click', event => {
            if (event.target && event.target.matches('.worldbook-custom-add, .worldbook-custom-remove')) {
                markDraftDirty(event);
            }
        });
        formRoot.appendChild(form);
        updateInspector(draft);
        if (draft.errorField) {
            const target = fieldMap.get(draft.errorField);
            if (target) target.setAttribute('aria-invalid', 'true');
        }
        setPendingState();
    }

    newButton.addEventListener('click', () => {
        if (selectedId !== null && !allowDraftSwitch()) return;
        selectedId = null;
        renderList(null);
        renderForm();
    });
    searchInput.addEventListener('input', () => {
        listQuery = searchInput.value;
        renderList();
    });
    categorySelect.addEventListener('change', () => {
        categoryFilter = categorySelect.value;
        renderList();
    });
    manualSaveButton.addEventListener('click', () => { void commitManualSelection(); });
    root.addEventListener('keydown', event => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase('zh-CN') === 's') {
            event.preventDefault();
            void saveEntry();
        }
        if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase('zh-CN') === 'k') {
            event.preventDefault();
            searchInput.focus();
        }
    });

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
                entryDirty: entryDirty(),
                entryPending,
                manualPending,
            });
        },
    });
}

export { ACTIVATIONS as WORLD_ACTIVATIONS };
