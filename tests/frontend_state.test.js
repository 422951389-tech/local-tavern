'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const {
    ApiClient,
    ApiError,
    parseSseFrame,
    payloadDetails,
    readSse,
} = require('../web/api-client.js');
const {
    SessionRefTracker,
    buildSessionCommit,
    createSessionRef,
    sameSessionRef,
    sessionBelongsToRef,
    shouldCreateDefaultSave,
} = require('../web/session-ref.js');

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

function sessionFixture(project, save, revision, marker) {
    return {
        project,
        session_id: save,
        name: `${marker}-name`,
        revision,
        current_model: `${marker}-model`,
        message_history: [{ id: `${marker}-message`, role: 'user', content: marker }],
        scene_meta: { location: `${marker}-scene` },
        characters_state: { [`${marker}-character`]: { name: marker } },
        updated_at: `${marker}-time`,
    };
}

function jsonResponse(body, status = 200, headers = {}) {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json', ...headers },
    });
}

test('ApiClient parses valid JSON and enforces response schema', async () => {
    const client = new ApiClient({
        fetchImpl: async () => jsonResponse({ models: ['local-model'] }),
    });
    const payload = await client.get('/api/models', {
        schema: body => Array.isArray(body && body.models) || 'models 必须是数组',
    });
    assert.deepEqual(payload.models, ['local-model']);

    await assert.rejects(
        () => client.get('/api/models', { schema: () => '响应字段缺失' }),
        error => error instanceof ApiError
            && error.code === 'invalid_response_schema'
            && error.message === '响应字段缺失',
    );
});

test('ApiClient normalizes JSON and text HTTP errors', async () => {
    const jsonClient = new ApiClient({
        fetchImpl: async () => jsonResponse({ detail: { code: 'revision_conflict', message: '版本冲突' } }, 409),
    });
    await assert.rejects(
        () => jsonClient.post('/api/session', {}),
        error => error instanceof ApiError
            && error.status === 409
            && error.code === 'revision_conflict'
            && error.message === '版本冲突',
    );

    const textClient = new ApiClient({
        fetchImpl: async () => new Response('upstream unavailable', { status: 503 }),
    });
    await assert.rejects(
        () => textClient.get('/api/models'),
        error => error instanceof ApiError
            && error.status === 503
            && error.code === 'http_503'
            && error.message === 'upstream unavailable',
    );

    const cases = [
        [{ detail: 'bad request' }, 400, 'http_400', 'bad request'],
        [{ detail: [{ msg: '字段无效' }, { msg: '缺少参数' }] }, 422, 'http_422', '字段无效；缺少参数'],
        [{ error: { code: 'internal_failure', message: '服务内部错误' } }, 500, 'internal_failure', '服务内部错误'],
    ];
    for (const [body, status, code, message] of cases) {
        const client = new ApiClient({ fetchImpl: async () => jsonResponse(body, status) });
        await assert.rejects(
            () => client.get('/api/test'),
            error => error instanceof ApiError
                && error.status === status
                && error.code === code
                && error.message === message,
        );
    }
});

test('ApiClient 优先采用规范 error 信封并兼容旧 detail 字段', async () => {
    const canonical = {
        error: {
            code: 'turn_conflict',
            message: '规范错误',
            details: { turn_id: 'canonical-turn', retryable: false },
        },
        detail: {
            code: 'legacy_conflict',
            message: '旧错误',
            turn_id: 'legacy-turn',
        },
    };
    const canonicalClient = new ApiClient({
        fetchImpl: async () => jsonResponse(canonical, 409),
    });
    await assert.rejects(
        () => canonicalClient.post('/api/test', {}),
        error => error instanceof ApiError
            && error.code === 'turn_conflict'
            && error.message === '规范错误'
            && error.details.turn_id === 'canonical-turn'
            && error.details.retryable === false,
    );
    assert.deepEqual(payloadDetails(canonical), canonical.error.details);

    const legacy = { detail: { code: 'legacy_only', message: '旧格式', turn_id: 'legacy-turn' } };
    const legacyClient = new ApiClient({ fetchImpl: async () => jsonResponse(legacy, 409) });
    await assert.rejects(
        () => legacyClient.get('/api/test'),
        error => error instanceof ApiError
            && error.code === 'legacy_only'
            && error.message === '旧格式'
            && error.details.turn_id === 'legacy-turn',
    );
});

test('ApiClient distinguishes invalid JSON, network failure, abort and timeout', async () => {
    const invalidClient = new ApiClient({
        fetchImpl: async () => new Response('<html>bad gateway</html>'),
    });
    await assert.rejects(
        () => invalidClient.get('/api/session'),
        error => error instanceof ApiError && error.code === 'invalid_json_response',
    );

    const networkClient = new ApiClient({
        fetchImpl: async () => { throw new TypeError('connection refused'); },
    });
    await assert.rejects(
        () => networkClient.get('/api/session'),
        error => error instanceof ApiError && error.code === 'network_error',
    );

    const abortController = new AbortController();
    abortController.abort();
    const pendingFetch = (_url, options) => new Promise((_resolve, reject) => {
        if (options.signal.aborted) {
            const abort = new Error('aborted');
            abort.name = 'AbortError';
            reject(abort);
        }
    });
    const abortClient = new ApiClient({ fetchImpl: pendingFetch });
    await assert.rejects(
        () => abortClient.get('/api/session', { signal: abortController.signal }),
        error => error instanceof ApiError && error.code === 'request_aborted',
    );

    const scheduleImmediately = callback => {
        queueMicrotask(callback);
        return 1;
    };
    const timeoutClient = new ApiClient({
        timeoutMs: 100,
        setTimeoutImpl: scheduleImmediately,
        clearTimeoutImpl: () => {},
        fetchImpl: (_url, options) => new Promise((_resolve, reject) => {
            if (options.signal.aborted) {
                const abort = new Error('aborted');
                abort.name = 'AbortError';
                reject(abort);
                return;
            }
            options.signal.addEventListener('abort', () => {
                const abort = new Error('aborted');
                abort.name = 'AbortError';
                reject(abort);
            }, { once: true });
        }),
    });
    await assert.rejects(
        () => timeoutClient.get('/api/session'),
        error => error instanceof ApiError && error.code === 'request_timeout',
    );

    const bodyTimeoutClient = new ApiClient({
        timeoutMs: 100,
        setTimeoutImpl: scheduleImmediately,
        clearTimeoutImpl: () => {},
        fetchImpl: async (_url, options) => ({
            ok: true,
            status: 200,
            text: () => new Promise((_resolve, reject) => {
                const rejectAbort = () => {
                    const abort = new Error('aborted');
                    abort.name = 'AbortError';
                    reject(abort);
                };
                if (options.signal.aborted) rejectAbort();
                else options.signal.addEventListener('abort', rejectAbort, { once: true });
            }),
        }),
    });
    await assert.rejects(
        () => bodyTimeoutClient.get('/api/session'),
        error => error instanceof ApiError && error.code === 'request_timeout',
    );
});

test('SSE parser preserves ids, event names and fragmented multiline data', async () => {
    assert.deepEqual(parseSseFrame('id: 7\nevent: content\ndata: {"value":1}'), {
        event: 'content', id: '7', retry: null, data: '{"value":1}',
    });

    const encoder = new TextEncoder();
    const chunks = [
        'id: 1\nevent: content\nda',
        'ta: {"content":"A"}\n\nid: 2\nevent: terminal\n',
        'data: {"status":"completed"}\n\n',
    ];
    const stream = new ReadableStream({
        start(controller) {
            chunks.forEach(chunk => controller.enqueue(encoder.encode(chunk)));
            controller.close();
        },
    });
    const events = [];
    for await (const event of readSse(new Response(stream))) events.push(event);
    assert.deepEqual(events.map(event => [event.id, event.event]), [
        ['1', 'content'],
        ['2', 'terminal'],
    ]);
    assert.equal(JSON.parse(events[0].data).content, 'A');
    assert.equal(JSON.parse(events[1].data).status, 'completed');
});

test('SSE keepalive comments are ignored and abort releases a blocked reader', async () => {
    const encoder = new TextEncoder();
    const comments = new ReadableStream({
        start(controller) {
            controller.enqueue(encoder.encode(': keep-alive\n\n'));
            controller.enqueue(encoder.encode('id: 1\nevent: started\ndata: {"type":"started"}\n\n'));
            controller.close();
        },
    });
    const events = [];
    for await (const event of readSse(new Response(comments))) events.push(event);
    assert.deepEqual(events.map(event => event.event), ['started']);

    const blocked = new ReadableStream({ start() {} });
    const controller = new AbortController();
    const iterator = readSse(new Response(blocked), { signal: controller.signal })[Symbol.asyncIterator]();
    const pending = iterator.next();
    controller.abort();
    await assert.rejects(
        pending,
        error => error instanceof ApiError && error.code === 'request_aborted',
    );

    const preAborted = new AbortController();
    preAborted.abort();
    const preAbortedIterator = readSse(new Response(new ReadableStream({ start() {} })), {
        signal: preAborted.signal,
    })[Symbol.asyncIterator]();
    await assert.rejects(
        preAbortedIterator.next(),
        error => error instanceof ApiError && error.code === 'request_aborted',
    );
});

test('SessionRef is immutable and stale epochs cannot commit after navigation', () => {
    const tracker = new SessionRefTracker('project-a', 'save-a');
    const delayedA = tracker.capture();
    assert.equal(Object.isFrozen(delayedA), true);
    assert.throws(() => { delayedA.save = 'tampered'; }, TypeError);

    const currentB = tracker.advance('project-b', 'save-b');
    assert.equal(tracker.isCurrent(delayedA), false);
    assert.equal(tracker.isCurrent(currentB), true);
    assert.equal(sameSessionRef(delayedA, currentB), false);
    assert.equal(sessionBelongsToRef({ session_id: 'save-b', project: 'project-b' }, currentB), true);
    assert.equal(sessionBelongsToRef({ session_id: 'save-a', project: 'project-a' }, currentB), false);

    const renewedB = tracker.invalidate();
    assert.equal(tracker.isCurrent(currentB), false);
    assert.equal(renewedB.epoch, currentB.epoch + 1);
    assert.deepEqual(createSessionRef('project-b', 'save-b', renewedB.epoch), renewedB);
    assert.throws(() => createSessionRef('project-b', '', 1), TypeError);
});

test('delayed A response cannot overwrite the complete B session view', async () => {
    const tracker = new SessionRefTracker('project-a', 'save-a');
    const refA = tracker.capture();
    const delayedA = deferred();
    const taskA = delayedA.promise.then(session => buildSessionCommit({
        session,
        ref: refA,
        currentRef: tracker.capture(),
        saveList: [],
    }));

    const refB = tracker.advance('project-b', 'save-b');
    const delayedB = deferred();
    const taskB = delayedB.promise.then(session => buildSessionCommit({
        session,
        ref: refB,
        currentRef: tracker.capture(),
        saveList: [],
    }));

    delayedB.resolve(sessionFixture('project-b', 'save-b', 7, 'B'));
    const commitB = await taskB;
    assert.equal(commitB.accepted, true);
    delayedA.resolve(sessionFixture('project-a', 'save-a', 9, 'A'));
    const commitA = await taskA;
    assert.deepEqual(commitA, { accepted: false, reason: 'stale_ref' });

    assert.equal(commitB.project, 'project-b');
    assert.equal(commitB.save, 'save-b');
    assert.equal(commitB.currentModel, 'B-model');
    assert.equal(commitB.messageHistory[0].content, 'B');
    assert.equal(commitB.sceneMeta.location, 'B-scene');
    assert.equal(commitB.charactersState['B-character'].name, 'B');
    assert.deepEqual(commitB.saveList.map(item => item.session_id), ['save-b']);
});

test('same-ref response with a lower revision is rejected without changing the view', () => {
    const tracker = new SessionRefTracker('project-a', 'save-a');
    const ref = tracker.capture();
    const current = sessionFixture('project-a', 'save-a', 12, 'new');
    const stale = sessionFixture('project-a', 'save-a', 11, 'old');
    const result = buildSessionCommit({
        session: stale,
        ref,
        currentRef: tracker.capture(),
        currentSession: current,
        saveList: [{ session_id: 'save-a', name: 'new-name', message_count: 1 }],
    });
    assert.deepEqual(result, { accepted: false, reason: 'stale_revision' });
    assert.equal(current.current_model, 'new-model');
    assert.equal(current.scene_meta.location, 'new-scene');
});

test('default save creation is allowed only for a current validated empty list', () => {
    const tracker = new SessionRefTracker('project-a', null);
    const candidate = tracker.capture();
    assert.equal(shouldCreateDefaultSave([], candidate, tracker.capture()), true);
    assert.equal(shouldCreateDefaultSave([{ session_id: 'save-a' }], candidate, tracker.capture()), false);
    assert.equal(shouldCreateDefaultSave(null, candidate, tracker.capture()), false);
    const newer = tracker.advance('project-b', null);
    assert.equal(shouldCreateDefaultSave([], candidate, newer), false);
});
