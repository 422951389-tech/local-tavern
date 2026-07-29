(function attachTavernDesktopTransport(root, factory) {
    const api = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.TavernDesktopTransport = Object.freeze(api);
})(typeof globalThis !== 'undefined' ? globalThis : this, function createModule(defaultRoot) {
    'use strict';

    const BRIDGE_OBJECT_NAME = 'tavernBridge';
    const QWEBCHANNEL_SCRIPT = 'qrc:///qtwebchannel/qwebchannel.js';
    const BODYLESS_STATUSES = new Set([204, 205, 304]);
    let fallbackRequestSequence = 0;

    class DesktopTransportError extends Error {
        constructor(message, options = {}) {
            super(message || '桌面通信失败');
            this.name = 'DesktopTransportError';
            this.code = options.code || 'desktop_transport_error';
            this.status = Number.isInteger(options.status) ? options.status : 0;
            this.requestId = options.requestId || '';
            this.cause = options.cause;
        }
    }

    function abortError(message = '请求已取消') {
        const error = new Error(message);
        error.name = 'AbortError';
        return error;
    }

    function hasOwn(value, key) {
        return Object.prototype.hasOwnProperty.call(value, key);
    }

    function bytesToBase64(bytes, root) {
        if (!bytes || bytes.byteLength === 0) return '';
        const btoaImpl = root && typeof root.btoa === 'function'
            ? root.btoa.bind(root)
            : (typeof btoa === 'function' ? btoa : null);
        if (btoaImpl) {
            let binary = '';
            const chunkSize = 0x8000;
            for (let offset = 0; offset < bytes.byteLength; offset += chunkSize) {
                const chunk = bytes.subarray(offset, Math.min(offset + chunkSize, bytes.byteLength));
                binary += String.fromCharCode(...chunk);
            }
            return btoaImpl(binary);
        }
        if (typeof Buffer !== 'undefined') return Buffer.from(bytes).toString('base64');
        throw new DesktopTransportError('当前环境不支持 Base64 编码', {
            code: 'desktop_base64_unavailable',
        });
    }

    function base64ToBytes(value, root) {
        if (typeof value !== 'string') {
            throw new DesktopTransportError('桌面桥返回了无效响应块', {
                code: 'invalid_desktop_event',
            });
        }
        if (value === '') return new Uint8Array();
        const atobImpl = root && typeof root.atob === 'function'
            ? root.atob.bind(root)
            : (typeof atob === 'function' ? atob : null);
        try {
            if (atobImpl) {
                const binary = atobImpl(value);
                const bytes = new Uint8Array(binary.length);
                for (let index = 0; index < binary.length; index += 1) {
                    bytes[index] = binary.charCodeAt(index);
                }
                return bytes;
            }
            if (typeof Buffer !== 'undefined') return new Uint8Array(Buffer.from(value, 'base64'));
        } catch (cause) {
            throw new DesktopTransportError('桌面桥返回了无效 Base64 数据', {
                code: 'invalid_desktop_event', cause,
            });
        }
        throw new DesktopTransportError('当前环境不支持 Base64 解码', {
            code: 'desktop_base64_unavailable',
        });
    }

    function nextRequestId(root) {
        if (root && root.crypto && typeof root.crypto.randomUUID === 'function') {
            return root.crypto.randomUUID();
        }
        fallbackRequestSequence += 1;
        return `desktop-${Date.now().toString(36)}-${fallbackRequestSequence.toString(36)}`;
    }

    function normalizePath(input, root) {
        const RequestCtor = (root && root.Request) || (typeof Request === 'function' ? Request : null);
        let raw = RequestCtor && input instanceof RequestCtor ? input.url : String(input || '');
        if (/^[a-z][a-z\d+.-]*:/i.test(raw)) {
            let parsed;
            try {
                parsed = new URL(raw);
            } catch (cause) {
                throw new TypeError(`无效请求地址：${raw}`, { cause });
            }
            const trustedDesktopOrigin = parsed.protocol === 'tavern:'
                && parsed.hostname === 'app'
                && parsed.port === ''
                && parsed.username === ''
                && parsed.password === '';
            if (!trustedDesktopOrigin || parsed.hash) {
                throw new TypeError('桌面桥拒绝外部或带片段的绝对 URL');
            }
            raw = `${parsed.pathname}${parsed.search}`;
        }
        const allowedApiPath = /^\/api(?:\/|\?|$)/.test(raw);
        const allowedLivenessPath = raw === '/health/live';
        if ((!allowedApiPath && !allowedLivenessPath) || raw.includes('#')) {
            throw new TypeError('桌面桥仅允许 /api 相对路径或精确 /health/live');
        }
        return raw;
    }

    function headersToPairs(input, root) {
        const HeadersCtor = (root && root.Headers) || (typeof Headers === 'function' ? Headers : null);
        if (!HeadersCtor) {
            throw new DesktopTransportError('当前环境不支持 Headers', {
                code: 'desktop_web_api_unavailable',
            });
        }
        return Array.from(new HeadersCtor(input || {}).entries());
    }

    function validateHeaderPairs(value) {
        if (!Array.isArray(value) || value.some(pair => (
            !Array.isArray(pair) || pair.length !== 2
            || typeof pair[0] !== 'string' || typeof pair[1] !== 'string'
        ))) {
            throw new DesktopTransportError('桌面桥返回了无效响应头', {
                code: 'invalid_desktop_event',
            });
        }
        return value;
    }

    async function bodyToBytes(body, root) {
        if (body === undefined || body === null) return new Uint8Array();
        if (typeof body === 'string') {
            const Encoder = (root && root.TextEncoder) || (typeof TextEncoder === 'function' ? TextEncoder : null);
            if (!Encoder) {
                throw new DesktopTransportError('当前环境不支持 UTF-8 编码', {
                    code: 'desktop_web_api_unavailable',
                });
            }
            return new Encoder().encode(body);
        }
        if (body instanceof ArrayBuffer) return new Uint8Array(body);
        if (ArrayBuffer.isView(body)) {
            return new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
        }
        if (body && typeof body.arrayBuffer === 'function') {
            return new Uint8Array(await body.arrayBuffer());
        }
        throw new TypeError('桌面桥不支持该请求体类型');
    }

    function loadQWebChannel(root, scriptUrl = QWEBCHANNEL_SCRIPT) {
        if (typeof root.QWebChannel === 'function') return Promise.resolve(root.QWebChannel);
        const document = root.document;
        if (!document || typeof document.createElement !== 'function') {
            return Promise.reject(new DesktopTransportError('无法加载 QWebChannel 客户端', {
                code: 'qwebchannel_unavailable',
            }));
        }
        const existing = document.querySelector('script[data-tavern-qwebchannel]');
        if (existing && existing._tavernReadyPromise) return existing._tavernReadyPromise;

        const script = existing || document.createElement('script');
        const promise = new Promise((resolve, reject) => {
            const onLoad = () => {
                if (typeof root.QWebChannel === 'function') resolve(root.QWebChannel);
                else reject(new DesktopTransportError('QWebChannel 客户端未注册', {
                    code: 'qwebchannel_unavailable',
                }));
            };
            const onError = () => reject(new DesktopTransportError('QWebChannel 客户端加载失败', {
                code: 'qwebchannel_load_failed',
            }));
            script.addEventListener('load', onLoad, { once: true });
            script.addEventListener('error', onError, { once: true });
        });
        script._tavernReadyPromise = promise;
        if (!existing) {
            script.dataset.tavernQwebchannel = 'true';
            script.src = scriptUrl;
            (document.head || document.documentElement).appendChild(script);
        }
        return promise;
    }

    function createDesktopTransport(options = {}) {
        const root = options.root || defaultRoot || {};
        const nativeFetch = options.nativeFetch
            || (typeof root.fetch === 'function' ? root.fetch.bind(root) : null);
        const explicitBridge = options.bridge || null;
        const idFactory = options.idFactory || (() => nextRequestId(root));
        const pending = new Map();
        let bridge = null;
        let readyPromise = null;
        let eventConnected = false;
        let mode = explicitBridge || (root.qt && root.qt.webChannelTransport) ? 'desktop' : 'http';

        function cleanupRequest(request) {
            if (!request) return;
            if (request.signal && request.abortListener) {
                request.signal.removeEventListener('abort', request.abortListener);
            }
            pending.delete(request.id);
        }

        function rejectRequest(request, error) {
            if (!request || request.closed) return;
            request.closed = true;
            if (request.responseStarted && request.controller) request.controller.error(error);
            else request.reject(error);
            cleanupRequest(request);
        }

        function cancelRequest(request, reason) {
            if (!request || request.closed) return;
            try {
                bridge.cancel(request.id);
            } catch (_error) {}
            rejectRequest(request, reason && reason.name === 'AbortError' ? reason : abortError());
        }

        function releaseCancelledStream(request) {
            if (!request || request.closed) return;
            try {
                bridge.cancel(request.id);
            } catch (_error) {}
            request.closed = true;
            cleanupRequest(request);
        }

        function failAll(error) {
            for (const request of [...pending.values()]) rejectRequest(request, error);
        }

        function parseBridgeEvent(raw) {
            try {
                const event = typeof raw === 'string' ? JSON.parse(raw) : raw;
                if (!event || typeof event !== 'object' || Array.isArray(event)) throw new Error('event is not object');
                if (typeof event.request_id !== 'string' || !event.request_id) throw new Error('request_id missing');
                if (typeof event.type !== 'string' || !event.type) throw new Error('type missing');
                return event;
            } catch (cause) {
                throw new DesktopTransportError('桌面桥返回了无效事件', {
                    code: 'invalid_desktop_event', cause,
                });
            }
        }

        function handleBridgeEvent(raw) {
            let event;
            try {
                event = parseBridgeEvent(raw);
            } catch (error) {
                failAll(error);
                return;
            }
            const request = pending.get(event.request_id);
            if (!request || request.closed) return;
            try {
                if (event.type === 'response_start') {
                    if (request.responseStarted) throw new Error('duplicate response_start');
                    if (!Number.isInteger(event.status) || event.status < 200 || event.status > 599) {
                        throw new Error('invalid response status');
                    }
                    const headerPairs = validateHeaderPairs(event.headers);
                    const HeadersCtor = (root && root.Headers) || Headers;
                    const ResponseCtor = options.Response || (root && root.Response) || Response;
                    const StreamCtor = options.ReadableStream || (root && root.ReadableStream) || ReadableStream;
                    request.responseStarted = true;
                    const body = BODYLESS_STATUSES.has(event.status)
                        ? null
                        : new StreamCtor({
                            start(controller) { request.controller = controller; },
                            cancel() { releaseCancelledStream(request); },
                        });
                    const response = new ResponseCtor(body, {
                        status: event.status,
                        headers: new HeadersCtor(headerPairs),
                    });
                    request.resolve(response);
                } else if (event.type === 'body') {
                    if (!request.responseStarted) throw new Error('body before response_start');
                    const chunk = base64ToBytes(event.body_base64, root);
                    if (request.controller) request.controller.enqueue(chunk);
                    else if (chunk.byteLength > 0) throw new Error('body not allowed for this status');
                } else if (event.type === 'complete') {
                    if (!request.responseStarted) throw new Error('complete before response_start');
                    request.closed = true;
                    if (request.controller) request.controller.close();
                    cleanupRequest(request);
                } else if (event.type === 'error') {
                    throw new DesktopTransportError(
                        typeof event.message === 'string' && event.message ? event.message : '桌面请求失败',
                        {
                            code: typeof event.code === 'string' && event.code
                                ? event.code
                                : 'desktop_request_failed',
                            status: Number.isInteger(event.status) ? event.status : 0,
                            requestId: request.id,
                        },
                    );
                } else {
                    throw new Error(`unknown event type: ${event.type}`);
                }
            } catch (cause) {
                const error = cause instanceof DesktopTransportError
                    ? cause
                    : new DesktopTransportError('桌面桥事件顺序或结构无效', {
                        code: 'invalid_desktop_event', requestId: request.id, cause,
                    });
                rejectRequest(request, error);
            }
        }

        function connectBridge(candidate) {
            if (!candidate || typeof candidate.dispatch !== 'function' || typeof candidate.cancel !== 'function') {
                throw new DesktopTransportError('桌面桥接口不完整', {
                    code: 'desktop_bridge_methods_invalid',
                });
            }
            const eventSignal = candidate.event && typeof candidate.event.connect === 'function'
                ? candidate.event
                : candidate.bridgeEvent;
            if (!eventSignal || typeof eventSignal.connect !== 'function') {
                throw new DesktopTransportError('桌面桥未提供 event 或 bridgeEvent 信号', {
                    code: 'desktop_bridge_signal_invalid',
                });
            }
            bridge = candidate;
            if (!eventConnected) {
                eventSignal.connect(handleBridgeEvent);
                eventConnected = true;
            }
            mode = 'desktop';
            return transport;
        }

        function connectChannel(factory) {
            return new Promise((resolve, reject) => {
                let settled = false;
                const onChannel = channel => {
                    if (settled) return;
                    settled = true;
                    try {
                        resolve(connectBridge(channel && channel.objects && channel.objects[BRIDGE_OBJECT_NAME]));
                    } catch (error) {
                        reject(error);
                    }
                };
                try {
                    const result = factory(root.qt.webChannelTransport, onChannel);
                    if (result && typeof result.then === 'function') {
                        result.then(value => {
                            if (!settled) onChannel(value);
                        }, error => {
                            if (!settled) reject(error);
                        });
                    }
                } catch (error) {
                    reject(new DesktopTransportError('QWebChannel 初始化失败', {
                        code: 'qwebchannel_init_failed', cause: error,
                    }));
                }
            });
        }

        function ready() {
            if (readyPromise) return readyPromise;
            if (explicitBridge) {
                readyPromise = Promise.resolve().then(() => connectBridge(explicitBridge));
                return readyPromise;
            }
            if (!(root.qt && root.qt.webChannelTransport)) {
                mode = 'http';
                readyPromise = Promise.resolve(transport);
                return readyPromise;
            }
            mode = 'desktop';
            if (typeof options.channelFactory === 'function') {
                readyPromise = connectChannel(options.channelFactory);
                return readyPromise;
            }
            readyPromise = loadQWebChannel(root, options.qWebChannelScript)
                .then(QWebChannel => connectChannel((channelTransport, callback) => (
                    new QWebChannel(channelTransport, callback)
                )));
            return readyPromise;
        }

        async function desktopFetch(input, init = {}) {
            await ready();
            if (mode === 'http') {
                if (typeof nativeFetch !== 'function') {
                    throw new DesktopTransportError('当前环境不支持 HTTP fetch', {
                        code: 'http_fetch_unavailable',
                    });
                }
                return nativeFetch(input, init);
            }

            const RequestCtor = (root && root.Request) || (typeof Request === 'function' ? Request : null);
            const requestInput = RequestCtor && input instanceof RequestCtor ? input : null;
            const method = String(init.method || (requestInput && requestInput.method) || 'GET').toUpperCase();
            const path = normalizePath(input, root);
            const baseHeaders = requestInput ? requestInput.headers : undefined;
            const headerPairs = headersToPairs(init.headers === undefined ? baseHeaders : init.headers, root);
            let sourceBody = hasOwn(init, 'body') ? init.body : undefined;
            if (sourceBody === undefined && requestInput && !['GET', 'HEAD'].includes(method)) {
                sourceBody = requestInput.clone();
            }
            const bodyBytes = await bodyToBytes(sourceBody, root);
            if (bodyBytes.byteLength > 0 && ['GET', 'HEAD'].includes(method)) {
                throw new TypeError(`${method} 请求不能携带请求体`);
            }
            const signal = init.signal || (requestInput && requestInput.signal) || null;
            if (signal && signal.aborted) throw abortError();

            const id = String(idFactory());
            if (!id || pending.has(id)) {
                throw new DesktopTransportError('桌面请求 ID 无效或重复', {
                    code: 'desktop_request_id_invalid', requestId: id,
                });
            }
            return new Promise((resolve, reject) => {
                const request = {
                    id, resolve, reject, signal,
                    abortListener: null,
                    responseStarted: false,
                    controller: null,
                    closed: false,
                };
                request.abortListener = () => cancelRequest(request, abortError());
                if (signal) signal.addEventListener('abort', request.abortListener, { once: true });
                pending.set(id, request);
                const payload = {
                    id,
                    method,
                    path,
                    headers: headerPairs,
                    body_base64: bytesToBase64(bodyBytes, root),
                };
                try {
                    const dispatched = bridge.dispatch(JSON.stringify(payload));
                    if (dispatched && typeof dispatched.catch === 'function') {
                        dispatched.catch(cause => rejectRequest(request, new DesktopTransportError(
                            '桌面请求发送失败',
                            { code: 'desktop_dispatch_failed', requestId: id, cause },
                        )));
                    }
                } catch (cause) {
                    rejectRequest(request, new DesktopTransportError('桌面请求发送失败', {
                        code: 'desktop_dispatch_failed', requestId: id, cause,
                    }));
                }
            });
        }

        const transport = {
            ready,
            fetch: desktopFetch,
            get mode() { return mode; },
            get isDesktop() { return mode === 'desktop'; },
        };
        return transport;
    }

    const defaultTransport = createDesktopTransport();
    return {
        BRIDGE_OBJECT_NAME,
        QWEBCHANNEL_SCRIPT,
        DesktopTransportError,
        createDesktopTransport,
        ready: (...args) => defaultTransport.ready(...args),
        fetch: (...args) => defaultTransport.fetch(...args),
        get mode() { return defaultTransport.mode; },
        get isDesktop() { return defaultTransport.isDesktop; },
    };
});
