'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

async function loadSearch() {
    return import('../web/search.mjs');
}

function messageResult(overrides = {}) {
    return {
        kind: 'message',
        save_id: '存档A',
        revision: 7,
        time: '2026-07-18T12:00:00',
        field: 'content',
        snippet: { prefix: '前文', match: '命中', suffix: '后文' },
        message_id: '11111111-1111-4111-8111-111111111111',
        role: 'user',
        pinned: false,
        ...overrides,
    };
}

function summaryResult(overrides = {}) {
    return {
        kind: 'summary',
        save_id: '存档A',
        revision: 7,
        time: '2026-07-18T12:30:00',
        field: 'text',
        snippet: { prefix: '', match: '摘要命中', suffix: '' },
        summary_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        ...overrides,
    };
}

function searchResponse(overrides = {}) {
    const results = overrides.results || [messageResult(), summaryResult()];
    return {
        project: '项目A',
        scope: 'all',
        results,
        total_matches: results.length,
        truncated: false,
        scanned: { sessions: 1, sources: 2, bytes: 1024 },
        skipped: [],
        ...overrides,
    };
}

function clone(value) {
    return JSON.parse(JSON.stringify(value));
}

class FakeClassList {
    constructor(element) { this.element = element; }
    contains(name) { return this.element.className.split(/\s+/).filter(Boolean).includes(name); }
    add(...names) {
        const values = new Set(this.element.className.split(/\s+/).filter(Boolean));
        names.forEach(name => values.add(name));
        this.element.className = [...values].join(' ');
    }
}

class FakeTextNode {
    constructor(text) {
        this.nodeType = 3;
        this.textContent = String(text);
        this.parentNode = null;
    }
}

class FakeElement {
    constructor(tagName) {
        this.nodeType = 1;
        this.tagName = String(tagName).toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.className = '';
        this.classList = new FakeClassList(this);
        this.attributes = new Map();
        this.listeners = new Map();
        this.textContent = '';
        this.type = '';
        this.id = '';
        this.isConnected = true;
        this.focusCount = 0;
        this.scrollCalls = [];
    }

    set innerHTML(_value) { throw new Error('搜索渲染禁止写 innerHTML'); }
    get innerHTML() { return ''; }

    appendChild(child) {
        if (child.parentNode) {
            child.parentNode.children = child.parentNode.children.filter(item => item !== child);
        }
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    replaceChildren(...children) {
        this.children.forEach(child => { child.parentNode = null; });
        this.children = [];
        children.forEach(child => this.appendChild(child));
    }

    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
    hasAttribute(name) { return this.attributes.has(name); }
    addEventListener(type, listener) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(listener);
    }

    dispatch(type, init = {}) {
        const event = { type, target: this, currentTarget: this, ...init };
        for (const listener of this.listeners.get(type) || []) listener(event);
        return event;
    }

    querySelector(selector) {
        const predicate = selector.startsWith('#')
            ? child => child.nodeType === 1 && child.id === selector.slice(1)
            : child => child.nodeType === 1 && child.tagName === selector.toUpperCase();
        const visit = element => {
            for (const child of element.children) {
                if (predicate(child)) return child;
                if (child.nodeType === 1) {
                    const nested = visit(child);
                    if (nested) return nested;
                }
            }
            return null;
        };
        return visit(this);
    }

    get nextElementSibling() {
        if (!this.parentNode) return null;
        const siblings = this.parentNode.children.filter(child => child.nodeType === 1);
        const index = siblings.indexOf(this);
        return index >= 0 ? siblings[index + 1] || null : null;
    }

    scrollIntoView(options) { this.scrollCalls.push(options); }
    focus(options) { this.focusCount += 1; this.focusOptions = options; }
}

class FakeDocument {
    constructor() { this.createdText = []; }
    createElement(tagName) { return new FakeElement(tagName); }
    createTextNode(text) {
        const node = new FakeTextNode(text);
        this.createdText.push(node);
        return node;
    }
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((accept, decline) => {
        resolve = accept;
        reject = decline;
    });
    return { promise, resolve, reject };
}

test('搜索响应采用严格白名单 Schema，并拒绝重复、越界和敏感扩展字段', async () => {
    const { validateSearchResponse } = await loadSearch();
    const valid = searchResponse();
    assert.equal(validateSearchResponse(valid, { project: '项目A', scope: 'all' }), true);
    const uuidV7 = searchResponse({
        results: [
            messageResult({ message_id: '01890f7e-7b7d-7cc7-98c4-dc0c0c0c0c0c' }),
            summaryResult({ summary_id: '01890f7e-7b7d-7cc7-98c4-dc0c0c0c0c0d' }),
        ],
    });
    assert.equal(validateSearchResponse(uuidV7), true);

    const invalid = [];
    const extraTop = clone(valid); extraTop.query = '不得回显'; invalid.push(extraTop);
    const missingTop = clone(valid); delete missingTop.scanned; invalid.push(missingTop);
    const extraResult = clone(valid); extraResult.results[0].thinking = '私密思考'; invalid.push(extraResult);
    const bodyResult = clone(valid); bodyResult.results[0].content = '完整正文'; invalid.push(bodyResult);
    const badRole = clone(valid); badRole.results[0].role = 'system'; invalid.push(badRole);
    const badRevision = clone(valid); badRevision.results[0].revision = '7'; invalid.push(badRevision);
    const badUuid = clone(valid); badUuid.results[0].message_id = 'not-a-uuid'; invalid.push(badUuid);
    const longSnippet = clone(valid); longSnippet.results[0].snippet.prefix = '长'.repeat(240); invalid.push(longSnippet);
    const duplicate = clone(valid); duplicate.results.push(clone(duplicate.results[0]));
    duplicate.total_matches += 1; invalid.push(duplicate);
    const badTruncation = clone(valid); badTruncation.truncated = true; invalid.push(badTruncation);
    const excessiveScan = clone(valid); excessiveScan.scanned.sources = 50001; invalid.push(excessiveScan);
    const unsafeSkip = clone(valid); unsafeSkip.skipped = [{ save_id: '坏档', code: 'bad', path: 'C:/secret' }];
    invalid.push(unsafeSkip);

    for (const [index, payload] of invalid.entries()) {
        assert.equal(typeof validateSearchResponse(payload), 'string', `case ${index}`);
    }
    assert.equal(typeof validateSearchResponse(valid, { project: '其他项目' }), 'string');
    assert.equal(typeof validateSearchResponse(valid, { scope: 'messages' }), 'string');
});

test('搜索服务规范化参数、透传 AbortSignal，并在客户端未执行 Schema 时再次校验', async () => {
    const { createSearchService } = await loadSearch();
    const calls = [];
    const signal = new AbortController().signal;
    const service = createSearchService({
        async get(path, options) {
            calls.push({ path, options });
            const body = searchResponse({ project: '项目 A', scope: 'pinned', results: [], total_matches: 0 });
            assert.equal(options.schema(body), true);
            return body;
        },
    });
    const response = await service.search({
        project: '项目 A', q: '  剧情 命中  ', scope: 'pinned', limit: 100, signal,
    });
    assert.equal(response.project, '项目 A');
    assert.equal(calls.length, 1);
    const requestUrl = new URL(`http://local${calls[0].path}`);
    assert.equal(requestUrl.pathname, '/api/search');
    assert.equal(requestUrl.searchParams.get('project'), '项目 A');
    assert.equal(requestUrl.searchParams.get('q'), '剧情 命中');
    assert.equal(requestUrl.searchParams.get('scope'), 'pinned');
    assert.equal(requestUrl.searchParams.get('limit'), '100');
    assert.equal(calls[0].options.signal, signal);

    for (const request of [
        { project: '项目 A', q: '   ' },
        { project: '项目 A', q: '字'.repeat(129) },
        { project: '项目 A', q: '命中', scope: 'regex' },
        { project: '项目 A', q: '命中', limit: 0 },
        { project: '项目 A', q: '命中', limit: 101 },
    ]) {
        await assert.rejects(() => service.search(request), TypeError);
    }

    const unvalidated = createSearchService({ async get() { return { results: [] }; } });
    await assert.rejects(
        () => unvalidated.search({ project: '项目 A', q: '命中' }),
        /搜索响应顶层字段无效/,
    );
});

test('片段和结果只使用文本节点与 mark，HTML 载荷保持纯文本且结果为原生按钮', async () => {
    const { renderSearchResults, renderSearchSnippet } = await loadSearch();
    const documentRef = new FakeDocument();
    const standalone = documentRef.createElement('span');
    const payload = {
        prefix: '<img src=x onerror=alert(1)>',
        match: '<script>alert(2)</script>',
        suffix: '&lt;仍是文本&gt;',
    };
    const mark = renderSearchSnippet(documentRef, standalone, payload);
    assert.equal(standalone.children.length, 3);
    assert.equal(standalone.children[0].nodeType, 3);
    assert.equal(standalone.children[0].textContent, payload.prefix);
    assert.equal(mark.tagName, 'MARK');
    assert.equal(mark.textContent, payload.match);
    assert.equal(standalone.children[2].nodeType, 3);
    assert.equal(standalone.children[2].textContent, payload.suffix);

    const container = documentRef.createElement('div');
    const activated = [];
    const result = messageResult({ snippet: payload, pinned: true });
    const buttons = renderSearchResults(documentRef, container, [result], (...args) => activated.push(args));
    assert.equal(buttons.length, 1);
    assert.equal(buttons[0].tagName, 'BUTTON');
    assert.equal(buttons[0].type, 'button');
    assert.equal(buttons[0].children[1].children[1].tagName, 'MARK');
    assert.equal(buttons[0].children[1].children[1].textContent, payload.match);
    buttons[0].dispatch('click');
    assert.equal(activated.length, 1);
    assert.equal(activated[0][0], result);
    assert.equal(activated[0][1], buttons[0]);

    const empty = documentRef.createElement('div');
    assert.deepEqual(renderSearchResults(documentRef, empty, [], () => {}), []);
    assert.equal(empty.children[0].tagName, 'P');
    assert.equal(empty.children[0].textContent, '没有找到匹配内容');
});

test('重复搜索会中止前一请求，并丢弃迟到响应、切换会话响应和已卸载 Modal 响应', async () => {
    const { createLatestSearchController } = await loadSearch();
    const requests = [];
    const outcomes = [];
    const ref = { project: '项目A', save: '存档A', epoch: 1 };
    const controller = createLatestSearchController({
        service: {
            search(request) {
                const task = deferred();
                requests.push({ request, task });
                return task.promise;
            },
        },
        captureSessionRef: () => ({ ...ref }),
        isCurrentSessionRef: candidate => candidate.epoch === ref.epoch,
        onResults: response => outcomes.push(response),
    });

    const first = controller.run({ project: '项目A', q: '一', mount: { isConnected: true } });
    const second = controller.run({ project: '项目A', q: '二', mount: { isConnected: true } });
    assert.equal(requests.length, 2);
    assert.equal(requests[0].request.signal.aborted, true);
    assert.equal(requests[1].request.signal.aborted, false);
    requests[1].task.resolve(searchResponse());
    const secondResult = await second;
    assert.equal(secondResult.accepted, true);
    requests[0].task.resolve(searchResponse({ results: [], total_matches: 0 }));
    const firstResult = await first;
    assert.equal(firstResult.accepted, false);
    assert.equal(firstResult.reason, 'stale_request');
    assert.equal(outcomes.length, 1);

    const switchedTask = deferred();
    const switchedController = createLatestSearchController({
        service: { search: () => switchedTask.promise },
        captureSessionRef: () => ({ ...ref }),
        isCurrentSessionRef: candidate => candidate.epoch === ref.epoch,
        onResults: response => outcomes.push(response),
    });
    const switched = switchedController.run({ project: '项目A', q: '三', mount: { isConnected: true } });
    ref.epoch = 2;
    switchedTask.resolve(searchResponse());
    assert.equal((await switched).reason, 'stale_session');
    assert.equal(outcomes.length, 1);

    const detached = createLatestSearchController({
        service: { search: async () => searchResponse() },
        captureSessionRef: () => ({ ...ref }),
        isCurrentSessionRef: () => true,
        onResults: response => outcomes.push(response),
    });
    const detachedResult = await detached.run({ project: '项目A', q: '四', mount: { isConnected: false } });
    assert.equal(detachedResult.reason, 'disconnected');
    assert.equal(outcomes.length, 1);
});

test('点击定位先校验 SessionRef/忙碌状态，并严格要求目标存档、revision 与 UUID', async () => {
    const { navigateToSearchResult } = await loadSearch();
    const originRef = { project: '项目A', save: '存档A', epoch: 1 };
    const result = messageResult();
    const authoritativeReads = [];
    const base = {
        result,
        originRef,
        captureSessionRef: () => ({ ...originRef }),
        getSession: async targetRef => {
            authoritativeReads.push(targetRef);
            return ({
            session_id: '存档A', revision: 7,
            message_history: [{ id: result.message_id, content: '目标' }], summaries: [],
            });
        },
        locateMessage: async id => ({ id }),
    };

    assert.equal((await navigateToSearchResult({
        ...base,
        captureSessionRef: () => ({ ...originRef, epoch: 2 }),
    })).code, 'stale_origin');
    assert.equal((await navigateToSearchResult({ ...base, isNavigationBusy: () => true })).code, 'navigation_busy');
    assert.equal((await navigateToSearchResult({ ...base, isTurnActive: () => true })).code, 'active_turn');
    assert.equal((await navigateToSearchResult({
        ...base,
        getSession: async () => ({ ...await base.getSession(), revision: 8 }),
    })).code, 'revision_mismatch');

    let locateCalls = 0;
    const missingId = await navigateToSearchResult({
        ...base,
        getSession: async () => ({
            session_id: '存档A', revision: 7,
            message_history: [{ id: '22222222-2222-4222-8222-222222222222', content: '相邻消息' }],
            summaries: [],
        }),
        locateMessage: async () => { locateCalls += 1; return { index: 0 }; },
    });
    assert.equal(missingId.code, 'message_not_found');
    assert.equal(locateCalls, 0, 'UUID 缺失时禁止用下标或相邻消息兜底');

    const located = [];
    const success = await navigateToSearchResult({
        ...base,
        onLocated: async (...args) => located.push(args),
    });
    assert.equal(success.ok, true);
    assert.equal(success.record.id, result.message_id);
    assert.equal(success.target.id, result.message_id);
    assert.equal(located.length, 1);
    assert.ok(authoritativeReads.length > 0, '当前存档定位也必须重新读取权威 Session');
    assert.deepEqual(authoritativeReads.at(-1), originRef);

    const staleCache = await navigateToSearchResult({
        ...base,
        getSession: async () => ({ ...await base.getSession(originRef), revision: 8 }),
    });
    assert.equal(staleCache.code, 'revision_mismatch');
});

test('跨存档定位等待切换后再做严格校验，摘要在零消息会话也能定位', async () => {
    const { navigateToSearchResult } = await loadSearch();
    const originRef = { project: '项目A', save: '存档A', epoch: 1 };
    let currentRef = { ...originRef };
    const targetResult = messageResult({ save_id: '存档B', revision: 12 });
    const calls = [];
    const crossSave = await navigateToSearchResult({
        result: targetResult,
        originRef,
        captureSessionRef: () => ({ ...currentRef }),
        switchSave: async saveId => {
            calls.push(`switch:${saveId}`);
            currentRef = { project: '项目A', save: saveId, epoch: 2 };
            return true;
        },
        getSession: async () => {
            calls.push('session');
            return {
                session_id: '存档B', revision: 12,
                message_history: [{ id: targetResult.message_id }], summaries: [],
            };
        },
        locateMessage: async messageId => {
            calls.push(`locate:${messageId}`);
            return { messageId };
        },
    });
    assert.equal(crossSave.ok, true);
    assert.deepEqual(calls, [
        'switch:存档B',
        'session',
        `locate:${targetResult.message_id}`,
    ]);

    const summary = summaryResult({ save_id: '存档B', revision: 12 });
    const summaryCalls = [];
    const noMessages = await navigateToSearchResult({
        result: summary,
        originRef: { ...currentRef },
        captureSessionRef: () => ({ ...currentRef }),
        getSession: async () => ({
            session_id: '存档B', revision: 12, message_history: [],
            summaries: [{ id: summary.summary_id, text: '摘要目标' }],
        }),
        revealSummary: async summaryId => {
            summaryCalls.push(summaryId);
            return { summaryId };
        },
    });
    assert.equal(noMessages.ok, true);
    assert.equal(noMessages.record.id, summary.summary_id);
    assert.deepEqual(summaryCalls, [summary.summary_id]);
});

test('剧情记忆锚点独立于消息数量，并能成为可聚焦的精确定位目标', async () => {
    const { ensureSummaryAnchor, focusSearchTarget } = await loadSearch();
    const documentRef = new FakeDocument();
    const stream = documentRef.createElement('main');
    const anchor = ensureSummaryAnchor(documentRef, stream);
    assert.equal(anchor.id, 'summary-anchor');
    assert.equal(anchor.getAttribute('aria-hidden'), 'true');
    assert.deepEqual(stream.children, [anchor]);

    const panel = documentRef.createElement('section');
    panel.className = 'summary-panel';
    stream.appendChild(panel);
    assert.equal(ensureSummaryAnchor(documentRef, stream), anchor);
    assert.deepEqual(stream.children, [anchor, panel]);

    assert.equal(focusSearchTarget(panel), true);
    assert.equal(panel.getAttribute('tabindex'), '-1');
    assert.deepEqual(panel.scrollCalls, [{ block: 'center', behavior: 'smooth' }]);
    assert.equal(panel.focusCount, 1);
    assert.deepEqual(panel.focusOptions, { preventScroll: true });
});

test('定位请求与搜索 Modal 生命周期绑定，卸载后不得定位或调用旧回调', async () => {
    const { navigateToSearchResult } = await loadSearch();
    const originRef = { project: '项目A', save: '存档A', epoch: 1 };
    const result = messageResult();
    const sessionTask = deferred();
    let mounted = true;
    let locateCalls = 0;
    let locatedCalls = 0;
    const navigation = navigateToSearchResult({
        result,
        originRef,
        captureSessionRef: () => ({ ...originRef }),
        isMounted: () => mounted,
        getSession: () => sessionTask.promise,
        locateMessage: async () => { locateCalls += 1; return {}; },
        onLocated: async () => { locatedCalls += 1; },
    });

    mounted = false;
    sessionTask.resolve({
        session_id: '存档A', revision: 7,
        message_history: [{ id: result.message_id }], summaries: [],
    });
    const outcome = await navigation;
    assert.equal(outcome.code, 'navigation_cancelled');
    assert.equal(locateCalls, 0);
    assert.equal(locatedCalls, 0);
});

test('结果定位 single-flight 仅允许持有 token 的请求解除在途状态', async () => {
    const { createSingleFlightGate } = await loadSearch();
    const gate = createSingleFlightGate();
    const first = gate.acquire();
    assert.ok(first);
    assert.equal(gate.isActive(), true);
    const second = gate.acquire();
    assert.equal(second, null, '第二次点击必须直接拒绝');
    assert.equal(gate.release(second), false, '被拒请求不能解除第一条请求的锁');
    assert.equal(gate.isActive(), true);
    assert.equal(gate.owns(first), true);
    assert.equal(gate.release(first), true);
    assert.equal(gate.isActive(), false);
});
