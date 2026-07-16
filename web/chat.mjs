export function createTurnPayload({ ref, revision, model, params, userInput }) {
    if (!ref || typeof ref.project !== 'string' || typeof ref.save !== 'string') {
        throw new TypeError('回合请求缺少 SessionRef');
    }
    const payload = {
        project: ref.project,
        save: ref.save,
        expected_revision: Number.isInteger(revision) && revision >= 0 ? revision : 0,
        model: model || null,
        ...(params || {}),
    };
    if (userInput !== undefined) payload.user_input = String(userInput);
    return payload;
}

export function createTurnPersistence(storage, key = 'local-tavern.active-turn.v1') {
    if (!storage || typeof storage.getItem !== 'function' || typeof storage.setItem !== 'function') {
        throw new TypeError('回合持久化需要 Storage');
    }

    function read() {
        let parsed;
        try { parsed = JSON.parse(storage.getItem(key) || 'null'); }
        catch (_error) { return null; }
        if (!parsed || typeof parsed !== 'object') return null;
        const turnId = parsed.turnId || parsed.turn_id;
        if (typeof turnId !== 'string' || typeof parsed.project !== 'string' || typeof parsed.save !== 'string') {
            return null;
        }
        const cursor = parsed.lastEventId ?? parsed.last_event_id;
        const lastEventId = Number.isSafeInteger(cursor) && cursor >= 0
            ? cursor
            : 0;
        return Object.freeze({
            turnId,
            project: parsed.project,
            save: parsed.save,
            lastEventId,
        });
    }

    function write(turn) {
        if (!turn || !turn.turnId || !turn.ref) throw new TypeError('活动回合无效');
        const value = {
            turnId: turn.turnId,
            project: turn.ref.project,
            save: turn.ref.save,
            lastEventId: Number.isSafeInteger(turn.lastEventId) ? turn.lastEventId : 0,
        };
        storage.setItem(key, JSON.stringify(value));
        return value;
    }

    function clear() {
        storage.removeItem(key);
    }

    return Object.freeze({ read, write, clear, key });
}
