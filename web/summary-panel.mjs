function createButton(documentRef, className, text, onClick) {
    const button = documentRef.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = text;
    if (typeof onClick === 'function') button.addEventListener('click', onClick);
    return button;
}
export function selectSummaryId(summaries, preferredId = null) {
    const valid = Array.isArray(summaries)
        ? summaries.filter(item => item && typeof item.id === 'string' && item.id)
        : [];
    if (valid.some(item => item.id === preferredId)) return preferredId;
    return valid.length ? valid[valid.length - 1].id : null;
}

export function summaryStatusLabel(summary) {
    if (!summary || typeof summary !== 'object') return '状态未知';
    if (summary.status === 'pending') {
        return summary.generation_active === false ? '生成已中断，可恢复' : '生成中';
    }
    if (summary.status === 'failed') return '生成失败';
    return '已完成';
}

export function summarySourceLabel(summary) {
    const labels = {
        available: '原文快照可用',
        missing: '原文快照缺失',
        invalid: '原文快照标识无效',
        unlinked: '旧摘要未绑定原文快照',
    };
    return labels[summary && summary.source_status] || '原文快照状态未知';
}

function appendList(documentRef, parent, title, items) {
    if (!Array.isArray(items) || items.length === 0) return;
    const group = documentRef.createElement('div');
    group.className = 'summary-detail-group';
    const heading = documentRef.createElement('div');
    heading.className = 'summary-detail-label';
    heading.textContent = title;
    group.appendChild(heading);
    const list = documentRef.createElement('ul');
    list.className = 'summary-detail-list';
    for (const item of items) {
        const row = documentRef.createElement('li');
        row.textContent = String(item);
        list.appendChild(row);
    }
    group.appendChild(list);
    parent.appendChild(group);
}

function appendText(documentRef, parent, label, value) {
    const group = documentRef.createElement('div');
    group.className = 'summary-detail-group';
    const heading = documentRef.createElement('div');
    heading.className = 'summary-detail-label';
    heading.textContent = label;
    const content = documentRef.createElement('div');
    content.className = 'summary-detail-text';
    content.textContent = value || '（空）';
    group.appendChild(heading);
    group.appendChild(content);
    parent.appendChild(group);
}

export function renderSummaryPanelView(options) {
    const {
        document: documentRef,
        anchor,
        session,
        selectedId = null,
        expanded = false,
        disabled = false,
        onSelect,
        onExpandedChange,
        onEdit,
        onRegenerate,
        onOpenSource,
    } = options;
    if (!documentRef || !anchor || !anchor.parentNode) return null;

    const existing = anchor.nextElementSibling;
    if (existing && existing.classList && existing.classList.contains('summary-panel')) {
        existing.remove();
    }

    const summaries = Array.isArray(session && session.summaries)
        ? session.summaries.filter(item => item && typeof item.id === 'string')
        : [];
    const globalError = typeof (session && session.summary_error) === 'string'
        ? session.summary_error
        : '';
    if (summaries.length === 0 && !globalError) return null;

    const resolvedId = selectSummaryId(summaries, selectedId);
    const selected = summaries.find(item => item.id === resolvedId) || null;
    const panel = documentRef.createElement('section');
    panel.className = `summary-panel${expanded ? '' : ' collapsed'}`;
    panel.setAttribute('aria-label', '剧情记忆');
    anchor.parentNode.insertBefore(panel, anchor.nextSibling);

    const bodyId = `summary-panel-body-${String(resolvedId || 'empty').replace(/[^a-zA-Z0-9_-]/g, '')}`;
    const toggle = createButton(documentRef, 'summary-panel-toggle', `剧情记忆 · ${summaries.length} 段`);
    toggle.setAttribute('aria-expanded', String(Boolean(expanded)));
    toggle.setAttribute('aria-controls', bodyId);
    toggle.addEventListener('click', () => {
        const nextExpanded = panel.classList.contains('collapsed');
        panel.classList.toggle('collapsed', !nextExpanded);
        toggle.setAttribute('aria-expanded', String(nextExpanded));
        if (typeof onExpandedChange === 'function') onExpandedChange(nextExpanded);
    });
    panel.appendChild(toggle);

    const body = documentRef.createElement('div');
    body.id = bodyId;
    body.className = 'summary-panel-body';
    body.setAttribute('aria-busy', String(Boolean(selected && selected.status === 'pending' && selected.generation_active !== false)));
    panel.appendChild(body);

    if (globalError && !selected) {
        const alert = documentRef.createElement('div');
        alert.className = 'summary-error';
        alert.setAttribute('role', 'alert');
        alert.textContent = globalError;
        body.appendChild(alert);
        return { panel, selectedId: resolvedId };
    }
    if (!selected) return { panel, selectedId: null };

    const chooser = documentRef.createElement('div');
    chooser.className = 'summary-chooser';
    const chooserLabel = documentRef.createElement('label');
    chooserLabel.textContent = '选择记忆段';
    chooserLabel.htmlFor = 'summary-segment-select';
    const select = documentRef.createElement('select');
    select.id = 'summary-segment-select';
    select.className = 'summary-segment-select';
    select.disabled = disabled;
    [...summaries].reverse().forEach(summary => {
        const originalIndex = summaries.findIndex(item => item.id === summary.id);
        const option = documentRef.createElement('option');
        option.value = summary.id;
        option.textContent = `第 ${originalIndex + 1} 段 · ${summaryStatusLabel(summary)}`;
        option.selected = summary.id === resolvedId;
        select.appendChild(option);
    });
    select.addEventListener('change', () => {
        if (typeof onSelect === 'function') onSelect(select.value);
    });
    chooser.appendChild(chooserLabel);
    chooser.appendChild(select);
    body.appendChild(chooser);

    const statusRow = documentRef.createElement('div');
    statusRow.className = 'summary-status-row';
    const status = documentRef.createElement('span');
    status.className = `summary-status summary-status-${selected.status || 'completed'}`;
    status.textContent = summaryStatusLabel(selected);
    const source = documentRef.createElement('span');
    source.className = `summary-source summary-source-${selected.source_status || 'unknown'}`;
    source.textContent = summarySourceLabel(selected);
    statusRow.appendChild(status);
    statusRow.appendChild(source);
    body.appendChild(statusRow);

    if (selected.error) {
        const alert = documentRef.createElement('div');
        alert.className = 'summary-error';
        alert.setAttribute('role', 'alert');
        alert.textContent = String(selected.error);
        body.appendChild(alert);
    }
    if (selected.status === 'pending' && selected.generation_active === false) {
        const interrupted = documentRef.createElement('div');
        interrupted.className = 'summary-warning';
        interrupted.setAttribute('role', 'status');
        interrupted.textContent = '上次生成任务已中断；可从绑定的原文快照恢复生成。';
        body.appendChild(interrupted);
    }
    if (selected.source_status && selected.source_status !== 'available') {
        const sourceWarning = documentRef.createElement('div');
        sourceWarning.className = 'summary-warning';
        sourceWarning.setAttribute('role', 'status');
        sourceWarning.textContent = `${summarySourceLabel(selected)}；现有摘要仍可人工编辑，但不能调用模型重生成。`;
        body.appendChild(sourceWarning);
    }

    appendText(documentRef, body, '前情提要', selected.text || '');
    if (selected.time) appendText(documentRef, body, '时间线', selected.time);
    appendList(documentRef, body, '关键事件', selected.facts);
    appendList(documentRef, body, '角色关系', selected.relations);

    const meta = documentRef.createElement('div');
    meta.className = 'summary-meta';
    const created = selected.created_at || selected.edited_at || '';
    meta.textContent = created ? `记录时间：${String(created).slice(0, 16).replace('T', ' ')}` : '记录时间：未记录';
    body.appendChild(meta);

    const actions = documentRef.createElement('div');
    actions.className = 'summary-actions';
    const isActivePending = selected.status === 'pending' && selected.generation_active !== false;
    const edit = createButton(documentRef, 'summary-edit-btn', '编辑', () => onEdit && onEdit(selected));
    edit.disabled = Boolean(disabled || isActivePending);
    const regenerateText = selected.status === 'failed'
        || (selected.status === 'pending' && selected.generation_active === false)
        ? '重试生成'
        : '重新生成';
    const regenerate = createButton(
        documentRef,
        'summary-regen-btn',
        regenerateText,
        () => onRegenerate && onRegenerate(selected),
    );
    regenerate.disabled = Boolean(
        disabled
        || isActivePending
        || selected.source_status !== 'available'
    );
    const openSource = createButton(
        documentRef,
        'summary-source-btn',
        '查看原文',
        () => onOpenSource && onOpenSource(selected),
    );
    openSource.disabled = Boolean(
        !selected.source_snapshot_id
        || selected.source_status !== 'available'
    );
    actions.appendChild(edit);
    actions.appendChild(regenerate);
    actions.appendChild(openSource);
    body.appendChild(actions);

    return { panel, selectedId: resolvedId };
}
