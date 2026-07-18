const WARNING_CODES = new Set([
    'muted_character_output',
    'ambiguous_character_identity',
    'unknown_character_identity',
]);
const WARNING_ACTIONS = new Set([
    'writeback_applied',
    'writeback_skipped',
    'unresolved_skipped',
]);

export class RoleplayValidationError extends TypeError {
    constructor(message, field = '') {
        super(message);
        this.name = 'RoleplayValidationError';
        this.field = field;
    }
}

function isRecord(value) {
    return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function requireSessionRef(ref) {
    if (!ref || typeof ref.project !== 'string' || !ref.project
        || typeof ref.save !== 'string' || !ref.save) {
        throw new RoleplayValidationError('角色扮演设置缺少当前项目或存档');
    }
    return ref;
}

export function normalizeRemainingSilentTurns(value, { strict = false } = {}) {
    if (value === undefined || value === null || value === '') return 0;
    const number = typeof value === 'number' ? value : Number(String(value).trim());
    if (!Number.isSafeInteger(number) || number < 0 || number > 999) {
        if (strict) throw new RoleplayValidationError('禁言轮次必须是 0 到 999 的整数', 'remaining_silent_turns');
        return 0;
    }
    return number;
}

export function normalizeRoleplaySession(session, { strict = false } = {}) {
    const source = isRecord(session) ? session : {};
    if (strict && !isRecord(session)) throw new RoleplayValidationError('Session 必须是对象');
    const policy = isRecord(source.roleplay_policy) ? source.roleplay_policy : {};
    if (strict && policy.strict_muted_writeback !== undefined
        && typeof policy.strict_muted_writeback !== 'boolean') {
        throw new RoleplayValidationError('严格禁言写回必须是布尔值', 'strict_muted_writeback');
    }
    const states = isRecord(source.characters_state) ? source.characters_state : {};
    const characters = Object.entries(states).map(([id, raw]) => {
        const value = isRecord(raw) ? raw : {};
        return Object.freeze({
            id,
            name: typeof value.name === 'string' && value.name.trim() ? value.name.trim() : id,
            mood: typeof value.mood === 'string' ? value.mood : '',
            affinity: Number.isFinite(Number(value.affinity))
                ? Math.min(100, Math.max(0, Number(value.affinity)))
                : 0,
            remainingSilentTurns: normalizeRemainingSilentTurns(
                value.remaining_silent_turns,
                { strict },
            ),
        });
    });
    return Object.freeze({
        strictMutedWriteback: policy.strict_muted_writeback === true,
        characters: Object.freeze(characters),
    });
}

function cleanId(value) {
    return typeof value === 'string' && value.trim() ? value.trim().slice(0, 256) : '';
}

export function sanitizeRoleplayWarning(value) {
    if (!isRecord(value) || !WARNING_CODES.has(value.code) || !WARNING_ACTIONS.has(value.action)) return null;
    const warning = { code: value.code, action: value.action };
    if (value.code === 'muted_character_output') {
        if (!['writeback_applied', 'writeback_skipped'].includes(value.action)) return null;
        const characterId = cleanId(value.character_id);
        if (!characterId) return null;
        warning.characterId = characterId;
    } else if (value.code === 'ambiguous_character_identity') {
        if (value.action !== 'unresolved_skipped' || !Array.isArray(value.candidate_ids)) return null;
        const candidateIds = [...new Set(value.candidate_ids.map(cleanId).filter(Boolean))].sort().slice(0, 64);
        if (candidateIds.length === 0) return null;
        warning.candidateIds = candidateIds;
    } else if (value.action !== 'unresolved_skipped') {
        return null;
    }
    return Object.freeze(warning);
}

function warningArrayFrom(source) {
    if (Array.isArray(source)) return source;
    if (!isRecord(source)) return [];
    if (Array.isArray(source.roleplay_warnings)) return source.roleplay_warnings;
    if (isRecord(source.metadata) && Array.isArray(source.metadata.roleplay_warnings)) {
        return source.metadata.roleplay_warnings;
    }
    if (isRecord(source.parsed) && Array.isArray(source.parsed.roleplay_warnings)) {
        return source.parsed.roleplay_warnings;
    }
    return [];
}

export function sanitizeRoleplayWarnings(source) {
    return warningArrayFrom(source).map(sanitizeRoleplayWarning).filter(Boolean);
}

export function formatRoleplayWarning(warning) {
    if (!warning) return '';
    if (warning.code === 'muted_character_output' && warning.action === 'writeback_applied') {
        return `角色「${warning.characterId}」仍在禁言期：已警告但按宽松模式写回状态。`;
    }
    if (warning.code === 'muted_character_output' && warning.action === 'writeback_skipped') {
        return `角色「${warning.characterId}」仍在禁言期：严格模式已跳过该角色写回。`;
    }
    if (warning.code === 'ambiguous_character_identity') {
        return `角色身份对应多个候选（${warning.candidateIds.join('、')}），本次写回已跳过。`;
    }
    if (warning.code === 'unknown_character_identity') {
        return '角色身份无法解析，本次写回已跳过。';
    }
    return '';
}

export function renderRoleplayWarnings(documentRef, host, source) {
    if (!documentRef || !host || typeof host.appendChild !== 'function') return null;
    const warnings = sanitizeRoleplayWarnings(source);
    if (warnings.length === 0) return null;
    const panel = documentRef.createElement('section');
    panel.className = 'roleplay-warning-panel';
    panel.setAttribute('role', 'status');
    panel.setAttribute('aria-live', 'polite');
    const title = documentRef.createElement('div');
    title.className = 'roleplay-warning-title';
    title.textContent = '角色写回提示';
    const list = documentRef.createElement('ul');
    for (const warning of warnings) {
        const item = documentRef.createElement('li');
        item.className = warning.action === 'writeback_applied'
            ? 'roleplay-warning-applied'
            : 'roleplay-warning-skipped';
        item.textContent = formatRoleplayWarning(warning);
        list.appendChild(item);
    }
    panel.appendChild(title);
    panel.appendChild(list);
    host.appendChild(panel);
    return panel;
}

export function createRoleplayService(sessionWrite, endpoints) {
    if (typeof sessionWrite !== 'function') throw new TypeError('角色扮演服务需要 sessionWrite');
    if (!endpoints || typeof endpoints.roleplaySilence !== 'function' || !endpoints.roleplayPolicy) {
        throw new TypeError('角色扮演服务缺少 endpoints');
    }
    return Object.freeze({
        saveSilence(ref, characterId, remainingSilentTurns) {
            requireSessionRef(ref);
            const id = cleanId(characterId);
            if (!id) throw new RoleplayValidationError('角色 ID 无效', 'character_id');
            const remaining = normalizeRemainingSilentTurns(remainingSilentTurns, { strict: true });
            return sessionWrite(endpoints.roleplaySilence(id), 'PATCH', {
                project: ref.project,
                save: ref.save,
                remaining_silent_turns: remaining,
            }, `保存角色「${id}」禁言轮次`);
        },
        savePolicy(ref, strictMutedWriteback) {
            requireSessionRef(ref);
            if (typeof strictMutedWriteback !== 'boolean') {
                throw new RoleplayValidationError('严格禁言写回必须是布尔值', 'strict_muted_writeback');
            }
            return sessionWrite(endpoints.roleplayPolicy, 'PATCH', {
                project: ref.project,
                save: ref.save,
                strict_muted_writeback: strictMutedWriteback,
            }, '保存严格禁言写回设置');
        },
    });
}

function element(documentRef, tag, className = '', text = '') {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
}

function affinityText(value) {
    const percent = Math.round(Math.min(100, Math.max(0, Number(value) || 0)));
    const filled = Math.round(percent / 10);
    return `${'█'.repeat(filled)}${'░'.repeat(10 - filled)} ${percent}%`;
}

function errorMessage(error) {
    return error instanceof Error && error.message ? error.message : String(error || '保存失败');
}

export function createRoleplayPanel({
    documentRef,
    container,
    session,
    sessionRef,
    service,
    disabled = false,
    isCurrent = () => true,
    isTurnActive = () => false,
    recoverDraft = null,
} = {}) {
    if (!documentRef || !container || !service) throw new TypeError('角色状态面板参数不完整');
    const normalized = normalizeRoleplaySession(session);
    const root = element(documentRef, 'div', 'roleplay-panel');
    root.setAttribute('aria-label', '当前存档角色控制');
    root.appendChild(element(
        documentRef,
        'p',
        'roleplay-scope-note',
        '本区只保存当前存档的禁言状态；项目共享的“发言倾向”请从顶部“角色”编辑。',
    ));
    const elements = { characters: new Map(), policy: null };
    let globallyDisabled = Boolean(disabled || isTurnActive());
    const pendingControls = new Set();
    const writeControls = [];

    function connected(node) {
        return node && node.isConnected !== false;
    }

    function applyDisabled() {
        for (const control of writeControls) {
            control.disabled = globallyDisabled || pendingControls.has(control.dataset.pendingGroup || '');
            control.setAttribute('aria-disabled', String(control.disabled));
        }
    }

    function register(control, group) {
        control.classList.add('roleplay-write-control');
        control.dataset.pendingGroup = group;
        writeControls.push(control);
        return control;
    }

    const policy = element(documentRef, 'section', 'roleplay-policy');
    const policyHeading = element(documentRef, 'h4', 'roleplay-section-title', '禁言写回策略');
    const policyDescription = element(
        documentRef,
        'p',
        'roleplay-help',
        '开启后，禁言角色若仍被模型输出，将整角色跳过写回；关闭时会警告但保留写回。',
    );
    const policyRow = element(documentRef, 'div', 'roleplay-policy-row');
    const policyInput = register(element(documentRef, 'input', 'roleplay-policy-checkbox'), 'policy');
    policyInput.type = 'checkbox';
    policyInput.id = 'roleplay-strict-muted-writeback';
    policyInput.checked = normalized.strictMutedWriteback;
    const policyLabel = element(documentRef, 'label', '', '严格禁言写回');
    policyLabel.htmlFor = policyInput.id;
    const policySave = register(element(documentRef, 'button', 'roleplay-save-button', '保存策略'), 'policy');
    policySave.type = 'button';
    const policyStatus = element(documentRef, 'div', 'roleplay-inline-status');
    policyStatus.setAttribute('role', 'status');
    policyStatus.setAttribute('aria-live', 'polite');
    policyRow.appendChild(policyInput);
    policyRow.appendChild(policyLabel);
    policyRow.appendChild(policySave);
    policy.appendChild(policyHeading);
    policy.appendChild(policyDescription);
    policy.appendChild(policyRow);
    policy.appendChild(policyStatus);
    root.appendChild(policy);
    elements.policy = { checkbox: policyInput, label: policyLabel, save: policySave, status: policyStatus };

    policySave.addEventListener('click', async () => {
        if (globallyDisabled || pendingControls.has('policy')) return;
        if (!isCurrent()) {
            policyStatus.textContent = '保存失败：当前项目或存档已切换，请刷新后重试。';
            policyInput.focus();
            return;
        }
        if (isTurnActive()) {
            globallyDisabled = true;
            applyDisabled();
            policyStatus.textContent = '当前存档正在生成，请等待完成后再保存。';
            return;
        }
        pendingControls.add('policy');
        policyStatus.textContent = '保存中…';
        applyDisabled();
        try {
            await service.savePolicy(sessionRef, policyInput.checked);
            if (!isCurrent() || !connected(policyInput)) return;
            policyStatus.textContent = isTurnActive()
                ? '策略已保存；生成已开始，控件已锁定。'
                : '策略已保存。';
            if (isTurnActive()) globallyDisabled = true;
        } catch (error) {
            const message = `保存失败：${errorMessage(error)}`;
            if (!connected(policyInput)) {
                if (isCurrent() && typeof recoverDraft === 'function') {
                    recoverDraft({ kind: 'policy', value: policyInput.checked, message });
                }
                return;
            }
            policyStatus.textContent = message;
            policyInput.focus();
        } finally {
            pendingControls.delete('policy');
            applyDisabled();
        }
    });

    const charactersHeading = element(documentRef, 'h4', 'roleplay-section-title', '角色状态');
    root.appendChild(charactersHeading);
    if (normalized.characters.length === 0) {
        root.appendChild(element(documentRef, 'p', 'empty', '无角色数据'));
    }

    normalized.characters.forEach((character, index) => {
        const group = `character-${index}`;
        const card = element(documentRef, 'section', 'panel-char roleplay-character');
        card.dataset.characterId = character.id;
        const name = element(documentRef, 'div', 'name', character.name);
        const affinity = element(documentRef, 'div', 'affinity-bar', affinityText(character.affinity));
        card.appendChild(name);
        card.appendChild(affinity);
        if (character.mood) card.appendChild(element(documentRef, 'div', 'roleplay-mood', `心情：${character.mood}`));
        const current = element(
            documentRef,
            'p',
            'roleplay-silence-current',
            character.remainingSilentTurns > 0
                ? `还需禁言 ${character.remainingSilentTurns} 个成功新轮次`
                : '当前允许发言',
        );
        card.appendChild(current);

        const controls = element(documentRef, 'div', 'roleplay-silence-controls');
        const inputId = `roleplay-silence-${index}`;
        const label = element(documentRef, 'label', '', '禁言剩余成功轮次');
        label.htmlFor = inputId;
        const input = register(element(documentRef, 'input', 'roleplay-silence-input'), group);
        input.type = 'number';
        input.id = inputId;
        input.min = '0';
        input.max = '999';
        input.step = '1';
        input.inputMode = 'numeric';
        input.value = String(character.remainingSilentTurns);
        const save = register(element(documentRef, 'button', 'roleplay-save-button', '保存禁言'), group);
        save.type = 'button';
        controls.appendChild(label);
        controls.appendChild(input);
        controls.appendChild(save);
        card.appendChild(controls);
        const status = element(documentRef, 'div', 'roleplay-inline-status');
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        card.appendChild(status);
        root.appendChild(card);
        elements.characters.set(character.id, { card, label, input, save, status, current });

        save.addEventListener('click', async () => {
            if (globallyDisabled || pendingControls.has(group)) return;
            let remaining;
            try {
                remaining = normalizeRemainingSilentTurns(input.value, { strict: true });
            } catch (error) {
                status.textContent = errorMessage(error);
                input.setAttribute('aria-invalid', 'true');
                input.focus();
                return;
            }
            input.removeAttribute('aria-invalid');
            if (!isCurrent()) {
                status.textContent = '保存失败：当前项目或存档已切换，请刷新后重试。';
                input.focus();
                return;
            }
            if (isTurnActive()) {
                globallyDisabled = true;
                applyDisabled();
                status.textContent = '当前存档正在生成，请等待完成后再保存。';
                return;
            }
            pendingControls.add(group);
            status.textContent = '保存中…';
            applyDisabled();
            try {
                await service.saveSilence(sessionRef, character.id, remaining);
                if (!isCurrent() || !connected(input)) return;
                input.value = String(remaining);
                current.textContent = remaining > 0
                    ? `还需禁言 ${remaining} 个成功新轮次`
                    : '当前允许发言';
                status.textContent = isTurnActive()
                    ? '禁言轮次已保存；生成已开始，控件已锁定。'
                    : '禁言轮次已保存。';
                if (isTurnActive()) globallyDisabled = true;
            } catch (error) {
                const message = `保存失败：${errorMessage(error)}`;
                if (!connected(input)) {
                    if (isCurrent() && typeof recoverDraft === 'function') {
                        recoverDraft({
                            kind: 'silence',
                            characterId: character.id,
                            value: input.value,
                            message,
                        });
                    }
                    return;
                }
                status.textContent = message;
                input.focus();
            } finally {
                pendingControls.delete(group);
                applyDisabled();
            }
        });
    });

    if (typeof container.replaceChildren === 'function') container.replaceChildren(root);
    else {
        while (container.firstChild) container.removeChild(container.firstChild);
        container.appendChild(root);
    }
    applyDisabled();
    // 构建期间生成状态可能已改变；在所有新控件已经进入 DOM 后再次封闭竞态窗口。
    if (isTurnActive()) {
        globallyDisabled = true;
        applyDisabled();
    }

    return Object.freeze({
        root,
        elements,
        setDisabled(value) {
            globallyDisabled = Boolean(value);
            applyDisabled();
        },
        isDisabled: () => globallyDisabled,
    });
}
