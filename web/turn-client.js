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
        'roleplay',
    ]);

    function isObject(value) {
        return Boolean(value && typeof value === 'object' && !Array.isArray(value));
    }

    function sanitizeRoleplayWarnings(value) {
        if (!Array.isArray(value)) return null;
        return Object.freeze(value.map(warning => {
            if (!isObject(warning)) return warning;
            const clean = {};
            for (const key of ['code', 'action', 'character_id']) {
                if (typeof warning[key] === 'string') clean[key] = warning[key];
            }
            if (Array.isArray(warning.candidate_ids)) {
                clean.candidate_ids = Object.freeze(warning.candidate_ids.filter(id => typeof id === 'string'));
            }
            return Object.freeze(clean);
        }));
    }

    function normalizeParsedEvent(event) {
        const modern = Object.prototype.hasOwnProperty.call(event, 'revision')
            || Object.prototype.hasOwnProperty.call(event, 'session_delta');
        const normalized = {
            parsed: event.parsed,
            roleplay_warnings: sanitizeRoleplayWarnings(event.roleplay_warnings) || Object.freeze([]),
            revision: modern ? event.revision : null,
            session_delta: modern ? Object.freeze({ ...event.session_delta }) : null,
            // 仅旧事件日志缺少 delta/revision 时保留完整 Session 供兼容重放。
            legacySession: !modern && isObject(event.session) ? event.session : null,
        };
        return Object.freeze(normalized);
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
        if (event.type === 'parsed') {
            const hasRevision = Object.prototype.hasOwnProperty.call(event, 'revision');
            const hasDelta = Object.prototype.hasOwnProperty.call(event, 'session_delta');
            const modern = hasRevision || hasDelta;
            if (!isObject(event.parsed)
                || (modern && !Array.isArray(event.roleplay_warnings))) {
                return 'parsed 事件结构无效';
            }
            if (event.roleplay_warnings !== undefined
                && (!Array.isArray(event.roleplay_warnings)
                    || event.roleplay_warnings.some(warning => !isObject(warning)))) {
                return 'parsed 角色警告无效';
            }
            if (modern) {
                if (!hasRevision || !hasDelta
                    || !Number.isSafeInteger(event.revision) || event.revision < 0
                    || !isObject(event.session_delta)) {
                    return 'parsed delta/revision 无效';
                }
            } else if (!isObject(event.session)) {
                return 'parsed 事件缺少 delta 或旧 Session';
            }
        }
        return true;
    }

    function createTurnState(turn, ref) {
        const validation = validateTurn(turn);
        if (validation !== true) throw new ApiError(validation, { code: 'invalid_turn_schema', payload: turn });
        const lastEventId = Number.isSafeInteger(turn.last_event_id) && turn.last_event_id >= 0
            ? turn.last_event_id
            : 0;
        const terminal = TERMINAL_STATUSES.includes(turn.status);
        return {
            turnId: turn.turn_id,
            ref,
            status: turn.status,
            lastEventId,
            content: typeof turn.content === 'string' ? turn.content : '',
            thinking: typeof turn.thinking === 'string' ? turn.thinking : '',
            parsed: null,
            error: turn.error || null,
            // Prompt 诊断不参与持久指针；由具体视图继续做字段白名单脱敏。
            promptDiagnostics: isObject(turn.prompt_diagnostics) ? turn.prompt_diagnostics : null,
            terminal,
            terminalCount: terminal ? 1 : 0,
            syncPending: terminal,
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
        else if (event.type === 'parsed') next.parsed = normalizeParsedEvent(event);
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
            next.syncPending = true;
        }
        return { state: next, accepted: true, duplicate: false, gap };
    }

    function canPerformAction(action, status) {
        if (!ACTIVE_STATUSES.includes(status)) return true;
        if (action === 'cancel') return true;
        return !BLOCKED_ACTIONS.has(action);
    }

    function turnLocksSession(turn, ref) {
        if (!turn || !turn.ref || !ref) return false;
        const sameRef = turn.ref.project === ref.project
            && turn.ref.save === ref.save
            && turn.ref.epoch === ref.epoch;
        return sameRef && (!turn.terminal || turn.syncPending === true);
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

        regenerate(payload, options = {}) {
            return this.api.post('/api/chat/turns/regenerate', payload, {
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
        normalizeParsedEvent,
        createTurnState,
        reduceTurnEvent,
        canPerformAction,
        turnLocksSession,
    };
});
