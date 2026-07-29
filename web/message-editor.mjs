function message(error) {
    return error instanceof Error && error.message ? error.message : String(error || '保存失败');
}

export function enterMessageEditor({
    documentRef,
    messageElement,
    messageRef,
    save,
    initialValue,
    initialError,
    recover,
}) {
    if (!documentRef || !messageElement || !messageRef || typeof save !== 'function') {
        throw new TypeError('消息编辑器参数不完整');
    }
    if (messageElement.dataset.editorState) return null;
    const content = messageElement.querySelector('.content');
    if (!content) throw new TypeError('消息节点缺少 .content');

    const renderedOriginal = content.textContent;
    const editableOriginal = initialValue === undefined
        ? renderedOriginal
        : String(initialValue);
    const textarea = documentRef.createElement('textarea');
    textarea.className = 'message-edit-input';
    textarea.value = editableOriginal;
    textarea.setAttribute('aria-label', '编辑消息内容');
    const error = documentRef.createElement('div');
    error.className = 'message-edit-error hidden';
    error.setAttribute('role', 'alert');
    error.setAttribute('aria-live', 'assertive');
    const hint = documentRef.createElement('p');
    hint.className = 'message-edit-hint';
    hint.textContent = 'Ctrl+Enter 保存 · Esc 取消；离开输入框不会自动保存';
    const actions = documentRef.createElement('div');
    actions.className = 'message-edit-actions';
    const cancelButton = documentRef.createElement('button');
    cancelButton.type = 'button';
    cancelButton.className = 'message-edit-cancel';
    cancelButton.textContent = '取消';
    const saveButton = documentRef.createElement('button');
    saveButton.type = 'button';
    saveButton.className = 'message-edit-save';
    saveButton.textContent = '保存修改';
    actions.appendChild(cancelButton);
    actions.appendChild(saveButton);

    messageElement.classList.add('editing');
    messageElement.dataset.editorState = 'editing';
    content.replaceWith(textarea);
    textarea.insertAdjacentElement('afterend', error);
    error.insertAdjacentElement('afterend', hint);
    hint.insertAdjacentElement('afterend', actions);
    if (initialError) {
        error.textContent = String(initialError);
        error.classList.remove('hidden');
    }
    textarea.focus();

    let inFlight = null;

    function finish(finalText, { preserveRenderedContent = false } = {}) {
        if (!messageElement.dataset.editorState) return;
        delete messageElement.dataset.editorState;
        messageElement.classList.remove('editing');
        messageElement.removeAttribute('aria-busy');
        if (!preserveRenderedContent) content.textContent = finalText;
        if (textarea.isConnected) textarea.replaceWith(content);
        if (error.isConnected) error.remove();
        if (hint.isConnected) hint.remove();
        if (actions.isConnected) actions.remove();
    }

    function cancel() {
        if (messageElement.dataset.editorState === 'saving') return false;
        finish(editableOriginal, { preserveRenderedContent: true });
        return true;
    }

    function commit() {
        if (inFlight) return inFlight;
        if (messageElement.dataset.editorState !== 'editing') return Promise.resolve(false);
        const next = textarea.value;
        if (next === editableOriginal) {
            finish(editableOriginal, { preserveRenderedContent: true });
            return Promise.resolve(true);
        }

        messageElement.dataset.editorState = 'saving';
        messageElement.setAttribute('aria-busy', 'true');
        textarea.disabled = true;
        saveButton.disabled = true;
        cancelButton.disabled = true;
        error.textContent = '';
        error.classList.add('hidden');
        inFlight = Promise.resolve()
            .then(() => save(messageRef, next))
            .then(() => {
                finish(next);
                return true;
            })
            .catch(caught => {
                if (!textarea.isConnected) {
                    delete messageElement.dataset.editorState;
                    messageElement.classList.remove('editing');
                    messageElement.removeAttribute('aria-busy');
                    if (typeof recover === 'function') {
                        return Promise.resolve(recover({
                            messageRef,
                            draft: next,
                            error: caught,
                        })).then(() => false);
                    }
                    return false;
                }
                messageElement.dataset.editorState = 'editing';
                messageElement.removeAttribute('aria-busy');
                textarea.disabled = false;
                saveButton.disabled = false;
                cancelButton.disabled = false;
                error.textContent = `保存失败：${message(caught)}`;
                error.classList.remove('hidden');
                textarea.focus();
                return false;
            })
            .finally(() => { inFlight = null; });
        return inFlight;
    }

    textarea.addEventListener('keydown', event => {
        if (
            event.key === 'Enter'
            && (event.ctrlKey || event.metaKey)
            && !event.isComposing
            && event.keyCode !== 229
        ) {
            event.preventDefault();
            void commit();
        } else if (event.key === 'Escape') {
            event.preventDefault();
            cancel();
        }
    });
    saveButton.addEventListener('click', () => { void commit(); });
    cancelButton.addEventListener('click', cancel);
    return Object.freeze({
        textarea,
        error,
        hint,
        actions,
        saveButton,
        cancelButton,
        commit,
        cancel,
        state: () => messageElement.dataset.editorState || 'finished',
    });
}
