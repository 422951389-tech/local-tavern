import {
    createProductActionButton,
    createProductElement,
    formatProductDate,
    productLines,
} from './product-ui.mjs';

export function createMemoryController(options) {
    const {
        documentRef,
        service,
        showModal,
        showToast,
        captureSessionRef,
        sessionBelongsToRef,
        getSession,
        getCharacters,
        isCurrentSessionRef,
        applySessionResult,
        currentRevision,
        reloadCurrentSession,
        errorDetail,
        confirmAction = message => globalThis.confirm(message),
    } = options;
    const element = (tag, className = '', text = null) => (
        createProductElement(documentRef, tag, className, text)
    );
    const actionButton = (label, className = 'secondary-btn', iconName = null) => (
        createProductActionButton(documentRef, label, className, iconName)
    );

    async function showCenter() {
        const requestRef = captureSessionRef();
        if (!sessionBelongsToRef(getSession(), requestRef)) {
            showToast('当前存档尚未加载完成');
            return;
        }
        const body = element('div', 'product-tool memory-notes-center');
        const form = element('section', 'memory-note-form product-section');
        form.appendChild(element('h3', '', '写一条长期记忆'));
        const textLabel = element('label', 'product-field');
        textLabel.appendChild(element('span', 'product-field-label', '记忆内容'));
        const textInput = element('textarea');
        textInput.rows = 3;
        textInput.maxLength = 50000;
        textInput.placeholder = '记录稳定事实、长期约定或重要偏好';
        textLabel.appendChild(textInput);
        const characterLabel = element('label', 'product-field');
        characterLabel.appendChild(element('span', 'product-field-label', '关联角色（可选）'));
        const characterSelect = element('select');
        characterSelect.appendChild(element('option', '', '不关联角色'));
        characterSelect.firstChild.value = '';
        for (const character of getCharacters()) {
            const id = String(character.id || '').trim();
            if (!id) continue;
            const option = element('option', '', character.name || id);
            option.value = id;
            characterSelect.appendChild(option);
        }
        characterLabel.appendChild(characterSelect);
        const timeLabel = element('label', 'product-field');
        timeLabel.appendChild(element('span', 'product-field-label', '时间线索（可选）'));
        const timeInput = element('input');
        timeInput.type = 'text';
        timeInput.maxLength = 1000;
        timeLabel.appendChild(timeInput);
        const factsLabel = element('label', 'product-field');
        factsLabel.appendChild(element('span', 'product-field-label', '事实（每行一条）'));
        const factsInput = element('textarea');
        factsInput.rows = 2;
        factsLabel.appendChild(factsInput);
        const relationsLabel = element('label', 'product-field');
        relationsLabel.appendChild(element('span', 'product-field-label', '关系（每行一条）'));
        const relationsInput = element('textarea');
        relationsInput.rows = 2;
        relationsLabel.appendChild(relationsInput);
        const actions = element('div', 'product-row-actions');
        const save = actionButton('添加记忆', 'primary-btn', 'plus');
        const cancelEdit = actionButton('取消编辑');
        cancelEdit.hidden = true;
        actions.append(save, cancelEdit);
        const formStatus = element('p', 'product-card-status');
        formStatus.setAttribute('role', 'status');
        formStatus.setAttribute('aria-live', 'polite');
        form.append(textLabel, characterLabel, timeLabel, factsLabel, relationsLabel, actions, formStatus);
        const list = element('section', 'memory-note-list product-section');
        list.appendChild(element('h3', '', '已保存记忆'));
        const noteItems = element('div', 'memory-note-items');
        list.appendChild(noteItems);
        body.append(form, list);
        let editingId = '';

        const resetForm = () => {
            editingId = '';
            textInput.value = '';
            characterSelect.value = '';
            timeInput.value = '';
            factsInput.value = '';
            relationsInput.value = '';
            save.querySelector('span').textContent = '添加记忆';
            cancelEdit.hidden = true;
            formStatus.textContent = '';
        };
        const draft = () => ({
            text: textInput.value,
            characterId: characterSelect.value,
            time: timeInput.value,
            facts: productLines(factsInput.value),
            relations: productLines(relationsInput.value),
        });
        let loadNotes;
        const recoverConflict = async () => {
            await reloadCurrentSession(requestRef);
            formStatus.textContent = '版本冲突，已刷新当前存档；你的草稿仍保留在表单中，请再次保存。';
            await loadNotes();
        };
        const mutate = async work => {
            save.disabled = true;
            try {
                const result = await work();
                if (!isCurrentSessionRef(requestRef)) return false;
                if (!applySessionResult(result.session, requestRef)) throw new Error('记忆响应不属于当前存档');
                resetForm();
                await loadNotes();
                return true;
            } catch (error) {
                if (error && error.code === 'revision_conflict' && isCurrentSessionRef(requestRef)) {
                    await recoverConflict();
                } else {
                    formStatus.textContent = `保存失败：${errorDetail(error)}`;
                }
                return false;
            } finally {
                save.disabled = false;
            }
        };
        loadNotes = async () => {
            noteItems.replaceChildren();
            const snapshot = await service.list(requestRef);
            if (!snapshot.notes.length) {
                noteItems.appendChild(element('div', 'product-empty', '当前存档还没有长期记忆便签。'));
                return;
            }
            for (const note of snapshot.notes) {
                const card = element('article', 'memory-note-card');
                const name = getCharacters().find(character => character.id === note.characterId)?.name || note.characterId;
                const meta = [name ? `角色：${name}` : '', note.time, note.editedAt ? `编辑：${formatProductDate(note.editedAt)}` : ''].filter(Boolean).join(' · ');
                card.appendChild(element('p', 'memory-note-text', note.text));
                if (meta) card.appendChild(element('p', 'product-help', meta));
                if (note.facts.length) card.appendChild(element('p', 'memory-note-detail', `事实：${note.facts.join('；')}`));
                if (note.relations.length) card.appendChild(element('p', 'memory-note-detail', `关系：${note.relations.join('；')}`));
                const row = element('div', 'product-row-actions');
                const edit = actionButton('编辑');
                const remove = actionButton('删除', 'danger-outline-btn');
                edit.addEventListener('click', () => {
                    editingId = note.id;
                    textInput.value = note.text;
                    characterSelect.value = note.characterId;
                    timeInput.value = note.time;
                    factsInput.value = note.facts.join('\n');
                    relationsInput.value = note.relations.join('\n');
                    save.querySelector('span').textContent = '保存修改';
                    cancelEdit.hidden = false;
                    formStatus.textContent = `正在编辑 ${note.id.slice(0, 8)}`;
                    textInput.focus();
                });
                remove.addEventListener('click', async () => {
                    if (!confirmAction('删除这条长期记忆？')) return;
                    remove.disabled = true;
                    try {
                        const result = await service.remove(requestRef, currentRevision(requestRef), note.id);
                        if (!isCurrentSessionRef(requestRef)) return;
                        if (!applySessionResult(result.session, requestRef)) throw new Error('删除响应不属于当前存档');
                        await loadNotes();
                        showToast('长期记忆已删除');
                    } catch (error) {
                        if (error && error.code === 'revision_conflict' && isCurrentSessionRef(requestRef)) await recoverConflict();
                        else formStatus.textContent = `删除失败：${errorDetail(error)}`;
                    } finally {
                        remove.disabled = false;
                    }
                });
                row.append(edit, remove);
                card.appendChild(row);
                noteItems.appendChild(card);
            }
        };
        save.addEventListener('click', () => {
            if (!textInput.value.trim()) {
                textInput.setAttribute('aria-invalid', 'true');
                formStatus.textContent = '请填写记忆内容';
                textInput.focus();
                return;
            }
            textInput.removeAttribute('aria-invalid');
            void mutate(() => editingId
                ? service.update(requestRef, currentRevision(requestRef), editingId, draft())
                : service.create(requestRef, currentRevision(requestRef), draft()));
        });
        cancelEdit.addEventListener('click', resetForm);
        showModal({ title: '长期记忆便签', body });
        try { await loadNotes(); }
        catch (error) { formStatus.textContent = `读取记忆失败：${errorDetail(error)}`; }
    }

    return Object.freeze({ showCenter });
}
