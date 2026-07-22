export const SEARCH_SCOPES = Object.freeze(['all', 'messages', 'summaries', 'pinned']);

const TOP_LEVEL_KEYS = Object.freeze([
    'project', 'results', 'scanned', 'scope', 'skipped', 'total_matches', 'truncated',
]);
const SCANNED_KEYS = Object.freeze(['bytes', 'sessions', 'sources']);
const SKIPPED_KEYS = Object.freeze(['code', 'save_id']);
const SNIPPET_KEYS = Object.freeze(['match', 'prefix', 'suffix']);
const MESSAGE_KEYS = Object.freeze([
    'field', 'kind', 'message_id', 'pinned', 'revision', 'role', 'save_id', 'snippet', 'time',
]);
const SUMMARY_KEYS = Object.freeze([
    'field', 'kind', 'revision', 'save_id', 'snippet', 'summary_id', 'time',
]);
const SUMMARY_FIELDS = Object.freeze(['text', 'time', 'facts', 'relations']);
const MAX_SNIPPET_CODE_POINTS = 240;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isRecord(value) {
    return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function hasExactKeys(value, expected) {
    if (!isRecord(value)) return false;
    const actual = Object.keys(value).sort();
    const wanted = [...expected].sort();
    return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

function isCount(value) {
    return Number.isSafeInteger(value) && value >= 0;
}

function codePointLength(value) {
    return Array.from(value).length;
}

function validateSnippet(snippet) {
    if (!hasExactKeys(snippet, SNIPPET_KEYS)) return 'snippet 字段不完整';
    if (!['prefix', 'match', 'suffix'].every(key => typeof snippet[key] === 'string')) {
        return 'snippet 必须是字符串片段';
    }
    if (!snippet.match) return 'snippet.match 不能为空';
    if (codePointLength(snippet.prefix + snippet.match + snippet.suffix) > MAX_SNIPPET_CODE_POINTS) {
        return 'snippet 超过 240 字符';
    }
    return '';
}

function validateResult(result) {
    if (!isRecord(result) || !['message', 'summary'].includes(result.kind)) return '搜索结果类型无效';
    const expectedKeys = result.kind === 'message' ? MESSAGE_KEYS : SUMMARY_KEYS;
    if (!hasExactKeys(result, expectedKeys)) return `${result.kind} 搜索结果字段不完整`;
    if (typeof result.save_id !== 'string' || !result.save_id) return '搜索结果缺少 save_id';
    if (!isCount(result.revision)) return '搜索结果 revision 无效';
    if (typeof result.time !== 'string') return '搜索结果 time 无效';
    const snippetError = validateSnippet(result.snippet);
    if (snippetError) return snippetError;
    if (result.kind === 'message') {
        if (result.field !== 'content') return '消息搜索字段无效';
        if (typeof result.message_id !== 'string' || !UUID_RE.test(result.message_id)) {
            return '消息搜索结果 message_id 无效';
        }
        if (!['user', 'assistant'].includes(result.role)) return '消息搜索结果 role 无效';
        if (typeof result.pinned !== 'boolean') return '消息搜索结果 pinned 无效';
    } else {
        if (!SUMMARY_FIELDS.includes(result.field)) return '摘要搜索字段无效';
        if (typeof result.summary_id !== 'string' || !UUID_RE.test(result.summary_id)) {
            return '摘要搜索结果 summary_id 无效';
        }
    }
    return '';
}

export function validateSearchResponse(payload, expected = {}) {
    if (!hasExactKeys(payload, TOP_LEVEL_KEYS)) return '搜索响应顶层字段无效';
    if (typeof payload.project !== 'string' || !payload.project) return '搜索响应 project 无效';
    if (!SEARCH_SCOPES.includes(payload.scope)) return '搜索响应 scope 无效';
    if (expected.project !== undefined && payload.project !== expected.project) return '搜索响应 project 不匹配';
    if (expected.scope !== undefined && payload.scope !== expected.scope) return '搜索响应 scope 不匹配';
    if (!Array.isArray(payload.results) || payload.results.length > 100) return '搜索响应 results 无效';
    if (expected.limit !== undefined && payload.results.length > expected.limit) {
        return '搜索响应 results 超过请求上限';
    }
    if (!isCount(payload.total_matches) || payload.total_matches < payload.results.length) {
        return '搜索响应 total_matches 无效';
    }
    if (typeof payload.truncated !== 'boolean'
        || payload.truncated !== (payload.total_matches > payload.results.length)) {
        return '搜索响应 truncated 无效';
    }
    if (!hasExactKeys(payload.scanned, SCANNED_KEYS)
        || !SCANNED_KEYS.every(key => isCount(payload.scanned[key]))) {
        return '搜索响应 scanned 无效';
    }
    if (payload.scanned.sessions > 500
        || payload.scanned.sources > 50000
        || payload.scanned.bytes > 64 * 1024 * 1024) {
        return '搜索响应扫描计数越界';
    }
    if (!Array.isArray(payload.skipped) || payload.skipped.some(item => (
        !hasExactKeys(item, SKIPPED_KEYS)
        || typeof item.save_id !== 'string'
        || typeof item.code !== 'string'
        || !item.code
    ))) {
        return '搜索响应 skipped 无效';
    }
    const identities = new Set();
    for (const result of payload.results) {
        const error = validateResult(result);
        if (error) return error;
        if (payload.scope === 'summaries' && result.kind !== 'summary') return '摘要范围包含非摘要结果';
        if (['messages', 'pinned'].includes(payload.scope) && result.kind !== 'message') {
            return '消息范围包含非消息结果';
        }
        if (payload.scope === 'pinned' && !result.pinned) return '钉选范围包含未钉选消息';
        const stableId = result.kind === 'message' ? result.message_id : result.summary_id;
        const identity = `${result.kind}\u0000${result.save_id}\u0000${stableId}\u0000${result.field}`;
        if (identities.has(identity)) return '搜索响应包含重复结果';
        identities.add(identity);
    }
    return true;
}

function normalizeRequest({ project, q, scope = 'all', limit = 50 }) {
    if (typeof project !== 'string' || !project) throw new TypeError('搜索 project 不能为空');
    if (typeof q !== 'string') throw new TypeError('搜索词必须是字符串');
    const query = q.trim();
    if (!query) throw new TypeError('请输入搜索词');
    if (codePointLength(query) > 128) throw new TypeError('搜索词最多 128 个字符');
    if (!SEARCH_SCOPES.includes(scope)) throw new TypeError('搜索范围无效');
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
        throw new TypeError('搜索数量必须在 1 到 100 之间');
    }
    return Object.freeze({ project, q: query, scope, limit });
}

export function createSearchService(apiClient, options = {}) {
    if (!apiClient || typeof apiClient.get !== 'function') {
        throw new TypeError('搜索服务需要 ApiClient.get');
    }
    const endpoint = options.endpoint || '/api/search';
    return Object.freeze({
        async search(request) {
            const normalized = normalizeRequest(request || {});
            const params = new URLSearchParams({
                project: normalized.project,
                q: normalized.q,
                scope: normalized.scope,
                limit: String(normalized.limit),
            });
            const expected = {
                project: normalized.project,
                scope: normalized.scope,
                limit: normalized.limit,
            };
            const payload = await apiClient.get(`${endpoint}?${params.toString()}`, {
                signal: request && request.signal,
                schema: body => validateSearchResponse(body, expected),
            });
            const validation = validateSearchResponse(payload, expected);
            if (validation !== true) throw new TypeError(validation);
            return payload;
        },
    });
}

function isAbort(error, signal) {
    return Boolean(signal && signal.aborted)
        || Boolean(error && (error.name === 'AbortError' || error.code === 'request_aborted'));
}

export function createSingleFlightGate() {
    let serial = 0;
    let active = null;
    return Object.freeze({
        acquire() {
            if (active) return null;
            active = Object.freeze({ id: ++serial });
            return active;
        },
        owns(token) {
            return Boolean(token && token === active);
        },
        release(token) {
            if (!token || token !== active) return false;
            active = null;
            return true;
        },
        isActive() {
            return Boolean(active);
        },
    });
}

export function createLatestSearchController(options) {
    if (!options || !options.service || typeof options.service.search !== 'function') {
        throw new TypeError('搜索控制器缺少 service.search');
    }
    if (typeof options.captureSessionRef !== 'function' || typeof options.isCurrentSessionRef !== 'function') {
        throw new TypeError('搜索控制器缺少 SessionRef 依赖');
    }
    const AbortControllerImpl = options.AbortControllerImpl || globalThis.AbortController;
    if (typeof AbortControllerImpl !== 'function') throw new TypeError('环境不支持 AbortController');
    const connected = options.isConnected || (mount => !mount || Boolean(mount.isConnected));
    let serial = 0;
    let active = null;

    async function run(request) {
        const token = ++serial;
        if (active) active.controller.abort();
        const controller = new AbortControllerImpl();
        const originRef = options.captureSessionRef();
        const mount = request && request.mount;
        active = { token, controller };
        if (!request || request.project !== originRef.project) {
            active = null;
            return { accepted: false, reason: 'stale_session', originRef, token };
        }
        try {
            const response = await options.service.search({ ...request, signal: controller.signal });
            if (token !== serial || controller.signal.aborted) {
                return { accepted: false, reason: 'stale_request', originRef, token };
            }
            if (!options.isCurrentSessionRef(originRef)) {
                active = null;
                return { accepted: false, reason: 'stale_session', originRef, token };
            }
            if (!connected(mount)) {
                active = null;
                return { accepted: false, reason: 'disconnected', originRef, token };
            }
            active = null;
            if (typeof options.onResults === 'function') options.onResults(response, { originRef, token, mount });
            return { accepted: true, response, originRef, token };
        } catch (error) {
            if (token !== serial || isAbort(error, controller.signal)) {
                return { accepted: false, reason: 'aborted', originRef, token };
            }
            active = null;
            if (!options.isCurrentSessionRef(originRef)) {
                return { accepted: false, reason: 'stale_session', originRef, token };
            }
            if (!connected(mount)) {
                return { accepted: false, reason: 'disconnected', originRef, token };
            }
            if (typeof options.onError === 'function') options.onError(error, { originRef, token, mount });
            return { accepted: false, reason: 'error', error, originRef, token };
        }
    }

    function cancel() {
        serial += 1;
        if (active) active.controller.abort();
        active = null;
    }

    return Object.freeze({ run, cancel, isActive: () => Boolean(active) });
}

export function renderSearchSnippet(documentRef, container, snippet) {
    if (!documentRef || typeof documentRef.createElement !== 'function'
        || typeof documentRef.createTextNode !== 'function'
        || !container || typeof container.replaceChildren !== 'function') {
        throw new TypeError('搜索片段渲染缺少 DOM 依赖');
    }
    const error = validateSnippet(snippet);
    if (error) throw new TypeError(error);
    const mark = documentRef.createElement('mark');
    mark.textContent = snippet.match;
    container.replaceChildren(
        documentRef.createTextNode(snippet.prefix),
        mark,
        documentRef.createTextNode(snippet.suffix),
    );
    return mark;
}

function resultLabel(result) {
    if (result.kind === 'message') {
        const role = result.role === 'user' ? '你' : 'AI';
        return `${role}${result.pinned ? ' · 已钉选' : ''}`;
    }
    const labels = { text: '前情提要', time: '时间线', facts: '关键事件', relations: '角色关系' };
    return `剧情记忆 · ${labels[result.field]}`;
}

export function renderSearchResults(documentRef, container, results, onActivate) {
    if (!documentRef || !container || typeof container.replaceChildren !== 'function' || !Array.isArray(results)) {
        throw new TypeError('搜索结果渲染参数无效');
    }
    container.replaceChildren();
    if (results.length === 0) {
        const empty = documentRef.createElement('p');
        empty.className = 'search-empty';
        empty.textContent = '没有找到匹配内容';
        container.appendChild(empty);
        return [];
    }
    const buttons = [];
    for (const result of results) {
        const validation = validateResult(result);
        if (validation) throw new TypeError(validation);
        const button = documentRef.createElement('button');
        button.type = 'button';
        button.className = 'search-result';
        const meta = documentRef.createElement('span');
        meta.className = 'search-result-meta';
        meta.textContent = `${resultLabel(result)} · ${result.save_id}${result.time ? ` · ${result.time}` : ''}`;
        const snippet = documentRef.createElement('span');
        snippet.className = 'search-result-snippet';
        renderSearchSnippet(documentRef, snippet, result.snippet);
        button.appendChild(meta);
        button.appendChild(snippet);
        button.addEventListener('click', () => {
            if (typeof onActivate === 'function') onActivate(result, button);
        });
        container.appendChild(button);
        buttons.push(button);
    }
    return buttons;
}

function sameRef(left, right) {
    return Boolean(left && right
        && left.project === right.project
        && left.save === right.save
        && left.epoch === right.epoch);
}

export function findMessageById(session, messageId) {
    if (!session || !Array.isArray(session.message_history) || typeof messageId !== 'string') return null;
    return session.message_history.find(message => message && message.id === messageId) || null;
}

export function findSummaryById(session, summaryId) {
    if (!session || !Array.isArray(session.summaries) || typeof summaryId !== 'string') return null;
    return session.summaries.find(summary => summary && summary.id === summaryId) || null;
}

export async function navigateToSearchResult(options) {
    if (!options || typeof options.captureSessionRef !== 'function' || typeof options.getSession !== 'function') {
        throw new TypeError('搜索定位缺少会话依赖');
    }
    const resultError = validateResult(options.result);
    if (resultError) return { ok: false, code: 'invalid_result' };
    const sameSessionRef = options.sameSessionRef || sameRef;
    const isMounted = typeof options.isMounted === 'function' ? options.isMounted : () => true;
    if (!isMounted()) return { ok: false, code: 'navigation_cancelled' };
    const current = options.captureSessionRef();
    if (!sameSessionRef(current, options.originRef)) return { ok: false, code: 'stale_origin' };
    if (typeof options.isNavigationBusy === 'function' && options.isNavigationBusy()) {
        return { ok: false, code: 'navigation_busy' };
    }
    if (typeof options.isTurnActive === 'function' && options.isTurnActive(current)) {
        return { ok: false, code: 'active_turn' };
    }
    if (current.project !== options.originRef.project) return { ok: false, code: 'project_mismatch' };

    if (options.result.save_id !== current.save) {
        if (typeof options.switchSave !== 'function') return { ok: false, code: 'switch_unavailable' };
        try {
            const switched = await options.switchSave(options.result.save_id);
            if (switched === false) return { ok: false, code: 'switch_failed' };
        } catch (_error) {
            return { ok: false, code: 'switch_failed' };
        }
    }
    if (!isMounted()) return { ok: false, code: 'navigation_cancelled' };

    const targetRef = options.captureSessionRef();
    if (!targetRef
        || targetRef.project !== options.originRef.project
        || targetRef.save !== options.result.save_id) {
        return { ok: false, code: 'target_ref_mismatch' };
    }
    if (typeof options.isNavigationBusy === 'function' && options.isNavigationBusy()) {
        return { ok: false, code: 'navigation_busy' };
    }
    if (typeof options.isTurnActive === 'function' && options.isTurnActive(targetRef)) {
        return { ok: false, code: 'active_turn' };
    }

    let session;
    try {
        session = await options.getSession(targetRef);
    } catch (_error) {
        return { ok: false, code: 'session_unavailable' };
    }
    if (!isMounted()) return { ok: false, code: 'navigation_cancelled' };
    if (!sameSessionRef(options.captureSessionRef(), targetRef)) {
        return { ok: false, code: 'target_ref_mismatch' };
    }
    if (!session || session.session_id !== options.result.save_id) {
        return { ok: false, code: 'session_mismatch' };
    }
    if (session.revision !== options.result.revision) return { ok: false, code: 'revision_mismatch' };

    let record;
    let target;
    if (options.result.kind === 'message') {
        record = findMessageById(session, options.result.message_id);
        if (!record) return { ok: false, code: 'message_not_found' };
        if (typeof options.locateMessage !== 'function') return { ok: false, code: 'message_target_unavailable' };
        target = await options.locateMessage(options.result.message_id);
        if (!target) return { ok: false, code: 'message_target_not_found' };
    } else {
        record = findSummaryById(session, options.result.summary_id);
        if (!record) return { ok: false, code: 'summary_not_found' };
        if (typeof options.revealSummary !== 'function') return { ok: false, code: 'summary_target_unavailable' };
        target = await options.revealSummary(options.result.summary_id);
        if (!target) return { ok: false, code: 'summary_target_not_found' };
    }
    if (!isMounted()) return { ok: false, code: 'navigation_cancelled' };
    if (typeof options.onLocated === 'function') await options.onLocated(target, record, targetRef);
    return { ok: true, target, record, sessionRef: targetRef };
}

export function ensureSummaryAnchor(documentRef, stream) {
    if (!documentRef || !stream || typeof stream.querySelector !== 'function') {
        throw new TypeError('剧情记忆锚点缺少 DOM 依赖');
    }
    let anchor = stream.querySelector('#summary-anchor');
    if (!anchor) {
        anchor = documentRef.createElement('div');
        anchor.id = 'summary-anchor';
        anchor.className = 'summary-anchor';
        anchor.setAttribute('aria-hidden', 'true');
    }
    const panel = anchor.parentNode === stream
        && anchor.nextElementSibling
        && anchor.nextElementSibling.classList
        && anchor.nextElementSibling.classList.contains('summary-panel')
        ? anchor.nextElementSibling
        : null;
    stream.appendChild(anchor);
    if (panel) stream.appendChild(panel);
    return anchor;
}

export function focusSearchTarget(target) {
    if (!target) return false;
    if (typeof target.hasAttribute === 'function'
        && typeof target.setAttribute === 'function'
        && !target.hasAttribute('tabindex')) {
        target.setAttribute('tabindex', '-1');
    }
    const view = target.ownerDocument && target.ownerDocument.defaultView;
    const reduceMotion = Boolean(
        view
        && typeof view.matchMedia === 'function'
        && view.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
    if (typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({ block: 'center', behavior: reduceMotion ? 'auto' : 'smooth' });
    }
    if (typeof target.focus === 'function') {
        try { target.focus({ preventScroll: true }); }
        catch (_error) { target.focus(); }
    }
    return true;
}
