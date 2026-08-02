const CATEGORIES = Object.freeze({
    general: '通用', location: '地点', faction: '势力', rule: '规则',
    history: '历史', culture: '文化', item: '物品', secret: '秘密',
});
const CHANGE_CATEGORIES = Object.freeze({
    event: '事件', location: '地点', faction: '势力', rule: '规则', item: '物品', other: '其他',
});
const CHANGE_STATUSES = Object.freeze({ active: '生效中', resolved: '已解决', retconned: '已推翻' });

function record(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function text(value, fallback = '') {
    return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

function stringArray(value) {
    if (!Array.isArray(value)) return [];
    return [...new Set(value.filter(item => typeof item === 'string').map(item => item.trim()).filter(Boolean))];
}

function diagnosticMatches(value) {
    const source = record(value);
    const rows = Array.isArray(source.worldbook_matches) ? source.worldbook_matches : [];
    return rows.flatMap(raw => {
        const item = record(raw);
        const id = text(item.id);
        if (!id) return [];
        return [Object.freeze({
            id,
            title: text(item.title, id),
            summary: text(item.summary),
            category: Object.hasOwn(CATEGORIES, item.category) ? item.category : 'general',
            visibility: ['public', 'discovered', 'hidden'].includes(item.visibility) ? item.visibility : 'public',
            knowledgeScope: ['global', 'narrator', 'characters'].includes(item.knowledge_scope)
                ? item.knowledge_scope : 'global',
            activated: item.activated === true,
            kept: item.kept === true,
            trigger: text(item.trigger),
            reason: text(item.reason),
        })];
    });
}

export function normalizeWorldState(value) {
    const source = record(value);
    const changes = Array.isArray(source.changes) ? source.changes.flatMap(raw => {
        const item = record(raw);
        const id = text(item.id);
        const title = text(item.title);
        if (!id || !title) return [];
        return [Object.freeze({
            id,
            title,
            detail: text(item.detail),
            category: Object.hasOwn(CHANGE_CATEGORIES, item.category) ? item.category : 'other',
            status: Object.hasOwn(CHANGE_STATUSES, item.status) ? item.status : 'active',
            relatedEntryIds: Object.freeze(stringArray(item.related_entry_ids)),
            evidenceMessageIds: Object.freeze(stringArray(item.evidence_message_ids)),
            createdAt: text(item.created_at),
            updatedAt: text(item.updated_at),
        })];
    }) : [];
    return Object.freeze({
        discoveredEntryIds: Object.freeze(stringArray(source.discovered_entry_ids)),
        changes: Object.freeze(changes),
    });
}

export function buildWorldContextModel(session, diagnostics) {
    const current = record(session);
    const scene = record(current.scene_meta);
    const worldState = normalizeWorldState(current.world_state);
    const matches = diagnosticMatches(diagnostics);
    const visibleMatches = matches.filter(item => item.visibility !== 'hidden'
        && (item.visibility !== 'discovered' || worldState.discoveredEntryIds.includes(item.id)));
    return Object.freeze({
        location: text(scene.location, '地点未设定'),
        time: text(scene.time, text(scene.period, '时间未设定')),
        active: Object.freeze(visibleMatches.filter(item => item.kept)),
        triggered: Object.freeze(visibleMatches.filter(item => item.activated)),
        worldState,
    });
}

function element(documentRef, tag, className = '', content = '') {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (content) node.textContent = content;
    return node;
}

function field(documentRef, host, { label, tag = 'input', type = '', value = '', rows = 0, placeholder = '', maxLength = 0 }) {
    const wrapper = element(documentRef, 'label', 'world-field');
    wrapper.appendChild(element(documentRef, 'span', 'world-field-label', label));
    const control = element(documentRef, tag, 'world-control');
    if (type) control.type = type;
    if (rows) control.rows = rows;
    if (placeholder) control.placeholder = placeholder;
    if (maxLength) control.maxLength = maxLength;
    control.value = value;
    wrapper.appendChild(control);
    host.appendChild(wrapper);
    return control;
}

function formatTime(value) {
    if (!value) return '未记录';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? '未记录' : parsed.toLocaleString('zh-CN');
}

function messagePreview(message, limit = 90) {
    const value = text(record(message).content, '空消息').replace(/\s+/g, ' ');
    return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

export function appendMessageWorldEffects(documentRef, messageElement, message, onOpen, onRecord) {
    if (!messageElement || !message) return null;
    const matches = diagnosticMatches(message.context_diagnostics || message.prompt_diagnostics)
        .filter(item => item.kept && item.visibility !== 'hidden');
    const messageId = text(message.id);
    if (matches.length === 0 && (!messageId || typeof onRecord !== 'function')) return null;
    const host = element(documentRef, 'div', 'message-world-effects');
    if (matches.length) host.appendChild(element(documentRef, 'span', 'message-world-effects-label', '本轮世界影响'));
    for (const match of matches.slice(0, 8)) {
        const tag = element(documentRef, 'button', `world-effect-tag category-${match.category}`, match.title);
        tag.type = 'button';
        tag.title = match.trigger || match.reason || match.summary || `${CATEGORIES[match.category]}设定已进入本轮上下文`;
        tag.addEventListener('click', () => { if (typeof onOpen === 'function') onOpen(match.id); });
        host.appendChild(tag);
    }
    if (messageId && typeof onRecord === 'function') {
        const recordButton = element(documentRef, 'button', 'world-record-change-button', '记录为世界变化');
        recordButton.type = 'button';
        recordButton.addEventListener('click', () => onRecord({
            evidenceMessageId: messageId,
            title: '由对话产生的世界变化',
            detail: messagePreview(message, 500),
        }));
        host.appendChild(recordButton);
    }
    const content = messageElement.querySelector('.content');
    if (content) content.appendChild(host);
    else messageElement.appendChild(host);
    return host;
}

function createMultiPicker(documentRef, options) {
    const root = element(documentRef, 'section', 'world-picker');
    const title = element(documentRef, 'h4', 'world-picker-title', options.title);
    const search = element(documentRef, 'input', 'world-control world-picker-search');
    search.type = 'search';
    search.placeholder = options.placeholder || `搜索${options.title}`;
    search.setAttribute('aria-label', search.placeholder);
    const choices = element(documentRef, 'div', 'world-picker-choices');
    const selected = new Set(options.selected || []);

    function renderChoices() {
        choices.replaceChildren();
        const query = search.value.trim().toLocaleLowerCase('zh-CN');
        const items = (options.items || []).filter(item => (
            !query || item.label.toLocaleLowerCase('zh-CN').includes(query)
            || item.id.toLocaleLowerCase('zh-CN').includes(query)
        ));
        for (const item of items) {
            const label = element(documentRef, 'label', 'world-picker-choice');
            const checkbox = element(documentRef, 'input');
            checkbox.type = 'checkbox';
            checkbox.checked = selected.has(item.id);
            checkbox.addEventListener('change', () => {
                if (checkbox.checked) selected.add(item.id);
                else selected.delete(item.id);
                options.onChange([...selected]);
            });
            label.append(checkbox, element(documentRef, 'span', '', item.label));
            choices.appendChild(label);
        }
        if (!items.length) choices.appendChild(element(documentRef, 'p', 'world-empty', '没有匹配对象。'));
    }
    search.addEventListener('input', renderChoices);
    root.append(title, search, choices);
    renderChoices();
    return root;
}

export function createWorldContextController(options) {
    const {
        documentRef, showModal, listEntries, getSession, getDiagnostics, sessionWrite,
        endpoints, buildLibraryEditor, errorDetail,
    } = options;
    const getCharacters = typeof options.getCharacters === 'function' ? options.getCharacters : () => [];
    const onLocateMessage = typeof options.onLocateMessage === 'function' ? options.onLocateMessage : () => false;
    const showToast = typeof options.showToast === 'function' ? options.showToast : () => {};
    const confirmAction = typeof options.confirmAction === 'function' ? options.confirmAction : message => globalThis.confirm(message);
    const bar = documentRef.getElementById('world-context-bar');
    const summary = documentRef.getElementById('world-context-summary');
    const count = documentRef.getElementById('world-context-count');
    let model = buildWorldContextModel(null, null);
    let entries = [];
    let activeMode = 'state';
    let dirtyProbe = () => false;
    let renderVersion = 0;

    function render(session = getSession(), diagnostics = getDiagnostics()) {
        model = buildWorldContextModel(session, diagnostics);
        const names = model.active.map(item => item.title).slice(0, 2);
        summary.textContent = names.length
            ? `${model.location} · ${model.time} · ${names.join(' · ')}`
            : `${model.location} · ${model.time}`;
        const changeCount = model.worldState.changes.filter(item => item.status === 'active').length;
        count.textContent = `设定 ${model.active.length} · 变化 ${changeCount}`;
        bar.classList.toggle('has-active-world', model.active.length > 0 || changeCount > 0);
        return model;
    }

    function shellHeader(body) {
        const session = record(getSession());
        const header = element(documentRef, 'header', 'world-workspace-header');
        const context = element(documentRef, 'div', 'world-workspace-context');
        context.append(
            element(documentRef, 'strong', '', text(session.project, '未选择项目')),
            element(documentRef, 'span', '', `当前存档 · ${text(session.session_id, '未加载')}`),
        );
        const scene = element(documentRef, 'span', 'world-workspace-scene', `${model.location} · ${model.time}`);
        header.append(context, scene);
        const nav = element(documentRef, 'nav', 'world-workspace-nav');
        nav.setAttribute('aria-label', '世界设定视图');
        const labels = { library: '世界设定', state: '当前世界变化', relations: '设定关联' };
        for (const [mode, label] of Object.entries(labels)) {
            const button = element(documentRef, 'button', 'world-workspace-mode', label);
            button.type = 'button';
            button.dataset.mode = mode;
            button.addEventListener('click', () => { void switchMode(mode); });
            nav.appendChild(button);
        }
        body.append(header, nav);
        return nav;
    }

    function updateModeNav(nav) {
        for (const button of nav.querySelectorAll('.world-workspace-mode')) {
            const selected = button.dataset.mode === activeMode;
            button.classList.toggle('active', selected);
            button.setAttribute('aria-current', selected ? 'page' : 'false');
        }
    }

    function worldEntryItems() {
        return entries.map(entry => ({
            id: entry.id,
            label: `${text(entry.title, entry.id)} · ${CATEGORIES[entry.category] || entry.category}`,
        }));
    }

    function stateWorkspace(intent = {}) {
        let currentModel = buildWorldContextModel(getSession(), getDiagnostics());
        let selectedId = text(intent.changeId);
        let isNew = !currentModel.worldState.changes.some(item => item.id === selectedId);
        let preset = isNew ? {
            id: '', title: text(intent.title), detail: text(intent.detail), category: 'event', status: 'active',
            relatedEntryIds: [], evidenceMessageIds: stringArray([intent.evidenceMessageId]),
        } : currentModel.worldState.changes.find(item => item.id === selectedId);
        let draft = { ...preset, relatedEntryIds: [...preset.relatedEntryIds], evidenceMessageIds: [...preset.evidenceMessageIds] };
        let baseline = JSON.stringify(draft);
        let pending = false;
        let query = '';
        let statusFilter = 'all';

        const root = element(documentRef, 'div', 'world-state-workspace');
        if (intent.changeId || intent.evidenceMessageId || intent.title || intent.detail) root.classList.add('mobile-detail');
        const sidebar = element(documentRef, 'aside', 'world-state-sidebar');
        const editorHost = element(documentRef, 'main', 'world-state-editor-host');
        const inspector = element(documentRef, 'aside', 'world-state-inspector');
        root.append(sidebar, editorHost, inspector);

        function isDirty() { return JSON.stringify(draft) !== baseline; }
        function allowSwitch() { return !isDirty() || confirmAction('当前世界变化有未保存内容，确定放弃吗？'); }
        dirtyProbe = isDirty;

        function selectChange(changeId) {
            if (!allowSwitch()) return;
            const change = currentModel.worldState.changes.find(item => item.id === changeId);
            if (!change) return;
            selectedId = changeId;
            isNew = false;
            draft = { ...change, relatedEntryIds: [...change.relatedEntryIds], evidenceMessageIds: [...change.evidenceMessageIds] };
            baseline = JSON.stringify(draft);
            root.classList.add('mobile-detail');
            renderSidebar();
            renderEditor();
            renderInspector();
        }

        function newChange(nextIntent = {}, showDetail = true) {
            if (!allowSwitch()) return;
            selectedId = '';
            isNew = true;
            draft = {
                id: '', title: text(nextIntent.title), detail: text(nextIntent.detail), category: 'event', status: 'active',
                relatedEntryIds: [], evidenceMessageIds: stringArray([nextIntent.evidenceMessageId]),
            };
            baseline = JSON.stringify(draft);
            root.classList.toggle('mobile-detail', showDetail);
            renderSidebar();
            renderEditor();
            renderInspector();
        }

        function renderSidebar() {
            sidebar.replaceChildren();
            const head = element(documentRef, 'div', 'world-pane-heading');
            head.append(element(documentRef, 'div', '', '世界变化时间线'));
            const add = element(documentRef, 'button', 'world-primary-button', '＋ 记录变化');
            add.type = 'button';
            add.addEventListener('click', () => newChange());
            head.appendChild(add);
            const search = element(documentRef, 'input', 'world-control');
            search.type = 'search';
            search.placeholder = '搜索世界变化';
            search.setAttribute('aria-label', '搜索世界变化');
            search.value = query;
            search.addEventListener('input', () => { query = search.value; renderChangeList(list); });
            const filter = element(documentRef, 'select', 'world-control');
            filter.setAttribute('aria-label', '筛选世界变化状态');
            for (const [value, label] of [['all', '全部状态'], ...Object.entries(CHANGE_STATUSES)]) {
                const option = element(documentRef, 'option', '', label); option.value = value; filter.appendChild(option);
            }
            filter.value = statusFilter;
            filter.addEventListener('change', () => { statusFilter = filter.value; renderChangeList(list); });
            const list = element(documentRef, 'div', 'world-change-timeline');
            sidebar.append(head, search, filter, list);
            renderChangeList(list);
        }

        function renderChangeList(list) {
            list.replaceChildren();
            const needle = query.trim().toLocaleLowerCase('zh-CN');
            const changes = [...currentModel.worldState.changes].reverse().filter(change => (
                (statusFilter === 'all' || change.status === statusFilter)
                && (!needle || `${change.title} ${change.detail}`.toLocaleLowerCase('zh-CN').includes(needle))
            ));
            for (const change of changes) {
                const button = element(documentRef, 'button', `world-change-timeline-card status-${change.status}`);
                button.type = 'button';
                button.classList.toggle('active', change.id === selectedId);
                button.append(
                    element(documentRef, 'strong', '', change.title),
                    element(documentRef, 'span', 'world-change-status', CHANGE_STATUSES[change.status]),
                    element(documentRef, 'small', '', `${CHANGE_CATEGORIES[change.category]} · ${formatTime(change.updatedAt || change.createdAt)}`),
                );
                button.addEventListener('click', () => selectChange(change.id));
                list.appendChild(button);
            }
            if (!changes.length) list.appendChild(element(documentRef, 'p', 'world-empty', '当前筛选下没有世界变化。'));
        }

        function renderEditor() {
            editorHost.replaceChildren();
            const form = element(documentRef, 'form', 'world-change-editor');
            form.noValidate = true;
            const head = element(documentRef, 'div', 'world-pane-heading');
            const back = element(documentRef, 'button', 'world-mobile-back', '← 返回时间线');
            back.type = 'button';
            back.addEventListener('click', () => root.classList.remove('mobile-detail'));
            head.append(
                back,
                element(documentRef, 'div', '', isNew ? '记录世界变化' : '编辑世界变化'),
                element(documentRef, 'span', 'world-scope-badge save-scope', '当前存档'),
            );
            const status = element(documentRef, 'p', 'world-form-status');
            status.setAttribute('role', 'status');
            const title = field(documentRef, form, { label: '变化标题', value: draft.title, maxLength: 200, placeholder: '例如：花园守卫解除封锁' });
            title.required = true;
            const row = element(documentRef, 'div', 'world-form-row');
            const category = field(documentRef, row, { label: '类型', tag: 'select' });
            for (const [value, label] of Object.entries(CHANGE_CATEGORIES)) {
                const option = element(documentRef, 'option', '', label); option.value = value; category.appendChild(option);
            }
            category.value = draft.category;
            const changeStatus = field(documentRef, row, { label: '状态', tag: 'select' });
            for (const [value, label] of Object.entries(CHANGE_STATUSES)) {
                const option = element(documentRef, 'option', '', label); option.value = value; changeStatus.appendChild(option);
            }
            changeStatus.value = draft.status;
            form.appendChild(row);
            const detail = field(documentRef, form, { label: '影响与结果', tag: 'textarea', rows: 6, value: draft.detail, maxLength: 2000, placeholder: '说明影响范围、当前结果和仍待解决的问题' });

            const links = createMultiPicker(documentRef, {
                title: '关联世界设定', items: worldEntryItems(), selected: draft.relatedEntryIds,
                onChange(ids) { draft.relatedEntryIds = ids; renderInspector(); },
            });
            const messages = Array.isArray(record(getSession()).message_history) ? getSession().message_history : [];
            const evidenceItems = messages.slice(-100).map((message, index) => ({
                id: text(message.id),
                label: `消息 ${Math.max(1, messages.length - 99) + index} · ${messagePreview(message)}`,
            })).filter(item => item.id);
            const evidence = createMultiPicker(documentRef, {
                title: '证据消息', items: evidenceItems, selected: draft.evidenceMessageIds,
                placeholder: '搜索当前存档消息',
                onChange(ids) { draft.evidenceMessageIds = ids; renderInspector(); },
            });
            const pickers = element(documentRef, 'div', 'world-change-pickers');
            pickers.append(links, evidence);

            const actions = element(documentRef, 'div', 'world-form-actions');
            if (!isNew) {
                const remove = element(documentRef, 'button', 'world-danger-button', '删除记录');
                remove.type = 'button';
                remove.addEventListener('click', async () => {
                    if (!confirmAction(`确定删除世界变化“${draft.title}”吗？`)) return;
                    remove.disabled = true;
                    try {
                        const session = getSession();
                        await sessionWrite(endpoints.removeChange(draft.id), 'DELETE', {
                            project: session.project, save: session.session_id,
                        }, '删除世界变化');
                        currentModel = buildWorldContextModel(getSession(), getDiagnostics());
                        render(getSession(), getDiagnostics());
                        newChange({}, false);
                        showToast('世界变化已删除');
                    } catch (error) {
                        status.textContent = `删除失败：${errorDetail(error)}`;
                        remove.disabled = false;
                    }
                });
                actions.appendChild(remove);
            }
            const save = element(documentRef, 'button', 'world-primary-button', isNew ? '记录并应用' : '保存并应用');
            save.type = 'submit';
            actions.appendChild(save);

            function syncDraft() {
                draft.title = title.value.trim();
                draft.detail = detail.value.trim();
                draft.category = category.value;
                draft.status = changeStatus.value;
            }
            for (const control of [title, detail, category, changeStatus]) {
                control.addEventListener('input', syncDraft);
                control.addEventListener('change', syncDraft);
            }
            form.addEventListener('submit', async event => {
                event.preventDefault();
                syncDraft();
                if (!draft.title) {
                    status.textContent = '请填写变化标题。';
                    status.setAttribute('role', 'alert');
                    title.focus();
                    return;
                }
                pending = true;
                save.disabled = true;
                save.textContent = '保存中…';
                status.textContent = '';
                try {
                    const session = getSession();
                    const payload = {
                        project: session.project, save: session.session_id,
                        category: draft.category, title: draft.title, detail: draft.detail, status: draft.status,
                        related_entry_ids: draft.relatedEntryIds, evidence_message_ids: draft.evidenceMessageIds,
                    };
                    const url = isNew ? endpoints.changes : endpoints.updateChange(draft.id);
                    await sessionWrite(url, isNew ? 'POST' : 'PATCH', payload, isNew ? '记录世界变化' : '编辑世界变化');
                    currentModel = buildWorldContextModel(getSession(), getDiagnostics());
                    render(getSession(), getDiagnostics());
                    const saved = isNew ? currentModel.worldState.changes.at(-1) : currentModel.worldState.changes.find(item => item.id === draft.id);
                    if (saved) {
                        selectedId = saved.id;
                        isNew = false;
                        draft = { ...saved, relatedEntryIds: [...saved.relatedEntryIds], evidenceMessageIds: [...saved.evidenceMessageIds] };
                        baseline = JSON.stringify(draft);
                    }
                    renderSidebar();
                    renderEditor();
                    renderInspector();
                    showToast('世界变化已保存');
                } catch (error) {
                    status.textContent = `保存失败：${errorDetail(error)}`;
                    status.setAttribute('role', 'alert');
                    save.disabled = false;
                    save.textContent = isNew ? '记录并应用' : '保存并应用';
                } finally {
                    pending = false;
                }
            });
            editorHost.append(head, status, form);
            form.append(pickers, actions);
        }

        function renderInspector() {
            inspector.replaceChildren();
            inspector.appendChild(element(documentRef, 'h3', 'world-pane-title', '当前状态检查器'));
            const session = record(getSession());
            const facts = element(documentRef, 'dl', 'world-inspector-facts');
            for (const [term, value] of [
                ['数据范围', '当前存档'],
                ['地点与时间', `${currentModel.location} · ${currentModel.time}`],
                ['关联设定', `${draft.relatedEntryIds.length} 条`],
                ['证据消息', `${draft.evidenceMessageIds.length} 条`],
                ['最后更新', isNew ? '尚未保存' : formatTime(draft.updatedAt || draft.createdAt)],
            ]) facts.append(element(documentRef, 'dt', '', term), element(documentRef, 'dd', '', value));
            inspector.appendChild(facts);

            const discovered = element(documentRef, 'section', 'world-discovery-section');
            discovered.appendChild(element(documentRef, 'h4', '', '发现状态'));
            const discoverable = entries.filter(entry => entry.visibility === 'discovered');
            for (const entry of discoverable) {
                const label = element(documentRef, 'label', 'world-discovery-choice');
                const checkbox = element(documentRef, 'input');
                checkbox.type = 'checkbox';
                checkbox.checked = currentModel.worldState.discoveredEntryIds.includes(entry.id);
                checkbox.addEventListener('change', async () => {
                    checkbox.disabled = true;
                    const selected = new Set(currentModel.worldState.discoveredEntryIds);
                    if (checkbox.checked) selected.add(entry.id); else selected.delete(entry.id);
                    try {
                        await sessionWrite(endpoints.discoveries, 'PUT', {
                            project: session.project, save: session.session_id, entry_ids: [...selected],
                        }, '保存世界发现');
                        currentModel = buildWorldContextModel(getSession(), getDiagnostics());
                        render(getSession(), getDiagnostics());
                        renderInspector();
                        showToast(checkbox.checked ? '已标记为发现' : '已撤销发现标记');
                    } catch (error) {
                        showToast(`保存失败：${errorDetail(error)}`);
                        checkbox.disabled = false;
                    }
                });
                label.append(checkbox, element(documentRef, 'span', '', text(entry.title, entry.id)));
                discovered.appendChild(label);
            }
            if (!discoverable.length) discovered.appendChild(element(documentRef, 'p', 'world-empty', '世界设定中没有“发现后玩家才知道”的内容。'));
            inspector.appendChild(discovered);

            if (draft.evidenceMessageIds.length) {
                const evidence = element(documentRef, 'section', 'world-evidence-section');
                evidence.appendChild(element(documentRef, 'h4', '', '定位证据'));
                for (const id of draft.evidenceMessageIds) {
                    const message = (session.message_history || []).find(item => item.id === id);
                    const button = element(documentRef, 'button', 'world-secondary-button', messagePreview(message || { content: id }, 44));
                    button.type = 'button';
                    button.addEventListener('click', () => onLocateMessage(id));
                    evidence.appendChild(button);
                }
                inspector.appendChild(evidence);
            }
        }

        renderSidebar();
        renderEditor();
        renderInspector();
        return root;
    }

    function relationWorkspace(onOpenEntry) {
        dirtyProbe = () => false;
        const root = element(documentRef, 'div', 'world-relations-workspace');
        const toolbar = element(documentRef, 'div', 'world-relations-toolbar');
        const search = element(documentRef, 'input', 'world-control');
        search.type = 'search';
        search.placeholder = '搜索地点、势力、规则或角色';
        search.setAttribute('aria-label', '搜索世界关系');
        const category = element(documentRef, 'select', 'world-control');
        category.setAttribute('aria-label', '筛选关系节点类型');
        for (const [value, label] of [['all', '全部类型'], ...Object.entries(CATEGORIES)]) {
            const option = element(documentRef, 'option', '', label); option.value = value; category.appendChild(option);
        }
        const graphButton = element(documentRef, 'button', 'world-secondary-button active', '关联图');
        const listButton = element(documentRef, 'button', 'world-secondary-button', '关系列表');
        graphButton.type = listButton.type = 'button';
        toolbar.append(search, category, graphButton, listButton);
        const host = element(documentRef, 'div', 'world-relations-host');
        root.append(toolbar, host);
        let view = 'graph';

        function edgesForVisible(visibleEntries) {
            const visibleIds = new Set(visibleEntries.map(entry => entry.id));
            const characters = new Map(getCharacters().map(character => [character.id, text(character.name, character.id)]));
            const edges = [];
            for (const entry of visibleEntries) {
                for (const target of entry.linkedEntryIds || []) {
                    if (visibleIds.has(target)) edges.push({ source: entry.id, target, label: '关联设定', sourceEntry: entry.id });
                }
                for (const target of entry.linkedCharacterIds || []) {
                    edges.push({ source: entry.id, target: `character:${target}`, targetLabel: characters.get(target) || '已缺失角色', label: '涉及角色', sourceEntry: entry.id });
                }
            }
            return edges;
        }

        function visibleEntries() {
            const needle = search.value.trim().toLocaleLowerCase('zh-CN');
            return entries.filter(entry => (
                (category.value === 'all' || entry.category === category.value)
                && (!needle || `${entry.title} ${entry.summary} ${entry.id}`.toLocaleLowerCase('zh-CN').includes(needle))
            ));
        }

        function renderGraph(visible, edges) {
            const section = element(documentRef, 'section', 'world-relation-graph-panel');
            const nodeIds = [...new Set(edges.flatMap(edge => [edge.source, edge.target]))].slice(0, 36);
            if (!nodeIds.length) {
                const empty = element(documentRef, 'div', 'world-relation-empty');
                empty.append(
                    element(documentRef, 'h3', '', '还没有世界关联'),
                    element(documentRef, 'p', '', '在世界设定的“关联角色与其他设定”中建立联系，关联图会自动生成。'),
                );
                const create = element(documentRef, 'button', 'world-primary-button', '前往世界设定建立关联');
                create.type = 'button';
                create.addEventListener('click', () => onOpenEntry(''));
                empty.appendChild(create);
                section.appendChild(empty);
                return section;
            }
            const entryMap = new Map(visible.map(entry => [entry.id, entry]));
            const svg = documentRef.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('class', 'world-relation-graph');
            svg.setAttribute('viewBox', '0 0 900 460');
            svg.setAttribute('role', 'img');
            svg.setAttribute('aria-label', `${nodeIds.length} 个节点、${edges.length} 条世界关系`);
            const positions = new Map();
            const radius = Math.min(165, 115 + nodeIds.length * 3);
            nodeIds.forEach((id, index) => {
                const angle = Math.PI * 2 * index / nodeIds.length - Math.PI / 2;
                positions.set(id, { x: 450 + Math.cos(angle) * radius, y: 230 + Math.sin(angle) * radius });
            });
            for (const edge of edges) {
                const start = positions.get(edge.source); const end = positions.get(edge.target);
                if (!start || !end) continue;
                const line = documentRef.createElementNS('http://www.w3.org/2000/svg', 'line');
                for (const [name, value] of Object.entries({ x1: start.x, y1: start.y, x2: end.x, y2: end.y })) line.setAttribute(name, String(value));
                line.setAttribute('class', 'world-relation-edge');
                svg.appendChild(line);
            }
            for (const id of nodeIds) {
                const position = positions.get(id);
                const group = documentRef.createElementNS('http://www.w3.org/2000/svg', 'g');
                const entry = entryMap.get(id);
                const categoryClass = entry && CATEGORIES[entry.category] ? ` category-${entry.category}` : '';
                group.setAttribute('class', `world-relation-node${id.startsWith('character:') ? ' character-node' : ''}${categoryClass}`);
                group.setAttribute('tabindex', '0');
                group.setAttribute('role', 'button');
                const linked = edges.find(edge => edge.target === id && edge.targetLabel);
                const label = entry ? text(entry.title, entry.id) : text(linked && linked.targetLabel, id.replace('character:', ''));
                group.setAttribute('aria-label', entry ? `打开世界设定 ${label}` : `关联角色 ${label}`);
                const circle = documentRef.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', position.x); circle.setAttribute('cy', position.y); circle.setAttribute('r', '42');
                const name = documentRef.createElementNS('http://www.w3.org/2000/svg', 'text');
                name.setAttribute('x', position.x); name.setAttribute('y', position.y + 5); name.setAttribute('text-anchor', 'middle');
                name.textContent = label.length > 7 ? `${label.slice(0, 7)}…` : label;
                group.append(circle, name);
                if (entry) {
                    group.addEventListener('click', () => onOpenEntry(entry.id));
                    group.addEventListener('keydown', event => {
                        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onOpenEntry(entry.id); }
                    });
                }
                svg.appendChild(group);
            }
            section.appendChild(svg);
            section.appendChild(element(documentRef, 'p', 'world-relation-hint', '点击设定节点进入编辑；角色节点用于说明设定涉及对象。'));
            return section;
        }

        function renderList(visible, edges) {
            const section = element(documentRef, 'section', 'world-relation-list');
            const entryMap = new Map(visible.map(entry => [entry.id, entry]));
            for (const edge of edges) {
                const source = entryMap.get(edge.source);
                const target = entryMap.get(edge.target);
                const card = element(documentRef, 'article', 'world-relation-card');
                card.append(
                    element(documentRef, 'strong', '', `${text(source && source.title, edge.source)} → ${text(target && target.title, edge.targetLabel || edge.target)}`),
                    element(documentRef, 'span', '', edge.label),
                );
                const edit = element(documentRef, 'button', 'world-secondary-button', '编辑关联');
                edit.type = 'button';
                edit.addEventListener('click', () => onOpenEntry(edge.sourceEntry));
                card.appendChild(edit);
                section.appendChild(card);
            }
            if (!edges.length) section.appendChild(element(documentRef, 'p', 'world-empty', '当前筛选下没有关联。'));
            return section;
        }

        function renderRelations() {
            const visible = visibleEntries();
            const edges = edgesForVisible(visible);
            graphButton.classList.toggle('active', view === 'graph');
            listButton.classList.toggle('active', view === 'list');
            host.replaceChildren(view === 'graph' ? renderGraph(visible, edges) : renderList(visible, edges));
        }
        search.addEventListener('input', renderRelations);
        category.addEventListener('change', renderRelations);
        graphButton.addEventListener('click', () => { view = 'graph'; renderRelations(); });
        listButton.addEventListener('click', () => { view = 'list'; renderRelations(); });
        renderRelations();
        return root;
    }

    let currentShell = null;

    async function switchMode(mode, intent = {}) {
        if (!currentShell || !['library', 'state', 'relations'].includes(mode)) return;
        if (mode !== activeMode && dirtyProbe() && !confirmAction('当前内容尚未保存，确定离开吗？')) return;
        activeMode = mode;
        dirtyProbe = () => false;
        updateModeNav(currentShell.nav);
        const version = ++renderVersion;
        currentShell.host.replaceChildren(element(documentRef, 'p', 'world-loading', '正在加载世界设定…'));
        try {
            if (mode !== 'library') entries = await listEntries();
            if (version !== renderVersion) return;
            if (mode === 'library') {
                const editor = await buildLibraryEditor({ selectedId: text(intent.entryId) });
                if (version !== renderVersion) return;
                currentShell.host.replaceChildren(editor.root || editor);
                if (editor && typeof editor.state === 'function') {
                    dirtyProbe = () => {
                        const state = editor.state();
                        return state.entryDirty || state.manualDirty;
                    };
                }
            } else if (mode === 'relations') {
                currentShell.host.replaceChildren(relationWorkspace(entryId => { void switchMode('library', { entryId }); }));
            } else {
                currentShell.host.replaceChildren(stateWorkspace(intent));
            }
        } catch (error) {
            if (version !== renderVersion) return;
            currentShell.host.replaceChildren(element(documentRef, 'p', 'world-error', `加载失败：${errorDetail(error)}`));
        }
    }

    async function showWorkbench(initialMode = 'state', intent = {}) {
        const requestedMode = initialMode === 'overview' ? 'state' : initialMode;
        try {
            entries = await listEntries();
        } catch (error) {
            showModal({ title: '世界设定', body: element(documentRef, 'p', 'world-error', `读取世界资料失败：${errorDetail(error)}`) });
            return;
        }
        render();
        const body = element(documentRef, 'div', 'world-workspace');
        const nav = shellHeader(body);
        const host = element(documentRef, 'div', 'world-workspace-body');
        body.appendChild(host);
        currentShell = { body, nav, host };
        activeMode = ['library', 'state', 'relations'].includes(requestedMode) ? requestedMode : 'state';
        updateModeNav(nav);
        showModal({
            title: '世界设定',
            body,
            dialogClass: 'world-workspace-dialog',
            onBeforeClose: () => !dirtyProbe() || confirmAction('世界设定中有未保存内容，确定关闭吗？'),
        });
        await switchMode(activeMode, intent);
    }

    bar.addEventListener('click', () => { void showWorkbench('state'); });
    return Object.freeze({ render, showWorkbench });
}
