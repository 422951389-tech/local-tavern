export function getPathValue(source, path, field = {}) {
    if (field.type === 'custom') return field.value || '';
    let value = String(path || '').split('.').reduce((current, key) => (
        current == null ? undefined : current[key]
    ), source);
    if (value === undefined && field.default !== undefined) value = field.default;
    if (field.array) return Array.isArray(value) ? value.join('\n') : '';
    if (field.type === 'checkbox') return value !== false;
    return value ?? '';
}

export function setPathValue(target, path, rawValue, field = {}) {
    const keys = String(path || '').split('.').filter(Boolean);
    if (keys.length === 0) return target;
    let value = rawValue;
    if (field.array) value = String(value).split('\n').map(item => item.trim()).filter(Boolean);
    if (field.type === 'number' || field.type === 'integer') {
        value = Number(value);
        if (!Number.isFinite(value)) throw new CardFieldValidationError(`${field.label || path} 必须是数字`, path);
        if (field.integer && !Number.isSafeInteger(value)) {
            throw new CardFieldValidationError(`${field.label || path} 必须是整数`, path);
        }
        if (field.min !== undefined && value < Number(field.min)) {
            throw new CardFieldValidationError(`${field.label || path} 不能小于 ${field.min}`, path);
        }
        if (field.max !== undefined && value > Number(field.max)) {
            throw new CardFieldValidationError(`${field.label || path} 不能大于 ${field.max}`, path);
        }
    }
    if (field.type === 'checkbox') value = Boolean(value);
    const last = keys.pop();
    let current = target;
    for (const key of keys) {
        if (!current[key] || typeof current[key] !== 'object' || Array.isArray(current[key])) current[key] = {};
        current = current[key];
    }
    current[last] = value;
    return target;
}

export class CardFieldValidationError extends TypeError {
    constructor(message, field = '') {
        super(message);
        this.name = 'CardFieldValidationError';
        this.field = field;
    }
}

export function serializeCardFields(records, { idField = null } = {}) {
    const data = { custom: {} };
    let idValue = '';
    for (const record of records || []) {
        if (record.customKey !== undefined) {
            const customKey = String(record.customKey || '').trim();
            if (customKey) data.custom[customKey] = String(record.value ?? '').trim();
            continue;
        }
        const key = String(record.key || '').trim();
        if (!key) continue;
        setPathValue(data, key, record.value, {
            array: Boolean(record.array),
            type: record.type,
            label: record.label,
            integer: Boolean(record.integer),
            min: record.min,
            max: record.max,
        });
        if (key === idField) idValue = record.value;
    }
    if (Object.keys(data.custom).length === 0) delete data.custom;
    if (!idField) data.id = 'user';
    return { data, idValue };
}

function cloneJsonValue(value) {
    if (Array.isArray(value)) return value.map(cloneJsonValue);
    if (!value || typeof value !== 'object') return value;
    const result = {};
    for (const [key, item] of Object.entries(value)) {
        Object.defineProperty(result, key, {
            value: cloneJsonValue(item),
            enumerable: true,
            configurable: true,
            writable: true,
        });
    }
    return result;
}

function mergeJsonRecord(target, source) {
    for (const [key, value] of Object.entries(source || {})) {
        const current = Object.prototype.hasOwnProperty.call(target, key) ? target[key] : undefined;
        if (value && typeof value === 'object' && !Array.isArray(value)
            && current && typeof current === 'object' && !Array.isArray(current)
            && key !== 'custom') {
            mergeJsonRecord(current, value);
        } else {
            Object.defineProperty(target, key, {
                value: cloneJsonValue(value),
                enumerable: true,
                configurable: true,
                writable: true,
            });
        }
    }
    return target;
}

export function mergeCardEditorData(original, edited) {
    const result = original && typeof original === 'object' && !Array.isArray(original)
        ? cloneJsonValue(original)
        : {};
    mergeJsonRecord(result, edited && typeof edited === 'object' && !Array.isArray(edited) ? edited : {});
    if (!edited || !Object.prototype.hasOwnProperty.call(edited, 'custom')) delete result.custom;
    return result;
}

export function collectCardEditorData(groupsRoot, options = {}) {
    const records = [...groupsRoot.querySelectorAll('.fld-row')].map(row => {
        const valueInput = row.querySelector('.ce-field-val');
        const keyInput = row.querySelector('.ce-field-key');
        const value = valueInput && valueInput.type === 'checkbox'
            ? valueInput.checked
            : (valueInput ? valueInput.value : '');
        if (keyInput) return { customKey: keyInput.value, value };
        return {
            key: row.dataset.key,
            type: row.dataset.type,
            label: row.dataset.label,
            array: row.dataset.array === '1',
            integer: row.dataset.integer === '1',
            min: row.dataset.min === undefined ? undefined : Number(row.dataset.min),
            max: row.dataset.max === undefined ? undefined : Number(row.dataset.max),
            value,
        };
    });
    return serializeCardFields(records, options);
}

export function clearDragState(root) {
    for (const element of root.querySelectorAll('.dragging,.fld-dragging,.drag-over,.fld-drag-over')) {
        element.classList.remove('dragging', 'fld-dragging', 'drag-over', 'fld-drag-over');
    }
}

export function groupsWithCustomFields(groups, item, customGroup = {}) {
    const result = (groups || []).map(group => ({
        ...group,
        fields: (group.fields || []).map(field => ({ ...field })),
    }));
    const custom = item && item.custom && typeof item.custom === 'object' && !Array.isArray(item.custom)
        ? item.custom
        : {};
    const fields = Object.entries(custom).map(([key, value]) => ({
        key,
        label: key,
        value: value == null ? '' : String(value),
        type: 'custom',
        builtin: false,
    }));
    if (fields.length > 0) {
        result.push({
            key: customGroup.key || '_custom',
            label: customGroup.label || '自定义字段',
            builtin: false,
            fields,
        });
    }
    return result;
}
