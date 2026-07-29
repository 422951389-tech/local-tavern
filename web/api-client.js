(function attachTavernApi(root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.TavernApi = Object.freeze(api);
})(typeof globalThis !== 'undefined' ? globalThis : this, function createTavernApi() {
    'use strict';

    const DEFAULT_TIMEOUT_MS = 15000;

    class ApiError extends Error {
        constructor(message, options = {}) {
            super(message || '请求失败');
            this.name = 'ApiError';
            this.code = options.code || 'request_failed';
            this.status = Number.isInteger(options.status) ? options.status : 0;
            this.payload = options.payload ?? null;
            this.details = options.details ?? payloadDetails(this.payload, null);
            this.method = options.method || '';
            this.url = options.url || '';
            this.cause = options.cause;
        }

        get isAbort() {
            return this.code === 'request_aborted';
        }

        get isTimeout() {
            return this.code === 'request_timeout';
        }
    }

    function payloadCode(payload, fallback = 'request_failed') {
        if (!payload || typeof payload !== 'object') return fallback;
        if (payload.error && typeof payload.error === 'object' && payload.error.code) {
            return String(payload.error.code);
        }
        if (payload.detail && typeof payload.detail === 'object' && payload.detail.code) {
            return String(payload.detail.code);
        }
        if (typeof payload.code === 'string') return payload.code;
        return fallback;
    }

    function payloadMessage(payload, fallback = '请求失败') {
        if (!payload) return fallback;
        if (typeof payload === 'string') return payload || fallback;
        if (typeof payload.error === 'string') return payload.error || fallback;
        if (payload.error && typeof payload.error === 'object') {
            return payload.error.message || payload.error.code || fallback;
        }
        if (typeof payload.detail === 'string') return payload.detail;
        if (Array.isArray(payload.detail)) {
            const messages = payload.detail
                .map(item => item && (item.msg || item.message || item.type))
                .filter(Boolean);
            return messages.length ? messages.join('；') : fallback;
        }
        if (payload.detail && typeof payload.detail === 'object') {
            return payload.detail.message || payload.detail.code || fallback;
        }
        if (typeof payload.message === 'string') return payload.message;
        return fallback;
    }

    function payloadDetails(payload, fallback = null) {
        if (!payload || typeof payload !== 'object') return fallback;
        if (payload.error && typeof payload.error === 'object'
            && payload.error.details && typeof payload.error.details === 'object'
            && !Array.isArray(payload.error.details)) {
            return payload.error.details;
        }
        if (payload.detail && typeof payload.detail === 'object' && !Array.isArray(payload.detail)) {
            if (payload.detail.details && typeof payload.detail.details === 'object'
                && !Array.isArray(payload.detail.details)) {
                return payload.detail.details;
            }
            return payload.detail;
        }
        return fallback;
    }

    function parseJsonText(text, context = {}) {
        if (text === '') return null;
        try {
            return JSON.parse(text);
        } catch (cause) {
            throw new ApiError('服务端返回了无效 JSON', {
                ...context,
                code: 'invalid_json_response',
                cause,
            });
        }
    }

    async function readErrorPayload(response) {
        const text = await response.text();
        if (!text) return null;
        try {
            return JSON.parse(text);
        } catch (_error) {
            return text;
        }
    }

    function createRequestSignal(
        externalSignal,
        timeoutMs,
        setTimeoutImpl = setTimeout,
        clearTimeoutImpl = clearTimeout,
    ) {
        const controller = new AbortController();
        let timedOut = false;
        let timeoutId = null;

        const abortFromExternal = () => controller.abort(externalSignal && externalSignal.reason);
        if (externalSignal) {
            if (externalSignal.aborted) abortFromExternal();
            else externalSignal.addEventListener('abort', abortFromExternal, { once: true });
        }
        if (Number.isFinite(timeoutMs) && timeoutMs > 0) {
            timeoutId = setTimeoutImpl(() => {
                timedOut = true;
                controller.abort(new Error('request timeout'));
            }, timeoutMs);
        }

        return {
            signal: controller.signal,
            timedOut: () => timedOut,
            cleanup() {
                if (timeoutId !== null) clearTimeoutImpl(timeoutId);
                if (externalSignal) externalSignal.removeEventListener('abort', abortFromExternal);
            },
        };
    }

    function validateSchema(payload, schema, context) {
        if (typeof schema !== 'function') return payload;
        let result;
        try {
            result = schema(payload);
        } catch (cause) {
            throw new ApiError('响应结构校验失败', {
                ...context,
                code: 'invalid_response_schema',
                payload,
                cause,
            });
        }
        if (result === true || result === undefined) return payload;
        throw new ApiError(typeof result === 'string' ? result : '响应结构校验失败', {
            ...context,
            code: 'invalid_response_schema',
            payload,
        });
    }

    class ApiClient {
        constructor(options = {}) {
            this.baseUrl = options.baseUrl || '';
            this.fetchImpl = options.fetchImpl || (typeof fetch === 'function' ? fetch.bind(globalThis) : null);
            this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
            this.setTimeoutImpl = options.setTimeoutImpl || setTimeout;
            this.clearTimeoutImpl = options.clearTimeoutImpl || clearTimeout;
            if (typeof this.fetchImpl !== 'function') throw new TypeError('ApiClient 需要 fetch 实现');
        }

        async request(path, options = {}) {
            const method = String(options.method || 'GET').toUpperCase();
            const url = `${this.baseUrl}${path}`;
            const responseType = options.responseType || 'json';
            const timeoutMs = options.timeoutMs === undefined ? this.timeoutMs : options.timeoutMs;
            const requestSignal = createRequestSignal(
                options.signal,
                timeoutMs,
                this.setTimeoutImpl,
                this.clearTimeoutImpl,
            );
            const headers = new Headers(options.headers || {});
            let body = options.body;
            if (Object.prototype.hasOwnProperty.call(options, 'json')) {
                headers.set('Content-Type', 'application/json');
                body = JSON.stringify(options.json);
            }

            let response;
            try {
                response = await this.fetchImpl(url, {
                    method,
                    headers,
                    body,
                    signal: requestSignal.signal,
                });
            } catch (cause) {
                requestSignal.cleanup();
                if (requestSignal.timedOut()) {
                    throw new ApiError('请求超时', {
                        code: 'request_timeout', method, url, cause,
                    });
                }
                if ((options.signal && options.signal.aborted) || (cause && cause.name === 'AbortError')) {
                    throw new ApiError('请求已取消', {
                        code: 'request_aborted', method, url, cause,
                    });
                }
                if (cause && cause.name === 'DesktopTransportError') {
                    throw new ApiError(cause.message || '桌面通信失败', {
                        code: cause.code || 'desktop_transport_error',
                        status: Number.isInteger(cause.status) ? cause.status : 0,
                        method,
                        url,
                        cause,
                    });
                }
                throw new ApiError('无法连接本地服务', {
                    code: 'network_error', method, url, cause,
                });
            }
            const context = { method, url, status: response.status };
            if (!response.ok) {
                let payload;
                try {
                    payload = await readErrorPayload(response);
                } catch (cause) {
                    requestSignal.cleanup();
                    if (requestSignal.timedOut()) {
                        throw new ApiError('请求超时', {
                            ...context, code: 'request_timeout', cause,
                        });
                    }
                    throw new ApiError('读取错误响应失败', {
                        ...context, code: 'response_read_error', cause,
                    });
                }
                requestSignal.cleanup();
                throw new ApiError(
                    payloadMessage(payload, `请求失败：HTTP ${response.status}`),
                    {
                        ...context,
                        code: payloadCode(payload, `http_${response.status}`),
                        payload,
                    },
                );
            }

            if (responseType === 'response') {
                requestSignal.cleanup();
                return response;
            }
            let text;
            try {
                text = await response.text();
            } catch (cause) {
                requestSignal.cleanup();
                if (requestSignal.timedOut()) {
                    throw new ApiError('请求超时', {
                        ...context, code: 'request_timeout', cause,
                    });
                }
                if (options.signal && options.signal.aborted) {
                    throw new ApiError('请求已取消', {
                        ...context, code: 'request_aborted', cause,
                    });
                }
                if (cause && cause.name === 'DesktopTransportError') {
                    throw new ApiError(cause.message || '桌面响应读取失败', {
                        ...context,
                        code: cause.code || 'desktop_transport_error',
                        status: Number.isInteger(cause.status) && cause.status > 0
                            ? cause.status
                            : context.status,
                        cause,
                    });
                }
                throw new ApiError('读取响应失败', {
                    ...context, code: 'response_read_error', cause,
                });
            }
            requestSignal.cleanup();
            if (responseType === 'text') return text;
            if (responseType !== 'json') {
                throw new TypeError(`不支持的 responseType: ${responseType}`);
            }
            const payload = parseJsonText(text, context);
            return validateSchema(payload, options.schema, context);
        }

        get(path, options = {}) {
            return this.request(path, { ...options, method: 'GET' });
        }

        post(path, json, options = {}) {
            return this.request(path, { ...options, method: 'POST', json });
        }

        put(path, json, options = {}) {
            return this.request(path, { ...options, method: 'PUT', json });
        }

        patch(path, json, options = {}) {
            return this.request(path, { ...options, method: 'PATCH', json });
        }

        delete(path, options = {}) {
            const request = { ...options, method: 'DELETE' };
            if (Object.prototype.hasOwnProperty.call(options, 'json')) request.json = options.json;
            return this.request(path, request);
        }
    }

    function parseSseFrame(frame) {
        if (typeof frame !== 'string') throw new TypeError('SSE frame 必须是字符串');
        let event = 'message';
        let id = '';
        let retry = null;
        const data = [];
        for (const rawLine of frame.split(/\r?\n/)) {
            if (!rawLine || rawLine.startsWith(':')) continue;
            const separator = rawLine.indexOf(':');
            const field = separator < 0 ? rawLine : rawLine.slice(0, separator);
            let value = separator < 0 ? '' : rawLine.slice(separator + 1);
            if (value.startsWith(' ')) value = value.slice(1);
            if (field === 'event') event = value || 'message';
            else if (field === 'id' && !value.includes('\0')) id = value;
            else if (field === 'retry' && /^\d+$/.test(value)) retry = Number(value);
            else if (field === 'data') data.push(value);
        }
        return { event, id, retry, data: data.join('\n') };
    }

    async function* readSse(response, options = {}) {
        if (!response || !response.body || typeof response.body.getReader !== 'function') {
            throw new ApiError('服务端未返回可读事件流', { code: 'invalid_sse_response' });
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let aborted = Boolean(options.signal && options.signal.aborted);
        const abortReader = () => {
            aborted = true;
            Promise.resolve(reader.cancel()).catch(() => {});
        };
        if (options.signal && !aborted) {
            options.signal.addEventListener('abort', abortReader, { once: true });
        }
        if (aborted) Promise.resolve(reader.cancel()).catch(() => {});
        try {
            while (true) {
                if (aborted) {
                    throw new ApiError('事件流已取消', { code: 'request_aborted' });
                }
                const { done, value } = await reader.read();
                if (aborted) {
                    throw new ApiError('事件流已取消', { code: 'request_aborted' });
                }
                buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
                let match;
                while ((match = /\r?\n\r?\n/.exec(buffer)) !== null) {
                    const frame = buffer.slice(0, match.index);
                    buffer = buffer.slice(match.index + match[0].length);
                    if (frame.trim()) {
                        const parsed = parseSseFrame(frame);
                        if (parsed.data || parsed.id || parsed.retry !== null || parsed.event !== 'message') {
                            yield parsed;
                        }
                    }
                }
                if (done) break;
            }
            if (buffer.trim()) {
                const parsed = parseSseFrame(buffer);
                if (parsed.data || parsed.id || parsed.retry !== null || parsed.event !== 'message') {
                    yield parsed;
                }
            }
        } finally {
            if (options.signal) options.signal.removeEventListener('abort', abortReader);
            reader.releaseLock();
        }
    }

    return {
        DEFAULT_TIMEOUT_MS,
        ApiClient,
        ApiError,
        payloadCode,
        payloadDetails,
        payloadMessage,
        parseJsonText,
        parseSseFrame,
        readSse,
    };
});
