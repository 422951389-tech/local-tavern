import { createIcon } from './icons.mjs';

const MESSAGE_STATES = new Set(['pending', 'streaming', 'completed', 'cancelled', 'failed']);

export function affinityBar(value, normalize) {
    const percent = normalize(value);
    const filled = Math.round(percent / 10);
    return '█'.repeat(filled) + '░'.repeat(10 - filled);
}

const AFFINITY_STAGES = Object.freeze([
    Object.freeze({ maximum: 19, label: '陌生' }),
    Object.freeze({ maximum: 39, label: '初识' }),
    Object.freeze({ maximum: 59, label: '熟悉' }),
    Object.freeze({ maximum: 79, label: '亲近' }),
    Object.freeze({ maximum: 100, label: '深厚' }),
]);

export function affinityPresentation(value, normalize, previousValue = null) {
    if (typeof normalize !== 'function') throw new TypeError('好感度展示缺少归一化函数');
    const percent = Math.round(normalize(value));
    const stage = AFFINITY_STAGES.find(item => percent <= item.maximum)?.label || '深厚';
    const hasPrevious = previousValue !== null
        && previousValue !== undefined
        && Number.isFinite(Number(previousValue));
    const previous = hasPrevious ? Math.round(normalize(previousValue)) : null;
    const delta = previous === null ? null : percent - previous;
    const direction = delta === null ? 'unknown' : delta > 0 ? 'up' : delta < 0 ? 'down' : 'steady';
    const changeText = delta === null
        ? ''
        : delta > 0
            ? `本轮 +${delta}`
            : delta < 0
                ? `本轮 −${Math.abs(delta)}`
                : '本轮无变化';
    const changeDescription = delta === null
        ? '本轮变化未记录'
        : delta > 0
            ? `本轮上升 ${delta}`
            : delta < 0
                ? `本轮下降 ${Math.abs(delta)}`
                : '本轮无变化';
    return Object.freeze({
        percent,
        previous,
        delta,
        stage,
        direction,
        changeText,
        ariaText: `好感度 ${percent}/100，关系阶段${stage}，${changeDescription}`,
    });
}

export function createAffinityIndicator(documentRef, {
    value,
    previousValue = null,
    normalize,
} = {}) {
    if (!documentRef || typeof documentRef.createElement !== 'function') {
        throw new TypeError('好感度展示缺少 document');
    }
    const presentation = affinityPresentation(value, normalize, previousValue);
    const root = documentRef.createElement('div');
    root.className = `affinity-summary affinity-${presentation.direction}`;

    const copy = documentRef.createElement('div');
    copy.className = 'affinity-copy';
    const label = documentRef.createElement('span');
    label.className = 'affinity-label';
    label.textContent = '好感度';
    const current = documentRef.createElement('strong');
    current.className = 'affinity-value';
    current.textContent = `${presentation.percent}/100`;
    const stage = documentRef.createElement('span');
    stage.className = 'affinity-stage';
    stage.textContent = presentation.stage;
    copy.appendChild(label);
    copy.appendChild(current);
    copy.appendChild(stage);
    if (presentation.changeText) {
        const change = documentRef.createElement('span');
        change.className = 'affinity-change';
        change.textContent = presentation.changeText;
        copy.appendChild(change);
    }

    const meter = documentRef.createElement('div');
    meter.className = 'affinity-meter';
    meter.setAttribute('role', 'meter');
    meter.setAttribute('aria-label', presentation.ariaText);
    meter.setAttribute('aria-valuemin', '0');
    meter.setAttribute('aria-valuemax', '100');
    meter.setAttribute('aria-valuenow', String(presentation.percent));
    meter.setAttribute('aria-valuetext', presentation.ariaText);
    const fill = documentRef.createElement('span');
    fill.className = 'affinity-meter-fill';
    fill.style.width = `${presentation.percent}%`;
    meter.appendChild(fill);

    root.appendChild(copy);
    root.appendChild(meter);
    return Object.freeze({ element: root, presentation });
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

function cleanLabel(value, fallback = '') {
    if (typeof value !== 'string') return fallback;
    const normalized = value.replace(/[\u0000-\u001f\u007f]/g, '').trim();
    return normalized.slice(0, 80) || fallback;
}

export function messagePresentation(role, message = {}) {
    if (role === 'user') return Object.freeze({ kind: 'action', label: '你的行动' });
    const explicit = cleanLabel(message.presentation || message.kind || message.message_type).toLowerCase();
    const speaker = cleanLabel(message.speaker || message.character_name || message.character);
    if (speaker || explicit === 'dialogue' || explicit === 'character') {
        return Object.freeze({ kind: 'dialogue', label: speaker || '角色对白' });
    }
    if (explicit === 'narration' || explicit === 'narrator') {
        return Object.freeze({ kind: 'narration', label: '旁白' });
    }
    if (explicit === 'status' || explicit === 'system') {
        return Object.freeze({ kind: 'status', label: '状态记录' });
    }
    return Object.freeze({ kind: 'scene', label: '场景叙事' });
}

function actionButton(documentRef, className, title, iconName) {
    const button = documentRef.createElement('button');
    button.type = 'button';
    button.className = `msg-action-btn ${className}`;
    button.title = title;
    button.setAttribute('aria-label', title);
    button.appendChild(createIcon(documentRef, iconName, { size: 18 }));
    return button;
}

function createThinkingDisclosure(documentRef, thinking) {
    const details = documentRef.createElement('details');
    details.className = 'message-thinking';
    const summary = documentRef.createElement('summary');
    summary.appendChild(createIcon(documentRef, 'brain', { size: 17 }));
    const label = documentRef.createElement('span');
    label.textContent = '思考过程';
    summary.appendChild(label);
    const content = documentRef.createElement('pre');
    content.className = 'message-thinking-content';
    content.textContent = String(thinking || '');
    details.appendChild(summary);
    details.appendChild(content);
    details.classList.toggle('hidden', !content.textContent);
    return { details, content };
}

function messageContainerFromTarget(target) {
    if (!target) return null;
    if (typeof target.closest === 'function') return target.closest('.msg');
    let current = target;
    while (current) {
        if (current.classList && current.classList.contains('msg')) return current;
        current = current.parentElement || current.parentNode || null;
    }
    return null;
}

export function updateMessageThinking(target, value, options = {}) {
    const container = messageContainerFromTarget(target);
    if (!container || typeof container.querySelector !== 'function') return false;
    const details = container.querySelector('.message-thinking');
    const content = container.querySelector('.message-thinking-content');
    if (!details || !content) return false;
    content.textContent = String(value || '');
    details.classList.toggle('hidden', !content.textContent);
    if (content.textContent && options.expand === true) details.open = true;
    if (!content.textContent) details.open = false;
    return true;
}

export function createMessageElement(documentRef, {
    role,
    content,
    thinking = '',
    message = {},
    timeText = '',
}) {
    if (!documentRef || !['user', 'assistant'].includes(role)) throw new TypeError('消息渲染参数无效');
    const presentation = messagePresentation(role, message);
    const container = documentRef.createElement('article');
    container.className = `msg ${role} message-${presentation.kind}`;
    container.dataset.messageId = message.id || '';
    container.dataset.presentation = presentation.kind;
    const status = MESSAGE_STATES.has(message.status) ? message.status : '';
    if (status) container.dataset.status = status;

    const includeControl = documentRef.createElement('label');
    includeControl.className = 'msg-include-control';
    const checkbox = documentRef.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'msg-checkbox';
    checkbox.checked = message.in_prompt !== false;
    checkbox.title = '纳入上下文';
    checkbox.setAttribute('aria-label', '包含在 Prompt 中');
    const includeText = documentRef.createElement('span');
    includeText.className = 'sr-only';
    includeText.textContent = '纳入上下文';
    includeControl.appendChild(checkbox);
    includeControl.appendChild(includeText);
    container.appendChild(includeControl);

    const actions = documentRef.createElement('div');
    actions.className = 'msg-actions';
    const collapse = actionButton(documentRef, 'collapse', '折叠消息', 'chevronDown');
    collapse.setAttribute('aria-expanded', String(message.collapsed !== true));
    collapse.addEventListener('click', () => {
        const collapsed = container.classList.toggle('collapsed');
        collapse.setAttribute('aria-expanded', String(!collapsed));
        collapse.setAttribute('aria-label', collapsed ? '展开消息' : '折叠消息');
        collapse.title = collapsed ? '展开消息' : '折叠消息';
    });
    actions.appendChild(collapse);
    actions.appendChild(actionButton(documentRef, 'edit', '编辑', 'edit'));
    actions.appendChild(actionButton(documentRef, 'regenerate', role === 'user' ? '从此条之后重新生成' : '重新生成', 'regenerate'));
    const pin = actionButton(documentRef, 'pin', '钉选为常驻记忆', 'pin');
    if (message.pinned) pin.classList.add('active');
    actions.appendChild(pin);
    actions.appendChild(actionButton(documentRef, 'delete', '删除', 'trash'));
    container.appendChild(actions);

    const header = documentRef.createElement('header');
    header.className = 'msg-header';
    const roleElement = documentRef.createElement('div');
    roleElement.className = 'msg-role';
    roleElement.textContent = presentation.label;
    header.appendChild(roleElement);
    if (role === 'assistant' && timeText) {
        const time = documentRef.createElement('time');
        time.className = 'msg-time';
        time.textContent = String(timeText);
        header.appendChild(time);
    }
    if (status && status !== 'completed') {
        const state = documentRef.createElement('span');
        state.className = `msg-state state-${status}`;
        state.textContent = ({ pending: '等待', streaming: '书写中', cancelled: '已取消', failed: '失败' })[status] || status;
        header.appendChild(state);
    }
    container.appendChild(header);

    const body = documentRef.createElement('div');
    body.className = 'msg-body';
    const contentElement = documentRef.createElement('div');
    contentElement.className = 'content';
    contentElement.textContent = String(content || '');
    body.appendChild(contentElement);
    if (role === 'assistant') {
        const disclosure = createThinkingDisclosure(documentRef, thinking || message.thinking || '');
        body.appendChild(disclosure.details);
    }
    container.appendChild(body);
    container.classList.toggle('collapsed', message.collapsed === true);
    return { container, contentElement };
}
