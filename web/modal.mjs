function requireElement(value, name) {
    if (!value) throw new TypeError(`Modal 缺少 ${name}`);
    return value;
}

function errorMessage(error) {
    if (error instanceof Error && error.message) return error.message;
    if (typeof error === 'string' && error.trim()) return error.trim();
    return '操作失败，请重试';
}

function focusFirstInput(body, fallback) {
    const candidate = focusableElements(body)[0];
    const target = candidate || fallback;
    if (target && typeof target.focus === 'function') target.focus();
}

const FOCUSABLE_SELECTOR = [
    'a[href]',
    'button:not([disabled])',
    'input:not([disabled])',
    'textarea:not([disabled])',
    'select:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
    '[contenteditable="true"]',
].join(', ');

function isFocusable(element) {
    if (!element || element.disabled || element.hidden) return false;
    if (element.type === 'hidden') return false;
    if (typeof element.matches === 'function' && element.matches(':disabled')) return false;
    if (typeof element.getAttribute === 'function') {
        if (element.getAttribute('aria-hidden') === 'true') return false;
        if (element.getAttribute('tabindex') === '-1') return false;
    }
    if (typeof element.closest === 'function' && element.closest('[hidden], .hidden, [inert]')) return false;
    const view = element.ownerDocument && element.ownerDocument.defaultView;
    if (view && typeof view.getComputedStyle === 'function') {
        const style = view.getComputedStyle(element);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
    }
    return typeof element.focus === 'function';
}

function focusableElements(dialog) {
    if (!dialog || typeof dialog.querySelectorAll !== 'function') return [];
    return Array.from(dialog.querySelectorAll(FOCUSABLE_SELECTOR)).filter(isFocusable);
}

export function createModalController(elements, options = {}) {
    const backdrop = requireElement(elements.backdrop, 'backdrop');
    const dialog = requireElement(elements.dialog, 'dialog');
    const title = requireElement(elements.title, 'title');
    const body = requireElement(elements.body, 'body');
    const footer = requireElement(elements.footer, 'footer');
    const cancelButton = requireElement(elements.cancelButton, 'cancelButton');
    const confirmButton = requireElement(elements.confirmButton, 'confirmButton');
    const closeButton = requireElement(elements.closeButton, 'closeButton');
    const error = requireElement(elements.error, 'error');
    const documentRef = options.documentRef || globalThis.document;

    let confirmHandler = null;
    let pending = false;
    let bound = false;
    let returnFocus = null;

    function setError(message = '') {
        error.textContent = String(message || '');
        error.classList.toggle('hidden', !error.textContent);
    }

    function setPending(value) {
        pending = Boolean(value);
        backdrop.setAttribute('aria-busy', String(pending));
        dialog.setAttribute('aria-busy', String(pending));
        confirmButton.disabled = pending;
        cancelButton.disabled = pending;
        closeButton.disabled = pending;
        confirmButton.textContent = pending
            ? (confirmButton.dataset.pendingText || '处理中…')
            : (confirmButton.dataset.idleText || '确定');
    }

    function hide({ force = false, restoreFocus = true } = {}) {
        if (pending && !force) return false;
        backdrop.classList.add('hidden');
        backdrop.setAttribute('aria-hidden', 'true');
        body.replaceChildren();
        title.textContent = '';
        confirmHandler = null;
        setError('');
        setPending(false);
        if (restoreFocus && returnFocus && typeof returnFocus.focus === 'function') returnFocus.focus();
        returnFocus = null;
        return true;
    }

    async function confirm() {
        if (pending || typeof confirmHandler !== 'function') return false;
        setError('');
        setPending(true);
        try {
            const result = await confirmHandler();
            if (result === false) {
                setError('操作未完成，请检查输入后重试');
                setPending(false);
                focusFirstInput(body, confirmButton);
                return false;
            }
            hide({ force: true });
            return true;
        } catch (caught) {
            setError(errorMessage(caught));
            setPending(false);
            focusFirstInput(body, confirmButton);
            return false;
        }
    }

    function show(config) {
        if (!config || typeof config !== 'object') throw new TypeError('Modal 配置必须是对象');
        if (pending) return false;
        const replacing = !backdrop.classList.contains('hidden');
        const replacementReturnFocus = replacing ? returnFocus : null;
        if (replacing) hide({ force: true, restoreFocus: false });
        returnFocus = replacing
            ? replacementReturnFocus
            : (documentRef && documentRef.activeElement ? documentRef.activeElement : null);
        title.textContent = String(config.title || '');
        body.replaceChildren();
        if (typeof config.body === 'string') body.textContent = config.body;
        else if (config.body) body.appendChild(config.body);
        setError('');

        const footerConfig = config.footer || null;
        confirmHandler = footerConfig && typeof footerConfig.onConfirm === 'function'
            ? footerConfig.onConfirm
            : null;
        footer.classList.toggle('hidden', !footerConfig);
        if (footerConfig) {
            cancelButton.textContent = footerConfig.cancelText || '取消';
            const idleText = footerConfig.confirmText || '确定';
            confirmButton.dataset.idleText = idleText;
            confirmButton.dataset.pendingText = footerConfig.pendingText || '处理中…';
            confirmButton.textContent = idleText;
            confirmButton.disabled = !confirmHandler;
        }
        setPending(false);
        if (footerConfig && !confirmHandler) confirmButton.disabled = true;
        backdrop.classList.remove('hidden');
        backdrop.setAttribute('aria-hidden', 'false');
        focusFirstInput(body, closeButton);
        return true;
    }

    function bind() {
        if (bound) return;
        bound = true;
        confirmButton.addEventListener('click', () => { void confirm(); });
        cancelButton.addEventListener('click', () => { hide(); });
        closeButton.addEventListener('click', () => { hide(); });
        backdrop.addEventListener('click', event => {
            if (event.target === event.currentTarget) hide();
        });
        if (documentRef && typeof documentRef.addEventListener === 'function') {
            documentRef.addEventListener('keydown', event => {
                if (backdrop.classList.contains('hidden')) return;
                if (event.key === 'Escape') {
                    event.preventDefault();
                    hide();
                    return;
                }
                if (event.key !== 'Tab') return;

                const candidates = focusableElements(dialog);
                if (candidates.length === 0) {
                    event.preventDefault();
                    if (typeof dialog.focus === 'function') dialog.focus();
                    return;
                }

                const first = candidates[0];
                const last = candidates[candidates.length - 1];
                const active = documentRef.activeElement;
                const activeInside = typeof dialog.contains === 'function'
                    ? dialog.contains(active)
                    : candidates.includes(active);
                if (event.shiftKey && (!activeInside || active === first)) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && (!activeInside || active === last)) {
                    event.preventDefault();
                    first.focus();
                }
            });
        }
    }

    return Object.freeze({
        bind,
        show,
        hide,
        confirm,
        setError,
        setPending,
        isPending: () => pending,
    });
}

export function modalElementsFromDocument(documentRef = globalThis.document) {
    if (!documentRef) throw new TypeError('缺少 document');
    return {
        backdrop: documentRef.getElementById('modal-backdrop'),
        dialog: documentRef.getElementById('modal'),
        title: documentRef.getElementById('modal-title'),
        body: documentRef.getElementById('modal-body'),
        footer: documentRef.getElementById('modal-footer'),
        cancelButton: documentRef.getElementById('modal-cancel'),
        confirmButton: documentRef.getElementById('modal-confirm'),
        closeButton: documentRef.getElementById('modal-close'),
        error: documentRef.getElementById('modal-error'),
    };
}
