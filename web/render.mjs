export function affinityBar(value, normalize) {
    const percent = normalize(value);
    const filled = Math.round(percent / 10);
    return '█'.repeat(filled) + '░'.repeat(10 - filled);
}

export function mountMessageHistory(documentRef, container, history, renderMessage) {
    if (!documentRef || typeof documentRef.createDocumentFragment !== 'function') {
        throw new TypeError('历史渲染缺少 document');
    }
    if (!container || typeof container.replaceChildren !== 'function') {
        throw new TypeError('历史渲染缺少容器');
    }
    if (!Array.isArray(history) || typeof renderMessage !== 'function') {
        throw new TypeError('历史渲染参数无效');
    }
    const fragment = documentRef.createDocumentFragment();
    let mounted = 0;
    for (const message of history) {
        const node = renderMessage(message);
        if (!node) continue;
        fragment.appendChild(node);
        mounted += 1;
    }
    container.replaceChildren(fragment);
    return mounted;
}

function actionButton(documentRef, className, title, text) {
    const button = documentRef.createElement('button');
    button.type = 'button';
    button.className = `msg-action-btn ${className}`;
    button.title = title;
    button.setAttribute('aria-label', title);
    button.textContent = text;
    return button;
}

export function createMessageElement(documentRef, { role, content, message = {}, timeText = '' }) {
    if (!documentRef || !['user', 'assistant'].includes(role)) throw new TypeError('消息渲染参数无效');
    const container = documentRef.createElement('div');
    container.className = `msg ${role}`;
    container.dataset.messageId = message.id || '';

    const checkbox = documentRef.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'msg-checkbox';
    checkbox.checked = message.in_prompt !== false;
    checkbox.title = '勾选 = 进 prompt';
    checkbox.setAttribute('aria-label', '包含在 Prompt 中');
    const includeControl = documentRef.createElement('span');
    includeControl.className = 'msg-include-control';
    includeControl.appendChild(checkbox);
    container.appendChild(includeControl);

    const actions = documentRef.createElement('div');
    actions.className = 'msg-actions';
    actions.appendChild(actionButton(documentRef, 'edit', '编辑', '✎'));
    actions.appendChild(actionButton(documentRef, 'regenerate', role === 'user' ? '重新生成（从此条之后）' : '重新生成', '🔄'));
    const pin = actionButton(documentRef, 'pin', '钉选为常驻记忆', '📌');
    if (message.pinned) pin.classList.add('active');
    actions.appendChild(pin);
    actions.appendChild(actionButton(documentRef, 'delete', '删除', '🗑'));
    container.appendChild(actions);

    const roleElement = documentRef.createElement('div');
    roleElement.className = 'msg-role';
    roleElement.textContent = role === 'user' ? '你' : `AI · ${timeText}`;
    container.appendChild(roleElement);
    const contentElement = documentRef.createElement('div');
    contentElement.className = 'content';
    contentElement.textContent = String(content || '');
    container.appendChild(contentElement);
    return { container, contentElement };
}
