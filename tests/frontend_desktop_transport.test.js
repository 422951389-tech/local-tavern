'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const {
    createDesktopTransport,
} = require('../web/desktop-transport.js');

function encodeBase64(value) {
    return Buffer.from(value, 'utf8').toString('base64');
}

function createBridgeFixture(signalName = 'event') {
    const listeners = [];
    const requests = [];
    const cancellations = [];
    const bridge = {
        dispatch(payload) {
            requests.push(JSON.parse(payload));
        },
        cancel(requestId) {
            cancellations.push(requestId);
        },
    };
    bridge[signalName] = {
        connect(listener) {
            listeners.push(listener);
        },
    };
    return {
        bridge,
        requests,
        cancellations,
        emit(event) {
            const payload = JSON.stringify(event);
            listeners.forEach(listener => listener(payload));
        },
    };
}

async function flushTasks() {
    await Promise.resolve();
    await new Promise(resolve => setImmediate(resolve));
}

test('桌面传输把普通 fetch 请求编码为统一桥协议并还原 Response', async () => {
    const fixture = createBridgeFixture();
    const transport = createDesktopTransport({
        bridge: fixture.bridge,
        idFactory: () => 'request-ordinary',
    });

    await transport.ready();
    assert.equal(transport.mode, 'desktop');
    assert.equal(transport.isDesktop, true);

    const responsePromise = transport.fetch('/api/echo?kind=desktop', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-Request-Tag': 'acceptance',
        },
        body: JSON.stringify({ text: '桌面请求' }),
    });
    await flushTasks();
    assert.equal(fixture.requests.length, 1);
    assert.deepEqual(fixture.requests[0], {
        id: 'request-ordinary',
        method: 'POST',
        path: '/api/echo?kind=desktop',
        headers: [
            ['content-type', 'application/json'],
            ['x-request-tag', 'acceptance'],
        ],
        body_base64: encodeBase64(JSON.stringify({ text: '桌面请求' })),
    });

    fixture.emit({
        request_id: 'request-ordinary',
        type: 'response_start',
        status: 201,
        headers: [
            ['content-type', 'application/json; charset=utf-8'],
            ['x-transport', 'asgi'],
        ],
    });
    fixture.emit({
        request_id: 'request-ordinary',
        type: 'body',
        body_base64: encodeBase64('{"ok":true,"transport":"desktop"}'),
    });
    fixture.emit({ request_id: 'request-ordinary', type: 'complete' });

    const response = await responsePromise;
    assert.equal(response.status, 201);
    assert.equal(response.headers.get('x-transport'), 'asgi');
    assert.deepEqual(await response.json(), { ok: true, transport: 'desktop' });
});

test('桌面传输逐块交付流式正文并以 complete 收口', async () => {
    const fixture = createBridgeFixture();
    const transport = createDesktopTransport({
        bridge: fixture.bridge,
        idFactory: () => 'request-stream',
    });
    await transport.ready();

    const responsePromise = transport.fetch('/api/chat/turns/turn-a/events?after=0');
    await flushTasks();
    fixture.emit({
        request_id: 'request-stream',
        type: 'response_start',
        status: 200,
        headers: [['content-type', 'text/event-stream']],
    });
    const response = await responsePromise;
    const reader = response.body.getReader();

    fixture.emit({
        request_id: 'request-stream',
        type: 'body',
        body_base64: encodeBase64('id: 1\nevent: content\ndata: {"content":"甲"}\n\n'),
    });
    const first = await reader.read();
    assert.equal(new TextDecoder().decode(first.value), 'id: 1\nevent: content\ndata: {"content":"甲"}\n\n');
    assert.equal(first.done, false);

    fixture.emit({
        request_id: 'request-stream',
        type: 'body',
        body_base64: encodeBase64('id: 2\nevent: terminal\ndata: {"status":"completed"}\n\n'),
    });
    const second = await reader.read();
    assert.equal(new TextDecoder().decode(second.value), 'id: 2\nevent: terminal\ndata: {"status":"completed"}\n\n');
    assert.equal(second.done, false);

    fixture.emit({ request_id: 'request-stream', type: 'complete' });
    assert.deepEqual(await reader.read(), { value: undefined, done: true });
});

test('AbortSignal 只取消对应桌面请求并使读取端得到 AbortError', async () => {
    const fixture = createBridgeFixture();
    const controller = new AbortController();
    const transport = createDesktopTransport({
        bridge: fixture.bridge,
        idFactory: () => 'request-cancel',
    });
    await transport.ready();

    const responsePromise = transport.fetch('/api/chat/turns/turn-a/events', {
        signal: controller.signal,
    });
    await flushTasks();
    fixture.emit({
        request_id: 'request-cancel',
        type: 'response_start',
        status: 200,
        headers: [['content-type', 'text/event-stream']],
    });
    const response = await responsePromise;
    const reader = response.body.getReader();

    controller.abort();
    await flushTasks();
    assert.deepEqual(fixture.cancellations, ['request-cancel']);
    await assert.rejects(
        reader.read(),
        error => error && error.name === 'AbortError',
    );
});

test('桌面 error 终态保留稳定错误码且不会静默改走网络', async () => {
    const fixture = createBridgeFixture();
    let nativeCalls = 0;
    const transport = createDesktopTransport({
        bridge: fixture.bridge,
        idFactory: () => 'request-error',
        nativeFetch: async () => {
            nativeCalls += 1;
            return new Response();
        },
    });
    await transport.ready();

    const responsePromise = transport.fetch('/api/failure');
    await flushTasks();
    fixture.emit({
        request_id: 'request-error',
        type: 'error',
        code: 'desktop_bridge_failure',
        message: '桌面请求失败',
        status: 500,
    });
    await assert.rejects(
        responsePromise,
        error => error
            && error.code === 'desktop_bridge_failure'
            && error.message === '桌面请求失败',
    );
    assert.equal(nativeCalls, 0);
});

test('真实 Qt bridgeEvent 信号可完成请求，桥方法与信号缺失使用不同稳定错误码', async () => {
    const fixture = createBridgeFixture('bridgeEvent');
    const transport = createDesktopTransport({
        bridge: fixture.bridge,
        idFactory: () => 'request-bridge-event',
    });
    await transport.ready();

    const responsePromise = transport.fetch('/api/projects');
    await flushTasks();
    fixture.emit({
        request_id: 'request-bridge-event',
        type: 'response_start',
        status: 204,
        headers: [],
    });
    fixture.emit({ request_id: 'request-bridge-event', type: 'complete' });
    assert.equal((await responsePromise).status, 204);

    const methodsInvalid = createDesktopTransport({
        bridge: { event: { connect() {} } },
    });
    await assert.rejects(
        methodsInvalid.ready(),
        error => error && error.code === 'desktop_bridge_methods_invalid',
    );

    const signalInvalid = createDesktopTransport({
        bridge: { dispatch() {}, cancel() {} },
    });
    await assert.rejects(
        signalInvalid.ready(),
        error => error && error.code === 'desktop_bridge_signal_invalid',
    );
});

test('QWebChannel 构造器的空同步返回不得抢先于真实异步 callback', async () => {
    const fixture = createBridgeFixture('bridgeEvent');
    const root = { qt: { webChannelTransport: {} } };
    const transport = createDesktopTransport({
        root,
        idFactory: () => 'request-async-channel',
        channelFactory(_channelTransport, callback) {
            setImmediate(() => callback({
                objects: { tavernBridge: fixture.bridge },
            }));
            return { objects: {} };
        },
    });

    await transport.ready();
    assert.equal(transport.mode, 'desktop');
    const responsePromise = transport.fetch('/api/projects');
    await flushTasks();
    fixture.emit({
        request_id: 'request-async-channel',
        type: 'response_start',
        status: 200,
        headers: [['content-type', 'application/json']],
    });
    fixture.emit({
        request_id: 'request-async-channel',
        type: 'body',
        body_base64: encodeBase64('{"projects":[]}'),
    });
    fixture.emit({ request_id: 'request-async-channel', type: 'complete' });
    assert.deepEqual(await (await responsePromise).json(), { projects: [] });
});

test('非桌面环境明确使用原生 fetch，桌面模式不需要 HTTP 服务地址', async () => {
    const calls = [];
    const transport = createDesktopTransport({
        root: {},
        nativeFetch: async (input, init) => {
            calls.push({ input, init });
            return new Response('browser');
        },
    });
    await transport.ready();
    assert.equal(transport.mode, 'http');
    assert.equal(transport.isDesktop, false);

    const response = await transport.fetch('/health/live', { method: 'GET' });
    assert.equal(await response.text(), 'browser');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].input, '/health/live');
});

test('桌面桥只额外放行精确 /health/live，不接受其他健康路径或外部 URL', async () => {
    const fixture = createBridgeFixture();
    const transport = createDesktopTransport({
        bridge: fixture.bridge,
        idFactory: () => 'request-health',
    });
    await transport.ready();

    const responsePromise = transport.fetch('/health/live');
    await flushTasks();
    assert.equal(fixture.requests.length, 1);
    assert.equal(fixture.requests[0].path, '/health/live');
    fixture.emit({
        request_id: 'request-health',
        type: 'response_start',
        status: 200,
        headers: [['content-type', 'application/json']],
    });
    fixture.emit({
        request_id: 'request-health',
        type: 'body',
        body_base64: encodeBase64('{"status":"alive"}'),
    });
    fixture.emit({ request_id: 'request-health', type: 'complete' });
    assert.deepEqual(await (await responsePromise).json(), { status: 'alive' });

    for (const rejected of [
        '/health/ready',
        '/health/live?probe=1',
        'https://example.com/api/projects',
        'file:///C:/api/projects',
    ]) {
        await assert.rejects(transport.fetch(rejected), TypeError, rejected);
    }
    assert.equal(fixture.requests.length, 1);
});

test('应用启动先建立桌面通道，并把所有 ApiClient 请求交给统一传输层', () => {
    const root = path.resolve(__dirname, '..');
    const index = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'web', 'app.mjs'), 'utf8');
    const transport = fs.readFileSync(path.join(root, 'web', 'desktop-transport.js'), 'utf8');

    const transportScript = index.indexOf('/static/desktop-transport.js');
    const apiScript = index.indexOf('/static/api-client.js');
    const appScript = index.indexOf('/static/app.mjs');
    assert.ok(transportScript >= 0);
    assert.ok(transportScript < apiScript);
    assert.ok(apiScript < appScript);
    assert.match(app, /fetchImpl:\s*desktopTransport\.fetch/);
    assert.match(app, /await\s+desktopTransport\.ready\(\)/);
    assert.match(app, /desktopTransport\.isDesktop/);
    assert.doesNotMatch(transport, /127\.0\.0\.1|localhost|8765|uvicorn/i);
});
