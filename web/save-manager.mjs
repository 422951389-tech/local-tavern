function text(value) {
    return typeof value === 'string' ? value.trim() : '';
}

function element(documentRef, tag, className = '', value = '') {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (value) node.textContent = value;
    return node;
}

function formatUpdatedAt(value) {
    if (!value) return '尚未记录更新时间';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '尚未记录更新时间';
    return `更新于 ${parsed.toLocaleString('zh-CN', { hour12: false })}`;
}

export function normalizeSaveManagerState(value = {}) {
    const saves = Array.isArray(value.saves) ? value.saves.flatMap(raw => {
        if (!raw || typeof raw !== 'object') return [];
        const id = text(raw.session_id);
        if (!id) return [];
        return [{
            id,
            name: text(raw.name) || '未命名存档',
            messageCount: Number.isSafeInteger(raw.message_count) && raw.message_count >= 0
                ? raw.message_count
                : 0,
            updatedAt: text(raw.updated_at),
        }];
    }) : [];
    return Object.freeze({
        project: text(value.project) || '当前世界',
        currentSaveId: text(value.currentSaveId),
        currentModel: text(value.currentModel),
        saves: Object.freeze(saves.map(item => Object.freeze(item))),
    });
}

export function createSaveManager(options = {}) {
    const documentRef = options.documentRef;
    if (!documentRef || typeof documentRef.createElement !== 'function') {
        throw new TypeError('存档管理器缺少 document');
    }
    for (const callback of ['getState', 'onSwitch', 'onCreate', 'onRename', 'onDelete', 'onExport', 'onImport']) {
        if (typeof options[callback] !== 'function') throw new TypeError(`存档管理器缺少 ${callback}`);
    }

    const root = element(documentRef, 'section', 'save-manager story-module');
    root.setAttribute('aria-label', '故事存档管理器');
    let snapshot = normalizeSaveManagerState(options.getState());
    let pending = false;
    let liveMessage = '';
    let liveError = false;

    function setPending(value) {
        pending = Boolean(value);
        if (typeof options.onPendingChange === 'function') options.onPendingChange(pending);
    }

    async function runOperation(operation, successMessage, focusSelector = '') {
        if (pending) return false;
        setPending(true);
        liveMessage = '正在处理…';
        liveError = false;
        render();
        try {
            const result = await operation();
            if (result === false || result === null) {
                liveMessage = '';
                return false;
            }
            snapshot = normalizeSaveManagerState(options.getState());
            liveMessage = successMessage;
            liveError = false;
            return true;
        } catch (error) {
            const detail = error instanceof Error && error.message ? error.message : String(error || '未知错误');
            liveMessage = `操作失败：${detail}`;
            liveError = true;
            return false;
        } finally {
            setPending(false);
            render();
            const target = focusSelector ? root.querySelector(focusSelector) : null;
            if (target && typeof target.focus === 'function') target.focus();
        }
    }

    function renderSaveCard(save) {
        const current = save.id === snapshot.currentSaveId;
        const card = element(documentRef, 'article', `save-manager-card${current ? ' is-current' : ''}`);
        const header = element(documentRef, 'header', 'save-manager-card-header');
        const titleWrap = element(documentRef, 'div', 'save-manager-card-title');
        titleWrap.appendChild(element(documentRef, 'h3', '', save.name));
        titleWrap.appendChild(element(
            documentRef,
            'p',
            '',
            `${save.messageCount} 条对话 · ${formatUpdatedAt(save.updatedAt)}`,
        ));
        header.appendChild(titleWrap);
        if (current) header.appendChild(element(documentRef, 'span', 'story-status-badge is-active', '当前存档'));
        card.appendChild(header);

        if (!current) {
            const switchButton = element(documentRef, 'button', 'modal-btn save-manager-switch', '切换到这个存档');
            switchButton.type = 'button';
            switchButton.disabled = pending;
            switchButton.addEventListener('click', () => {
                void runOperation(() => options.onSwitch(save.id), `已切换到「${save.name}」`, '.save-manager-create-input');
            });
            card.appendChild(switchButton);
            return card;
        }

        const details = element(documentRef, 'div', 'save-manager-current-details');
        details.appendChild(element(
            documentRef,
            'p',
            'save-manager-model',
            snapshot.currentModel ? `当前模型：${snapshot.currentModel}` : '当前模型尚未记录',
        ));
        const renameForm = element(documentRef, 'form', 'save-manager-rename-form');
        const renameLabel = element(documentRef, 'label', '', '存档名称');
        renameLabel.htmlFor = 'save-manager-rename';
        const renameInput = element(documentRef, 'input', 'save-manager-control');
        renameInput.id = 'save-manager-rename';
        renameInput.name = 'save_name';
        renameInput.value = save.name;
        renameInput.maxLength = 200;
        renameInput.disabled = pending;
        const renameButton = element(documentRef, 'button', 'modal-btn', '保存名称');
        renameButton.type = 'submit';
        renameButton.disabled = pending;
        renameForm.append(renameLabel, renameInput, renameButton);
        renameForm.addEventListener('submit', event => {
            event.preventDefault();
            const nextName = renameInput.value.trim();
            if (!nextName || nextName === save.name) {
                renameInput.focus();
                return;
            }
            void runOperation(() => options.onRename(nextName), '存档名称已保存', '#save-manager-rename');
        });
        details.appendChild(renameForm);

        const actions = element(documentRef, 'div', 'save-manager-card-actions');
        const exportButton = element(documentRef, 'button', 'modal-btn', '导出备份');
        exportButton.type = 'button';
        exportButton.disabled = pending;
        exportButton.addEventListener('click', () => {
            void runOperation(options.onExport, '存档已导出', '.save-manager-export');
        });
        exportButton.classList.add('save-manager-export');
        const deleteButton = element(documentRef, 'button', 'modal-btn danger', '移入回收区');
        deleteButton.type = 'button';
        deleteButton.disabled = pending || snapshot.saves.length <= 1;
        deleteButton.title = snapshot.saves.length <= 1 ? '至少保留一个存档' : '';
        deleteButton.addEventListener('click', () => {
            void runOperation(options.onDelete, '存档已移入回收区', '.save-manager-create-input');
        });
        actions.append(exportButton, deleteButton);
        details.appendChild(actions);
        card.appendChild(details);
        return card;
    }

    function render() {
        root.replaceChildren();
        const heading = element(documentRef, 'header', 'story-module-heading save-manager-heading');
        const headingCopy = element(documentRef, 'div');
        headingCopy.appendChild(element(documentRef, 'span', 'story-module-kicker', 'STORY SAVES'));
        headingCopy.appendChild(element(documentRef, 'h2', '', '故事存档'));
        headingCopy.appendChild(element(
            documentRef,
            'p',
            '',
            `管理「${snapshot.project}」中的独立故事进度。切换存档不会修改其他故事。`,
        ));
        heading.appendChild(headingCopy);
        heading.appendChild(element(documentRef, 'span', 'story-scope-badge', `${snapshot.saves.length} 个存档`));
        root.appendChild(heading);

        const tools = element(documentRef, 'div', 'save-manager-tools');
        const createForm = element(documentRef, 'form', 'save-manager-create-form');
        const createLabel = element(documentRef, 'label', '', '新存档名称');
        createLabel.htmlFor = 'save-manager-create';
        const createInput = element(documentRef, 'input', 'save-manager-control save-manager-create-input');
        createInput.id = 'save-manager-create';
        createInput.placeholder = '例如：主线剧情、支线 A';
        createInput.maxLength = 200;
        createInput.disabled = pending;
        const createButton = element(documentRef, 'button', 'modal-btn primary', pending ? '处理中…' : '新建存档');
        createButton.type = 'submit';
        createButton.disabled = pending;
        createForm.append(createLabel, createInput, createButton);
        createForm.addEventListener('submit', event => {
            event.preventDefault();
            const name = createInput.value.trim();
            if (!name) {
                createInput.setAttribute('aria-invalid', 'true');
                createInput.focus();
                return;
            }
            void runOperation(() => options.onCreate(name), `已创建「${name}」`, '.save-manager-create-input');
        });
        tools.appendChild(createForm);

        const importWrap = element(documentRef, 'div', 'save-manager-import');
        const importLabel = element(documentRef, 'label', 'modal-btn save-manager-import-label', '导入存档');
        importLabel.htmlFor = 'save-manager-import-file';
        const importInput = element(documentRef, 'input', 'save-manager-import-input');
        importInput.id = 'save-manager-import-file';
        importInput.type = 'file';
        importInput.accept = '.json,application/json';
        importInput.disabled = pending;
        importInput.addEventListener('change', () => {
            const file = importInput.files && importInput.files[0];
            if (file) void runOperation(() => options.onImport(file), '存档已导入', '.save-manager-create-input');
        });
        importWrap.append(importLabel, importInput);
        tools.appendChild(importWrap);
        root.appendChild(tools);

        const list = element(documentRef, 'div', 'save-manager-list');
        list.setAttribute('aria-label', '存档列表');
        if (snapshot.saves.length === 0) {
            list.appendChild(element(documentRef, 'p', 'story-empty-state', '还没有存档，请先创建一个故事。'));
        } else {
            for (const save of snapshot.saves) list.appendChild(renderSaveCard(save));
        }
        root.appendChild(list);

        const status = element(
            documentRef,
            'p',
            `story-module-status save-manager-status${liveError ? ' is-error' : ''}`,
            liveMessage,
        );
        status.setAttribute('role', liveError ? 'alert' : 'status');
        status.setAttribute('aria-live', liveError ? 'assertive' : 'polite');
        root.appendChild(status);
    }

    render();
    return Object.freeze({
        root,
        refresh() {
            snapshot = normalizeSaveManagerState(options.getState());
            render();
        },
        isPending: () => pending,
    });
}
