(function attachTavernSessionRef(root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.TavernSessionRef = Object.freeze(api);
})(typeof globalThis !== 'undefined' ? globalThis : this, function createTavernSessionRef() {
    'use strict';

    function normalizePart(value, label, allowEmpty = false) {
        if (value === null && allowEmpty) return null;
        if (typeof value !== 'string' || value.length === 0) {
            throw new TypeError(`${label} 必须是非空字符串`);
        }
        return value;
    }

    function createSessionRef(project, save, epoch) {
        const normalizedProject = normalizePart(project, 'project');
        const normalizedSave = normalizePart(save, 'save', true);
        if (!Number.isSafeInteger(epoch) || epoch < 0) {
            throw new TypeError('epoch 必须是非负安全整数');
        }
        return Object.freeze({
            project: normalizedProject,
            save: normalizedSave,
            epoch,
        });
    }

    function sameSession(left, right) {
        return Boolean(left && right)
            && left.project === right.project
            && left.save === right.save;
    }

    function sameSessionRef(left, right) {
        return sameSession(left, right) && left.epoch === right.epoch;
    }

    function sessionBelongsToRef(session, ref) {
        if (!session || typeof session !== 'object' || !ref || !ref.save) return false;
        if (session.session_id !== ref.save) return false;
        return !session.project || session.project === ref.project;
    }

    function buildSessionCommit({
        session,
        ref,
        currentRef,
        currentSession = null,
        saveList = [],
    }) {
        if (!sameSessionRef(ref, currentRef)) return { accepted: false, reason: 'stale_ref' };
        if (!sessionBelongsToRef(session, ref)) return { accepted: false, reason: 'wrong_session' };
        if (!Number.isSafeInteger(session.revision) || session.revision < 0) {
            return { accepted: false, reason: 'invalid_revision' };
        }
        if (
            sessionBelongsToRef(currentSession, ref)
            && Number.isSafeInteger(currentSession.revision)
            && currentSession.revision > session.revision
        ) {
            return { accepted: false, reason: 'stale_revision' };
        }

        const nextSave = {
            session_id: session.session_id,
            name: session.name || session.session_id,
            message_count: Array.isArray(session.message_history) ? session.message_history.length : 0,
            updated_at: session.updated_at || '',
        };
        const nextSaveList = Array.isArray(saveList) ? saveList.map(item => ({ ...item })) : [];
        const index = nextSaveList.findIndex(item => item.session_id === session.session_id);
        if (index >= 0) nextSaveList[index] = { ...nextSaveList[index], ...nextSave };
        else nextSaveList.push(nextSave);

        return {
            accepted: true,
            reason: '',
            project: ref.project,
            save: ref.save,
            session,
            currentModel: session.current_model || '',
            messageHistory: Array.isArray(session.message_history) ? session.message_history : [],
            sceneMeta: session.scene_meta && typeof session.scene_meta === 'object' ? session.scene_meta : {},
            charactersState: session.characters_state && typeof session.characters_state === 'object'
                ? session.characters_state
                : {},
            saveList: nextSaveList,
        };
    }

    function shouldCreateDefaultSave(saves, candidateRef, currentRef) {
        return Array.isArray(saves)
            && saves.length === 0
            && sameSessionRef(candidateRef, currentRef);
    }

    class SessionRefTracker {
        constructor(project, save = null) {
            this._epoch = 0;
            this._current = createSessionRef(project, save, this._epoch);
        }

        capture() {
            return this._current;
        }

        advance(project = this._current.project, save = this._current.save) {
            this._epoch += 1;
            this._current = createSessionRef(project, save, this._epoch);
            return this._current;
        }

        invalidate() {
            return this.advance();
        }

        isCurrent(ref) {
            return sameSessionRef(this._current, ref);
        }
    }

    return {
        createSessionRef,
        sameSession,
        sameSessionRef,
        sessionBelongsToRef,
        buildSessionCommit,
        shouldCreateDefaultSave,
        SessionRefTracker,
    };
});
