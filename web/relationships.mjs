const SVG_NS = 'http://www.w3.org/2000/svg';
const MAX_EDGES = 200;
const MAX_EVIDENCE = 20;
const MAX_RELATION_TYPE = 80;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const UNSAFE_RELATION_TYPE_PATTERN = /[\p{Cc}\p{Cf}\p{Cs}]/u;

export class RelationshipValidationError extends TypeError {
    constructor(message, field = '') {
        super(message);
        this.name = 'RelationshipValidationError';
        this.field = field;
    }
}

export class RelationshipNavigationError extends Error {
    constructor(message, code) {
        super(message);
        this.name = 'RelationshipNavigationError';
        this.code = code || 'relationship_navigation_failed';
    }
}

function isRecord(value) {
    return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function text(value) {
    return typeof value === 'string' ? value : '';
}

function cleanId(value) {
    return text(value).trim();
}

function isUuid(value) {
    return typeof value === 'string' && UUID_PATTERN.test(value);
}

function freezeArray(values) {
    return Object.freeze(values.map(value => Object.freeze(value)));
}

function messagePreview(message, limit = 96) {
    const content = text(message && message.content).replace(/\s+/g, ' ').trim();
    if (!content) return '（空消息）';
    return content.length > limit ? `${content.slice(0, limit)}…` : content;
}

function messageChoiceLabel(message, index) {
    const role = message.role === 'assistant' ? 'AI' : '用户';
    const time = message.createdAt
        ? message.createdAt.slice(0, 16).replace('T', ' ')
        : '时间未记录';
    const shortId = `${message.id.slice(0, 8)}…${message.id.slice(-4)}`;
    return `${role} · #${index + 1} · ${time} · ${shortId}：${messagePreview(message)}`;
}

export function normalizeRelationshipSession(session) {
    const source = isRecord(session) ? session : {};
    const states = isRecord(source.characters_state) ? source.characters_state : {};
    const characters = Object.entries(states)
        .filter(([id, value]) => cleanId(id) && isRecord(value))
        .map(([id, value]) => ({
            id,
            name: text(value.name).trim() || id,
        }));
    const characterIds = new Set(characters.map(character => character.id));
    const messages = (Array.isArray(source.message_history) ? source.message_history : [])
        .filter(message => isRecord(message) && isUuid(message.id))
        .map(message => ({
            id: message.id,
            role: message.role === 'assistant' ? 'assistant' : 'user',
            content: text(message.content),
            createdAt: text(message.timestamps && message.timestamps.created_at)
                || text(message.created_at),
        }));
    const messageIds = new Set(messages.map(message => message.id));
    const rawEdges = Array.isArray(source.relationship_edges) ? source.relationship_edges : [];
    const edges = rawEdges.slice(0, MAX_EDGES).flatMap(raw => {
        if (!isRecord(raw)) return [];
        const sourceId = cleanId(raw.source_character_id);
        const targetId = cleanId(raw.target_character_id);
        const relationType = text(raw.relation_type).trim();
        const strength = raw.strength;
        const rawEvidence = Array.isArray(raw.evidence_message_ids) ? raw.evidence_message_ids : [];
        const evidence = [...new Set(rawEvidence.filter(id => isUuid(id) && messageIds.has(id)))];
        if (!characterIds.has(sourceId) || !characterIds.has(targetId) || sourceId === targetId) return [];
        if (
            !relationType
            || relationType.length > MAX_RELATION_TYPE
            || UNSAFE_RELATION_TYPE_PATTERN.test(relationType)
        ) return [];
        if (!Number.isSafeInteger(strength) || strength < 0 || strength > 100) return [];
        if (evidence.length === 0 || evidence.length > MAX_EVIDENCE) return [];
        return [{
            sourceCharacterId: sourceId,
            targetCharacterId: targetId,
            relationType,
            strength,
            evidenceMessageIds: Object.freeze(evidence),
            updatedAt: text(raw.updated_at),
        }];
    });
    return Object.freeze({
        characters: freezeArray(characters),
        messages: freezeArray(messages),
        edges: freezeArray(edges),
    });
}

export function relationshipKey(edge) {
    if (!edge) throw new RelationshipValidationError('关系边缺少复合键');
    return Object.freeze({
        source_character_id: cleanId(edge.sourceCharacterId ?? edge.source_character_id),
        target_character_id: cleanId(edge.targetCharacterId ?? edge.target_character_id),
        relation_type: text(edge.relationType ?? edge.relation_type).trim(),
    });
}

export function validateRelationshipDraft(draft, session) {
    if (!isRecord(draft)) throw new RelationshipValidationError('关系表单无效');
    const normalized = normalizeRelationshipSession(session);
    const characterIds = new Set(normalized.characters.map(character => character.id));
    const messageIds = new Set(normalized.messages.map(message => message.id));
    const source = cleanId(draft.source_character_id ?? draft.sourceCharacterId);
    const target = cleanId(draft.target_character_id ?? draft.targetCharacterId);
    const relationType = text(draft.relation_type ?? draft.relationType).trim();
    if (!characterIds.has(source)) {
        throw new RelationshipValidationError('请选择当前存档中的来源角色', 'source_character_id');
    }
    if (!characterIds.has(target)) {
        throw new RelationshipValidationError('请选择当前存档中的目标角色', 'target_character_id');
    }
    if (source === target) {
        throw new RelationshipValidationError('来源角色和目标角色不能相同', 'target_character_id');
    }
    if (!relationType || relationType.length > MAX_RELATION_TYPE || UNSAFE_RELATION_TYPE_PATTERN.test(relationType)) {
        throw new RelationshipValidationError(`关系类型必须为 1 到 ${MAX_RELATION_TYPE} 个有效字符`, 'relation_type');
    }
    const rawStrength = draft.strength;
    const strength = typeof rawStrength === 'number'
        ? rawStrength
        : (/^\d+$/.test(text(rawStrength).trim()) ? Number(rawStrength) : Number.NaN);
    if (!Number.isSafeInteger(strength) || strength < 0 || strength > 100) {
        throw new RelationshipValidationError('关系强度必须是 0 到 100 的整数', 'strength');
    }
    if (!Array.isArray(draft.evidence_message_ids ?? draft.evidenceMessageIds)) {
        throw new RelationshipValidationError('请选择证据消息', 'evidence_message_ids');
    }
    const evidence = [...(draft.evidence_message_ids ?? draft.evidenceMessageIds)];
    if (evidence.length < 1 || evidence.length > MAX_EVIDENCE) {
        throw new RelationshipValidationError(`每条关系必须选择 1 到 ${MAX_EVIDENCE} 条证据消息`, 'evidence_message_ids');
    }
    if (new Set(evidence).size !== evidence.length) {
        throw new RelationshipValidationError('证据消息不能重复', 'evidence_message_ids');
    }
    if (evidence.some(id => !messageIds.has(id))) {
        throw new RelationshipValidationError('证据消息已不属于当前存档', 'evidence_message_ids');
    }
    return Object.freeze({
        source_character_id: source,
        target_character_id: target,
        relation_type: relationType,
        strength,
        evidence_message_ids: Object.freeze(evidence),
    });
}

function requireRef(ref) {
    if (!ref || !cleanId(ref.project) || !cleanId(ref.save)) {
        throw new RelationshipValidationError('关系操作缺少当前项目或存档');
    }
    return ref;
}

export function createRelationshipService(sessionWrite, endpoints) {
    if (typeof sessionWrite !== 'function') throw new TypeError('关系服务需要 sessionWrite');
    if (!endpoints || !endpoints.relationships) throw new TypeError('关系服务缺少 endpoint');
    return Object.freeze({
        save(ref, edge, originalKey = null) {
            requireRef(ref);
            return sessionWrite(endpoints.relationships, 'PUT', {
                project: ref.project,
                save: ref.save,
                edge,
                ...(originalKey ? { original_key: originalKey } : {}),
            }, '保存角色关系');
        },
        remove(ref, key) {
            requireRef(ref);
            return sessionWrite(endpoints.relationships, 'DELETE', {
                project: ref.project,
                save: ref.save,
                key,
            }, '删除角色关系');
        },
    });
}

export function createRelationshipOperationGate() {
    let owner = null;
    let serial = 0;
    return Object.freeze({
        acquire() {
            if (owner !== null) return null;
            owner = ++serial;
            return owner;
        },
        release(token) {
            if (token !== owner) return false;
            owner = null;
            return true;
        },
        isPending: () => owner !== null,
    });
}

export async function coordinateRelationshipEvidenceLocation({
    messageId,
    expectedRevision,
    loadAuthoritativeSession,
    assertCurrent,
    findMessage,
    locateTarget,
    releasePending,
    hideModal,
    focusTarget,
    makeError = (message, code) => new RelationshipNavigationError(message, code),
} = {}) {
    for (const [name, callback] of Object.entries({
        loadAuthoritativeSession,
        assertCurrent,
        findMessage,
        locateTarget,
        releasePending,
        hideModal,
        focusTarget,
    })) {
        if (typeof callback !== 'function') throw new TypeError(`证据定位缺少 ${name}`);
    }
    if (!isUuid(messageId) || !Number.isSafeInteger(expectedRevision) || expectedRevision < 0) {
        throw new RelationshipNavigationError('证据定位参数无效', 'invalid_relationship_evidence_ref');
    }

    const fail = (message, code) => { throw makeError(message, code); };
    assertCurrent();
    const authoritative = await loadAuthoritativeSession();
    assertCurrent();
    if (!isRecord(authoritative) || authoritative.revision !== expectedRevision) {
        fail('当前存档已更新，请重新打开关系图谱', 'revision_mismatch');
    }
    if (!findMessage(authoritative, messageId)) {
        fail('证据消息已不存在', 'message_not_found');
    }
    const target = locateTarget(messageId);
    if (!target) {
        fail('证据消息无法在当前对话中定位', 'message_target_not_found');
    }
    releasePending();
    if (!hideModal()) {
        fail('关系编辑器正在处理其他操作', 'relationship_editor_busy');
    }
    focusTarget(target);
    return true;
}

function element(documentRef, tag, className = '', value = '') {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (value !== '') node.textContent = value;
    return node;
}

function svgElement(documentRef, tag, attributes = {}) {
    const node = documentRef.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    return node;
}

function characterName(model, id) {
    const character = model.characters.find(item => item.id === id);
    return character ? character.name : id;
}

export function renderRelationshipGraph(documentRef, host, model) {
    const section = element(documentRef, 'section', 'relationship-graph-section');
    section.appendChild(element(documentRef, 'h3', 'relationship-section-title', '关系概览'));
    if (!model || model.edges.length === 0) {
        section.appendChild(element(documentRef, 'p', 'relationship-empty', '未记录'));
        host.appendChild(section);
        return section;
    }
    const svg = svgElement(documentRef, 'svg', {
        class: 'relationship-graph',
        viewBox: '0 0 720 420',
        role: 'img',
        'aria-labelledby': 'relationship-graph-title relationship-graph-desc',
    });
    const title = svgElement(documentRef, 'title', { id: 'relationship-graph-title' });
    title.textContent = '当前存档角色关系图';
    const desc = svgElement(documentRef, 'desc', { id: 'relationship-graph-desc' });
    desc.textContent = `${model.characters.length} 个角色，${model.edges.length} 条有向关系；完整文字信息见下方关系列表。`;
    svg.appendChild(title);
    svg.appendChild(desc);

    const defs = svgElement(documentRef, 'defs');
    const marker = svgElement(documentRef, 'marker', {
        id: 'relationship-arrow',
        viewBox: '0 0 10 10',
        refX: '9',
        refY: '5',
        markerWidth: '7',
        markerHeight: '7',
        orient: 'auto-start-reverse',
    });
    marker.appendChild(svgElement(documentRef, 'path', { d: 'M 0 0 L 10 5 L 0 10 z' }));
    defs.appendChild(marker);
    svg.appendChild(defs);

    const usedIds = new Set(model.edges.flatMap(edge => [edge.sourceCharacterId, edge.targetCharacterId]));
    const nodes = model.characters.filter(character => usedIds.has(character.id));
    const positions = new Map();
    const radius = Math.min(145, 52 + nodes.length * 11);
    nodes.forEach((character, index) => {
        const angle = (Math.PI * 2 * index / nodes.length) - Math.PI / 2;
        positions.set(character.id, {
            x: 360 + Math.cos(angle) * radius,
            y: 210 + Math.sin(angle) * radius,
        });
    });
    for (const edge of model.edges) {
        const from = positions.get(edge.sourceCharacterId);
        const to = positions.get(edge.targetCharacterId);
        if (!from || !to) continue;
        svg.appendChild(svgElement(documentRef, 'line', {
            x1: from.x,
            y1: from.y,
            x2: to.x,
            y2: to.y,
            class: 'relationship-edge-line',
            'stroke-width': 2 + edge.strength / 40,
            'marker-end': 'url(#relationship-arrow)',
        }));
        const label = svgElement(documentRef, 'text', {
            x: (from.x + to.x) / 2,
            y: (from.y + to.y) / 2 - 7,
            class: 'relationship-edge-label',
            'text-anchor': 'middle',
        });
        label.textContent = `${edge.relationType} · ${edge.strength}/100`;
        svg.appendChild(label);
    }
    for (const character of nodes) {
        const position = positions.get(character.id);
        const group = svgElement(documentRef, 'g', { class: 'relationship-node' });
        group.appendChild(svgElement(documentRef, 'circle', {
            cx: position.x,
            cy: position.y,
            r: 34,
        }));
        const label = svgElement(documentRef, 'text', {
            x: position.x,
            y: position.y + 5,
            'text-anchor': 'middle',
        });
        label.textContent = character.name.length > 8 ? `${character.name.slice(0, 8)}…` : character.name;
        group.appendChild(label);
        svg.appendChild(group);
    }
    section.appendChild(svg);
    host.appendChild(section);
    return section;
}

function appendDefinition(documentRef, list, termText, detailText) {
    const term = element(documentRef, 'dt', '', termText);
    const detail = element(documentRef, 'dd', '', detailText);
    list.appendChild(term);
    list.appendChild(detail);
}

function displayUpdatedAt(value) {
    if (!value) return '未记录';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? '未记录' : parsed.toLocaleString('zh-CN');
}

export function renderRelationshipList(documentRef, host, model, callbacks = {}) {
    const section = element(documentRef, 'section', 'relationship-list-section');
    section.appendChild(element(documentRef, 'h3', 'relationship-section-title', '关系列表'));
    if (!model || model.edges.length === 0) {
        section.appendChild(element(documentRef, 'p', 'relationship-empty', '未记录'));
        host.appendChild(section);
        return [];
    }
    const list = element(documentRef, 'ul', 'relationship-list');
    const cards = [];
    model.edges.forEach((edge, index) => {
        const item = element(documentRef, 'li', 'relationship-card');
        item.dataset.relationshipIndex = String(index);
        item.appendChild(element(
            documentRef,
            'h4',
            'relationship-card-title',
            `${characterName(model, edge.sourceCharacterId)} → ${characterName(model, edge.targetCharacterId)}`,
        ));
        const details = element(documentRef, 'dl', 'relationship-details');
        appendDefinition(documentRef, details, '关系类型', edge.relationType);
        appendDefinition(documentRef, details, '强度', `${edge.strength}/100`);
        appendDefinition(documentRef, details, '证据', `${edge.evidenceMessageIds.length} 条`);
        appendDefinition(documentRef, details, '更新时间', displayUpdatedAt(edge.updatedAt));
        item.appendChild(details);

        const evidence = element(documentRef, 'div', 'relationship-evidence-links');
        evidence.appendChild(element(documentRef, 'span', 'relationship-evidence-label', '证据消息：'));
        edge.evidenceMessageIds.forEach((messageId, evidenceIndex) => {
            const message = model.messages.find(itemMessage => itemMessage.id === messageId);
            const button = element(documentRef, 'button', 'relationship-evidence-button', `定位 ${evidenceIndex + 1}`);
            button.type = 'button';
            button.dataset.messageId = messageId;
            button.setAttribute(
                'aria-label',
                `定位证据消息 ${evidenceIndex + 1}：${messagePreview(message)}`,
            );
            button.addEventListener('click', () => {
                if (typeof callbacks.onLocateEvidence === 'function') callbacks.onLocateEvidence(messageId, button);
            });
            if (typeof callbacks.onControl === 'function') callbacks.onControl(button);
            evidence.appendChild(button);
        });
        item.appendChild(evidence);

        const actions = element(documentRef, 'div', 'relationship-card-actions');
        const edit = element(documentRef, 'button', 'relationship-write-control relationship-edit-button', '编辑');
        edit.type = 'button';
        edit.addEventListener('click', () => {
            if (typeof callbacks.onEdit === 'function') callbacks.onEdit(edge, edit);
        });
        const remove = element(documentRef, 'button', 'relationship-write-control relationship-delete-button', '删除');
        remove.type = 'button';
        remove.addEventListener('click', () => {
            if (typeof callbacks.onDelete === 'function') callbacks.onDelete(edge, remove);
        });
        if (typeof callbacks.onControl === 'function') {
            callbacks.onControl(edit);
            callbacks.onControl(remove);
        }
        actions.appendChild(edit);
        actions.appendChild(remove);
        item.appendChild(actions);
        list.appendChild(item);
        cards.push(item);
    });
    section.appendChild(list);
    host.appendChild(section);
    return cards;
}

function defaultDraft(model) {
    return {
        source_character_id: model.characters[0] ? model.characters[0].id : '',
        target_character_id: model.characters[1] ? model.characters[1].id : '',
        relation_type: '',
        strength: '50',
        evidence_message_ids: [],
    };
}

function draftFromEdge(edge) {
    return {
        source_character_id: edge.sourceCharacterId,
        target_character_id: edge.targetCharacterId,
        relation_type: edge.relationType,
        strength: String(edge.strength),
        evidence_message_ids: [...edge.evidenceMessageIds],
    };
}

function errorText(error) {
    return error instanceof Error && error.message ? error.message : String(error || '操作失败');
}

export function createRelationshipEditor({
    documentRef,
    session,
    sessionRef,
    service,
    isCurrent = () => true,
    isBlocked = () => false,
    onPendingChange = () => {},
    onLocateEvidence = async () => false,
    confirmDelete = edge => globalThis.confirm(`确认删除关系“${edge.relationType}”？`),
} = {}) {
    if (!documentRef || !service || !sessionRef) throw new TypeError('关系编辑器参数不完整');
    const root = element(documentRef, 'div', 'relationship-editor');
    root.setAttribute('aria-label', '当前存档角色关系编辑器');
    const gate = createRelationshipOperationGate();
    let currentSession = session;
    let currentModel = normalizeRelationshipSession(currentSession);
    let draft = defaultDraft(currentModel);
    let originalKey = null;
    let globallyDisabled = Boolean(isBlocked());
    let controls = [];
    let status = null;

    function connected() {
        return root.isConnected !== false;
    }

    function register(control) {
        control.classList.add('relationship-write-control');
        controls.push(control);
        return control;
    }

    function applyDisabled() {
        const disabled = globallyDisabled || gate.isPending() || isBlocked();
        for (const control of controls) {
            control.disabled = disabled;
            control.setAttribute('aria-disabled', String(disabled));
        }
    }

    async function runOperation(operation, successMessage) {
        const token = gate.acquire();
        if (token === null) return null;
        if (!isCurrent() || !connected()) {
            gate.release(token);
            return null;
        }
        if (isBlocked()) {
            globallyDisabled = true;
            if (status) status.textContent = '当前存档正在切换或生成，请等待完成后再操作。';
            gate.release(token);
            applyDisabled();
            return null;
        }
        applyDisabled();
        onPendingChange(true);
        try {
            const result = await operation();
            if (!isCurrent() || !connected()) return null;
            if (result && isRecord(result.session)) currentSession = result.session;
            originalKey = null;
            currentModel = normalizeRelationshipSession(currentSession);
            draft = defaultDraft(currentModel);
            render(successMessage);
            return result;
        } catch (error) {
            if (isCurrent() && connected() && status) {
                status.textContent = `操作失败：${errorText(error)}`;
                status.setAttribute('role', 'alert');
            }
            return null;
        } finally {
            if (gate.release(token)) {
                onPendingChange(false);
                applyDisabled();
            }
        }
    }

    function buildCharacterSelect(id, labelText, value) {
        const wrapper = element(documentRef, 'div', 'relationship-field');
        const label = element(documentRef, 'label', '', labelText);
        label.htmlFor = id;
        const select = register(element(documentRef, 'select', 'relationship-select'));
        select.id = id;
        for (const character of currentModel.characters) {
            const label = character.name === character.id
                ? character.name
                : `${character.name}（${character.id}）`;
            const option = element(documentRef, 'option', '', label);
            option.value = character.id;
            option.selected = character.id === value;
            select.appendChild(option);
        }
        wrapper.appendChild(label);
        wrapper.appendChild(select);
        return { wrapper, select };
    }

    function renderEvidenceChoices(host, selected, query = '') {
        host.replaceChildren();
        const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN');
        const matching = currentModel.messages.filter(message => (
            !normalizedQuery
            || messagePreview(message, 200).toLocaleLowerCase('zh-CN').includes(normalizedQuery)
            || message.id.includes(normalizedQuery)
        ));
        const selectedIds = new Set(selected);
        const visibleIds = new Set(matching.slice(-100).map(message => message.id));
        for (const messageId of selectedIds) visibleIds.add(messageId);
        const visible = currentModel.messages.filter(message => visibleIds.has(message.id));
        if (visible.length === 0) {
            host.appendChild(element(documentRef, 'p', 'relationship-evidence-empty', '没有匹配的当前存档消息'));
            return;
        }
        for (const message of visible) {
            const row = element(documentRef, 'label', 'relationship-evidence-option');
            const checkbox = register(element(documentRef, 'input', 'relationship-evidence-checkbox'));
            checkbox.type = 'checkbox';
            checkbox.value = message.id;
            checkbox.checked = selectedIds.has(message.id);
            row.appendChild(checkbox);
            const messageIndex = currentModel.messages.findIndex(item => item.id === message.id);
            const choice = element(documentRef, 'span', '', messageChoiceLabel(message, messageIndex));
            checkbox.setAttribute('aria-label', `${choice.textContent} · 完整 UUID ${message.id}`);
            row.appendChild(choice);
            host.appendChild(row);
        }
    }

    function buildForm() {
        const section = element(documentRef, 'section', 'relationship-form-section');
        section.appendChild(element(
            documentRef,
            'h3',
            'relationship-section-title',
            originalKey ? '编辑关系' : '新增关系',
        ));
        if (currentModel.characters.length < 2) {
            section.appendChild(element(documentRef, 'p', 'relationship-empty', '至少需要两个当前存档角色才能记录关系。'));
            return section;
        }
        const form = element(documentRef, 'form', 'relationship-form');
        form.noValidate = true;
        const sourceField = buildCharacterSelect('relationship-source', '来源角色', draft.source_character_id);
        const targetField = buildCharacterSelect('relationship-target', '目标角色', draft.target_character_id);
        form.appendChild(sourceField.wrapper);
        form.appendChild(targetField.wrapper);

        const typeField = element(documentRef, 'div', 'relationship-field');
        const typeLabel = element(documentRef, 'label', '', '关系类型');
        typeLabel.htmlFor = 'relationship-type';
        const typeInput = register(element(documentRef, 'input', 'relationship-type-input'));
        typeInput.id = 'relationship-type';
        typeInput.type = 'text';
        typeInput.maxLength = MAX_RELATION_TYPE;
        typeInput.value = draft.relation_type;
        typeInput.autocomplete = 'off';
        typeField.appendChild(typeLabel);
        typeField.appendChild(typeInput);
        form.appendChild(typeField);

        const strengthField = element(documentRef, 'div', 'relationship-field');
        const strengthLabel = element(documentRef, 'label', '', '关系强度（0–100）');
        strengthLabel.htmlFor = 'relationship-strength';
        const strengthInput = register(element(documentRef, 'input', 'relationship-strength-input'));
        strengthInput.id = 'relationship-strength';
        strengthInput.type = 'number';
        strengthInput.min = '0';
        strengthInput.max = '100';
        strengthInput.step = '1';
        strengthInput.inputMode = 'numeric';
        strengthInput.value = draft.strength;
        strengthField.appendChild(strengthLabel);
        strengthField.appendChild(strengthInput);
        form.appendChild(strengthField);

        const fieldset = element(documentRef, 'fieldset', 'relationship-evidence-fieldset');
        fieldset.appendChild(element(documentRef, 'legend', '', '证据消息（1–20 条）'));
        const filterLabel = element(documentRef, 'label', 'relationship-evidence-filter-label', '筛选当前存档消息');
        filterLabel.htmlFor = 'relationship-evidence-filter';
        const filter = register(element(documentRef, 'input', 'relationship-evidence-filter'));
        filter.id = 'relationship-evidence-filter';
        filter.type = 'search';
        filter.placeholder = '输入消息内容或 UUID';
        const evidenceHost = element(documentRef, 'div', 'relationship-evidence-options');
        fieldset.appendChild(filterLabel);
        fieldset.appendChild(filter);
        fieldset.appendChild(evidenceHost);
        form.appendChild(fieldset);
        renderEvidenceChoices(evidenceHost, draft.evidence_message_ids);
        filter.addEventListener('input', () => {
            const checked = Array.from(evidenceHost.querySelectorAll('input[type="checkbox"]:checked'))
                .map(input => input.value);
            draft.evidence_message_ids = checked;
            controls = controls.filter(control => !control.classList.contains('relationship-evidence-checkbox'));
            renderEvidenceChoices(evidenceHost, draft.evidence_message_ids, filter.value);
            applyDisabled();
        });

        const actions = element(documentRef, 'div', 'relationship-form-actions');
        const save = register(element(documentRef, 'button', 'relationship-save-button', originalKey ? '保存修改' : '新增关系'));
        save.type = 'submit';
        actions.appendChild(save);
        if (originalKey) {
            const cancel = register(element(documentRef, 'button', 'relationship-cancel-edit-button', '取消编辑'));
            cancel.type = 'button';
            cancel.addEventListener('click', () => {
                originalKey = null;
                draft = defaultDraft(currentModel);
                render();
            });
            actions.appendChild(cancel);
        }
        form.appendChild(actions);
        form.addEventListener('submit', event => {
            event.preventDefault();
            const evidence = Array.from(evidenceHost.querySelectorAll('input[type="checkbox"]:checked'))
                .map(input => input.value);
            const rawDraft = {
                source_character_id: sourceField.select.value,
                target_character_id: targetField.select.value,
                relation_type: typeInput.value,
                strength: strengthInput.value,
                evidence_message_ids: evidence,
            };
            let edge;
            try {
                edge = validateRelationshipDraft(rawDraft, currentSession);
                for (const input of [sourceField.select, targetField.select, typeInput, strengthInput]) {
                    input.removeAttribute('aria-invalid');
                }
            } catch (error) {
                if (status) status.textContent = errorText(error);
                const fieldMap = {
                    source_character_id: sourceField.select,
                    target_character_id: targetField.select,
                    relation_type: typeInput,
                    strength: strengthInput,
                    evidence_message_ids: fieldset,
                };
                const target = fieldMap[error.field] || typeInput;
                target.setAttribute('aria-invalid', 'true');
                if (typeof target.focus === 'function') target.focus();
                return;
            }
            draft = rawDraft;
            void runOperation(
                () => service.save(sessionRef, edge, originalKey),
                originalKey ? '关系已更新。' : '关系已记录。',
            );
        });
        section.appendChild(form);
        return section;
    }

    function render(message = '') {
        currentModel = normalizeRelationshipSession(currentSession);
        controls = [];
        root.replaceChildren();
        const note = element(
            documentRef,
            'p',
            'relationship-scope-note',
            '只展示人工记录的结构化关系；不会从亲密度、心情或剧情总结推断关系。',
        );
        root.appendChild(note);
        renderRelationshipGraph(documentRef, root, currentModel);
        renderRelationshipList(documentRef, root, currentModel, {
            onControl(control) {
                controls.push(control);
            },
            onEdit(edge) {
                if (gate.isPending() || globallyDisabled || isBlocked() || !isCurrent() || !connected()) return;
                originalKey = relationshipKey(edge);
                draft = draftFromEdge(edge);
                render();
                const input = root.querySelector('#relationship-type');
                if (input && typeof input.focus === 'function') input.focus();
            },
            onDelete(edge) {
                if (gate.isPending() || globallyDisabled || isBlocked() || !isCurrent() || !connected()) return;
                if (!confirmDelete(edge)) return;
                void runOperation(
                    () => service.remove(sessionRef, relationshipKey(edge)),
                    '关系已删除。',
                );
            },
            onLocateEvidence(messageId) {
                if (gate.isPending() || globallyDisabled || isBlocked() || !isCurrent() || !connected()) return;
                void runOperation(
                    () => onLocateEvidence(messageId),
                    '',
                );
            },
        });
        root.appendChild(buildForm());
        status = element(documentRef, 'div', 'relationship-status', message);
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        root.appendChild(status);
        applyDisabled();
    }

    render();
    if (isBlocked()) {
        globallyDisabled = true;
        applyDisabled();
    }
    return Object.freeze({
        root,
        setDisabled(value) {
            globallyDisabled = Boolean(value);
            applyDisabled();
        },
        isDisabled: () => globallyDisabled,
        model: () => currentModel,
        gate,
    });
}
