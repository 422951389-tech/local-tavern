(function attachTavernTurn(root, factory) {
    const apiModule = typeof module === 'object' && module.exports
        ? require('./api-client.js')
        : root.TavernApi;
    const api = factory(apiModule);
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.TavernTurn = Object.freeze(api);
})(typeof globalThis !== 'undefined' ? globalThis : this, function createTavernTurn(apiModule) {
    'use strict';

    if (!apiModule) throw new Error('TurnClient 需要 TavernApi');
    const { ApiError, parseJsonText, readSse } = apiModule;
    const ACTIVE_STATUSES = Object.freeze(['pending', 'streaming']);
    const TERMINAL_STATUSES = Object.freeze(['completed', 'cancelled', 'failed']);
    const BLOCKED_ACTIONS = new Set([
        'send', 'model', 'reset', 'rename', 'delete', 'restore',
        'message_edit', 'message_delete', 'pin', 'include', 'regenerate',
        'summary', 'project_switch', 'save_switch', 'card_write',
    ]);

    function isObject(value) {
        return Boolean(value && typeof value === 'object' && !Array.isArray(value));
    }

    function validateTurn(turn) {
        if (!isObject(turn)) return 'turn 响应必须是对象';
        if (typeof turn.turn_id !== 'string' || !turn.turn_id) return 'turn_id 缺失';
        if (typeof turn.project !== 'string' || typeof turn.save !== 'string') return 'turn 归属缺失';
        if (![...ACTIVE_STATUSES, ...TERMINAL_STATUSES].includes(turn.status)) return 'turn 状态无效';
        if (typeof turn.events_url !== 'string' || typeof turn.cancel_url !== 'string') return 'turn URL 缺失';
        return true;
    }

    function validateEvent(event) {
        if (!isObject(event)) return 'turn 事件必须是对象';
        if (!Number.isSafeInteger(event.id) || event.id <= 0) return 'turn 事件 ID 无效';
        if (typeof event.type !== 'string' || !event.type) return 'turn 事件类型缺失';
        return true;
    }

    function createTurnState(turn, ref) {
        const validation = validateTurn(turn);
        if (validation !== true) throw new ApiError(validation, { code: 'invalid_turn_schema', payload: turn });
        const lastEventId = Number.isSafeInteger(turn.last_event_id) && turn.last_event_id >= 0
            ? turn.last_event_id
            : 0;
        return {
            turnId: turn.turn_id,
            ref,
            status: turn.status,
            lastEventId,
            content: typeof turn.content === 'string' ? turn.content : '',
            thinking: typeof turn.thinking === 'string' ? turn.thinking : '',
            parsed: null,
            error: turn.error || null,
            terminal: TERMINAL_STATUSES.includes(turn.status),
            terminalCount: TERMINAL_STATUSES.includes(turn.status) ? 1 : 0,
        };
    }

    function reduceTurnEvent(previous, event) {
        const validation = validateEvent(event);
        if (validation !== true) throw new ApiError(validation, { code: 'invalid_turn_event', payload: event });
        if (event.id <= previous.lastEventId) {
            return { state: previous, accepted: false, duplicate: true, gap: false };
        }
        if (previous.terminal) {
            return { state: previous, accepted: false, duplicate: false, gap: true };
        }

        const next = { ...previous, lastEventId: event.id };
        const gap = event.id !== previous.lastEventId + 1;
        if (event.type === 'started') next.status = 'streaming';
        else if (event.type === 'content') next.content += String(event.content || '');
        else if (event.type === 'thinking') next.thinking += String(event.content || '');
        else if (event.type === 'parsed') next.parsed = event;
        else if (event.type === 'terminal') {
            if (!TERMINAL_STATUSES.includes(event.status)) {
                throw new ApiError('turn terminal 状态无效', {
                    code: 'invalid_turn_event', payload: event,
                });
            }
            next.status = event.status;
            next.error = event.error || null;
            next.terminal = true;
            next.terminalCount = previous.terminalCount + 1;
        }
        return { state: next, accepted: true, duplicate: false, gap };
    }

    function canPerformAction(action, status) {
        if (!ACTIVE_STATUSES.includes(status)) return true;
        if (action === 'cancel') return true;
        return !BLOCKED_ACTIONS.has(action);
    }

    class TurnClient {
        constructor(apiClient) {
            if (!apiClient || typeof apiClient.request !== 'function') {
                throw new TypeError('TurnClient 需要 ApiClient 实例');
            }
            this.api = apiClient;
            this.cancelRequests = new Map();
        }

        create(payload, options = {}) {
            return this.api.post('/api/chat/turns', payload, {
                ...options,
                schema: validateTurn,
            });
        }

        get(turnId, options = {}) {
            return this.api.get(`/api/chat/turns/${encodeURIComponent(turnId)}`, {
                ...options,
                schema: validateTurn,
            });
        }

        cancel(turnId, options = {}) {
            if (this.cancelRequests.has(turnId)) return this.cancelRequests.get(turnId);
            const request = this.api.post(
                `/api/chat/turns/${encodeURIComponent(turnId)}/cancel`,
                undefined,
                { ...options, schema: validateTurn },
            );
            this.cancelRequests.set(turnId, request);
            request.catch(() => this.cancelRequests.delete(turnId));
            return request;
        }

        async *events(turnId, options = {}) {
            const after = Number.isSafeInteger(options.after) && options.after >= 0 ? options.after : 0;
            const response = await this.api.get(
                `/api/chat/turns/${encodeURIComponent(turnId)}/events?after=${after}`,
                {
                    signal: options.signal,
                    timeoutMs: 0,
                    responseType: 'response',
                },
            );
            for await (const frame of readSse(response, { signal: options.signal })) {
                const event = parseJsonText(frame.data, {
                    code: 'invalid_turn_event',
                    url: response.url || '',
                    status: response.status,
                });
                const validation = validateEvent(event);
                if (validation !== true) {
                    throw new ApiError(validation, { code: 'invalid_turn_event', payload: event });
                }
                if (frame.id && Number(frame.id) !== event.id) {
                    throw new ApiError('SSE ID 与事件 ID 不一致', {
                        code: 'turn_event_id_mismatch', payload: event,
                    });
                }
                yield event;
            }
        }
    }

    return {
        ACTIVE_STATUSES,
        TERMINAL_STATUSES,
        BLOCKED_ACTIONS: Object.freeze([...BLOCKED_ACTIONS]),
        TurnClient,
        validateTurn,
        validateEvent,
        createTurnState,
        reduceTurnEvent,
        canPerformAction,
    };
});
