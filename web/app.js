// 本地酒馆 — 前端逻辑 v2（项目+存档双层架构）
// 流式对话、角色卡渲染、行动建议、会话管理、提示词编辑

if (!globalThis.TavernSecurity) throw new Error('安全渲染模块未加载');
if (!globalThis.TavernApi) throw new Error('ApiClient 模块未加载');
if (!globalThis.TavernSessionRef) throw new Error('SessionRef 模块未加载');
if (!globalThis.TavernTurn) throw new Error('TurnClient 模块未加载');

const { ApiClient, ApiError, payloadMessage } = globalThis.TavernApi;
const {
    SessionRefTracker,
    sessionBelongsToRef,
    buildSessionCommit,
    shouldCreateDefaultSave,
} = globalThis.TavernSessionRef;
const {
    TurnClient,
    TERMINAL_STATUSES,
    createTurnState,
    reduceTurnEvent,
    canPerformAction,
} = globalThis.TavernTurn;
const apiClient = new ApiClient({ timeoutMs: 15000 });
const turnClient = new TurnClient(apiClient);

const API = {
    models: '/api/models',
    characters: '/api/characters',
    user: '/api/user',
    worldbook: '/api/worldbook',
    session: '/api/session',
    reset: '/api/session/reset',
    switchModel: '/api/model/switch',
    // 项目
    projects: '/api/projects',
    // 多存档
    sessions: '/api/sessions',
    sessionCreate: '/api/sessions',
    sessionRename: '/api/sessions/rename',
    sessionDelete: '/api/sessions/delete',
    sessionExport: '/api/sessions/export',
    sessionImport: '/api/sessions/import',
    // 历史快照
    sessionHistory: '/api/session/history',
    sessionRestore: '/api/session/restore',
    sessionSnapshot: (filename) => `/api/session/snapshot?filename=${encodeURIComponent(filename)}`,
    summaryRegen: '/api/session/summary/regenerate',
    // 角色/世界书/用户
    characterSchema: '/api/schema/character',
    characterSave: (id) => `/api/characters/${encodeURIComponent(id)}`,
    characterDelete: (id) => `/api/characters/${encodeURIComponent(id)}`,
    worldbookSave: (id) => `/api/worldbook/${encodeURIComponent(id)}`,
    worldbookDelete: (id) => `/api/worldbook/${encodeURIComponent(id)}`,
    userSave: '/api/user',
    userDelete: '/api/user',
    settings: '/api/settings',
    // 提示词
    prompts: '/api/prompts',
    promptSave: (name) => `/api/prompts/${name}`,
    promptReset: (name) => `/api/prompts/${name}/reset`,
};

const state = {
    currentProject: '默认项目',
    currentSave: '默认存档',
    session: null,
    characters: [],
    projectList: [],
    saveList: [],
    isStreaming: false,
    navigationBusy: false,
    activeTurn: null,
    modelParams: {
        temperature: 0.8,
        top_p: 0.9,
        top_k: 40,
        num_predict: 4096,
        think: true,
    },
};

const sessionRefs = new SessionRefTracker(state.currentProject, state.currentSave);
let committedSessionRef = sessionRefs.capture();
let activeController = null;   // 当前 turn SSE 的 AbortController；业务取消必须调用服务端 cancel API
const latestRequest = { projects: 0, projectStats: 0, saves: 0 };

function currentRevision(ref = captureSessionRef()) {
    if (!isCurrentSessionRef(ref) || !sessionBelongsToRef(state.session, ref)) return 0;
    const revision = state.session && state.session.revision;
    return Number.isInteger(revision) && revision >= 0 ? revision : 0;
}

function errorDetail(payload, fallback = '请求失败') {
    if (payload instanceof Error) return payload.message || fallback;
    return payloadMessage(payload, fallback);
}

function captureSessionRef() {
    return sessionRefs.capture();
}

function isCurrentSessionRef(ref) {
    return sessionRefs.isCurrent(ref);
}

function setNavigationUiState(active) {
    state.navigationBusy = active;
    const blocked = active || Boolean(state.activeTurn && !state.activeTurn.terminal);
    for (const id of ['project-btn', 'tab-saves', 'model-select', 'reset-btn']) {
        const element = document.getElementById(id);
        if (!element) continue;
        if ('disabled' in element) element.disabled = blocked;
        element.setAttribute('aria-disabled', String(blocked));
        element.setAttribute('aria-busy', String(active));
    }
}

function beginSessionTransition(project, save = null) {
    setNavigationUiState(true);
    return sessionRefs.advance(project, save);
}

function rollbackSessionTransition(candidateRef) {
    if (isCurrentSessionRef(candidateRef)) {
        sessionRefs.advance(committedSessionRef.project, committedSessionRef.save);
        setNavigationUiState(false);
    }
}

function commitSessionIdentity(ref) {
    if (!isCurrentSessionRef(ref)) return false;
    state.currentProject = ref.project;
    state.currentSave = ref.save;
    committedSessionRef = ref;
    setNavigationUiState(false);
    return true;
}

function sessionFromResult(body) {
    if (!body || typeof body !== 'object') return null;
    const session = body.session || body;
    if (!session || typeof session !== 'object') return null;
    if (!session.session_id || !Number.isInteger(session.revision)) return null;
    return session;
}

function applySessionResult(payload, ref = null) {
    if (ref && !isCurrentSessionRef(ref)) return null;
    const session = sessionFromResult(payload);
    if (!session || (ref && !sessionBelongsToRef(session, ref))) return null;
    if (!commitSessionState(session, ref || captureSessionRef())) return null;
    return session;
}

function commitSessionState(session, ref) {
    const commit = buildSessionCommit({
        session,
        ref,
        currentRef: captureSessionRef(),
        currentSession: state.session,
        saveList: state.saveList,
    });
    if (!commit.accepted) return false;
    if (!commitSessionIdentity(ref)) return false;
    state.session = commit.session;
    state.saveList = commit.saveList;
    renderSession(commit.session);
    renderHistory(commit.messageHistory);
    const modelSelect = document.getElementById('model-select');
    if (modelSelect && commit.currentModel) modelSelect.value = commit.currentModel;
    renderSaveListControls();
    if (isTurnActiveForRef(ref)) setTurnUiState(true, Boolean(state.activeTurn && state.activeTurn.cancelling));
    return true;
}

function isTurnActiveForRef(ref = captureSessionRef()) {
    return Boolean(
        state.activeTurn
        && !state.activeTurn.terminal
        && state.activeTurn.ref
        && state.activeTurn.ref.project === ref.project
        && state.activeTurn.ref.save === ref.save
        && state.activeTurn.ref.epoch === ref.epoch
    );
}

async function sessionWrite(url, method, payload, label = '保存', options = {}) {
    const requestRef = captureSessionRef();
    if (state.navigationBusy) throw new ApiError('正在切换项目或存档，请稍候', { code: 'navigation_busy' });
    if (isTurnActiveForRef(requestRef)) {
        throw new ApiError('当前存档正在生成，请先取消或等待完成', { code: 'active_turn_client' });
    }
    if (
        (payload && payload.project !== undefined && payload.project !== requestRef.project)
        || (payload && payload.save !== undefined && payload.save !== requestRef.save)
    ) {
        throw new ApiError(`${label}目标与当前存档不一致`, { code: 'stale_session_ref' });
    }
    const expectedRevision = currentRevision(requestRef);
    if (!sessionBelongsToRef(state.session, requestRef)) {
        throw new ApiError('当前存档尚未加载完成', { code: 'session_not_ready' });
    }
    let result;
    try {
        result = await apiClient.request(url, {
            method,
            json: { ...payload, expected_revision: expectedRevision },
            signal: options.signal,
            timeoutMs: options.timeoutMs,
        });
    } catch (error) {
        if (error instanceof ApiError && error.status === 409 && isCurrentSessionRef(requestRef)) {
            try { await reloadCurrentSession(requestRef); } catch (_reloadError) {}
        }
        throw error;
    }
    if (options.applyResult !== false) {
        const applied = applySessionResult(result, requestRef);
        if (!applied && isCurrentSessionRef(requestRef)) {
            throw new ApiError(`${label}响应缺少有效存档`, {
                code: 'invalid_response_schema', payload: result,
            });
        }
    }
    return result;
}

// ===== 项目列表 =====
async function loadProjects() {
    const requestRef = captureSessionRef();
    const requestId = ++latestRequest.projects;
    const data = await apiClient.get(API.projects, {
        schema: body => Array.isArray(body && body.projects) || '项目列表响应无效',
    });
    if (!isCurrentSessionRef(requestRef) || requestId !== latestRequest.projects) return null;
    if (data.projects.length === 0) {
        throw new ApiError('服务端没有可用项目', { code: 'empty_project_list' });
    }
    state.projectList = [...data.projects];

    const oldSelect = document.getElementById('project-select');
    if (oldSelect) {
        oldSelect.innerHTML = '';
        state.projectList.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p;
            opt.textContent = p;
            oldSelect.appendChild(opt);
        });
        oldSelect.value = state.currentProject;
    }
    await renderProjectDropdown(requestRef);
    return state.projectList;
}

// ===== 项目下拉渲染（含元信息） =====
async function renderProjectDropdown(requestRef = captureSessionRef()) {
    const listEl = document.getElementById('project-list');
    if (!listEl) return;
    const requestId = ++latestRequest.projectStats;

    // 并发拉每个项目的 stats（角色/世界书/存档数）
    const stats = {};
    const projectList = [...state.projectList];
    await Promise.all(projectList.map(async (p) => {
        try {
            const [c, w, s] = await Promise.all([
                apiClient.get(`${API.characters}?project=${encodeURIComponent(p)}`, {
                    schema: body => Array.isArray(body && body.characters) || '角色列表响应无效',
                }),
                apiClient.get(`${API.worldbook}?project=${encodeURIComponent(p)}`, {
                    schema: body => Array.isArray(body && body.entries) || '世界书列表响应无效',
                }),
                apiClient.get(`${API.sessions}?project=${encodeURIComponent(p)}`, {
                    schema: body => Array.isArray(body && body.sessions) || '存档列表响应无效',
                }),
            ]);
            stats[p] = {
                chars: (c.characters || []).length,
                world: (w.entries || []).length,
                saves: (s.sessions || []).length,
            };
        } catch (error) {
            console.warn(`加载项目 ${p} 元信息失败`, error);
            stats[p] = { chars: '—', world: '—', saves: '—' };
        }
    }));

    if (!isCurrentSessionRef(requestRef) || requestId !== latestRequest.projectStats) return;

    listEl.innerHTML = projectList.map(p => {
        const st = stats[p] || { chars:0, world:0, saves:0 };
        const active = p === state.currentProject ? 'active' : '';
        return `<div class="dropdown-item ${active}" data-project="${escapeHtml(p)}">
            <span class="item-name">📁 ${escapeHtml(p)}</span>
            <span class="project-item-stats">👥${st.chars} 📖${st.world} 💾${st.saves}</span>
        </div>`;
    }).join('');

    listEl.querySelectorAll('.dropdown-item').forEach(item => {
        item.addEventListener('click', () => {
            const p = item.dataset.project;
            hideAllDropdowns();
            switchProject(p);
        });
    });

    // 更新顶栏项目名 + 元信息
    updateProjectButton(stats);
}

function updateProjectButton(statsMap) {
    const nameEl = document.getElementById('project-name');
    const statsEl = document.getElementById('project-stats');
    if (nameEl) nameEl.textContent = state.currentProject;
    if (statsEl && statsMap) {
        const st = statsMap[state.currentProject] || { chars:0, world:0, saves:0 };
        statsEl.innerHTML = `<span class="stat">👥${st.chars}</span><span class="stat">📖${st.world}</span><span class="stat">💾${st.saves}</span>`;
    }
}

// ===== Toast 提示 =====
function showToast(msg, duration = 1800) {
    const status = document.getElementById('app-status');
    if (status) status.textContent = String(msg || '');
    let el = document.getElementById('toast-msg');
    if (!el) {
        el = document.createElement('div');
        el.id = 'toast-msg';
        el.className = 'toast';
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('show'), duration);
}

// ===== 下拉面板显隐 =====
function hideAllDropdowns() {
    document.querySelectorAll('.dropdown-panel').forEach(p => p.classList.add('hidden'));
}

function positionDropdown(panel, anchor) {
    const r = anchor.getBoundingClientRect();
    panel.style.top = (r.bottom + 4) + 'px';
    panel.style.left = r.left + 'px';
}

// ===== 初始化 =====
async function init() {
    try {
        await Promise.all([loadModels(), loadProjects(), loadSettings()]);
        // 优先用 URL hash 指定的项目；其次用「默认项目」字面量；最后才 fallback 到 projectList[0]
        const hash = window.location.hash.replace('#', '');
        const persistedTurn = readPersistedTurnPointer();
        let initialProject = state.projectList[0];
        if (persistedTurn && state.projectList.includes(persistedTurn.project)) initialProject = persistedTurn.project;
        else if (hash && state.projectList.includes(hash)) initialProject = hash;
        else if (state.projectList.includes('默认项目')) initialProject = '默认项目';
        const preferredSave = persistedTurn && persistedTurn.project === initialProject
            ? persistedTurn.save
            : null;
        await loadProjectContext(initialProject, preferredSave);
        await renderProjectDropdown(captureSessionRef());
        bindUI();
        showToast(`已进入「${state.currentProject}」`, 1500);
        resumePersistedTurn(persistedTurn).catch(error => console.warn('恢复 turn 失败', error));
    } catch (e) {
        console.error('初始化失败', e);
        alert('初始化失败：' + e.message + '\n请确认 server.py 已启动');
    }
}

async function loadSettings() {
    try {
        const s = await apiClient.get(API.settings, {
            schema: body => Boolean(body && typeof body === 'object' && !Array.isArray(body)) || '设置响应无效',
        });
        if (s.temperature !== undefined) state.modelParams.temperature = s.temperature;
        if (s.top_p !== undefined) state.modelParams.top_p = s.top_p;
        if (s.top_k !== undefined) state.modelParams.top_k = s.top_k;
        if (s.num_predict !== undefined) state.modelParams.num_predict = s.num_predict;
        if (s.think !== undefined) state.modelParams.think = s.think;
    } catch (e) { console.warn('读取设置失败', e); }
}

async function saveSettings() {
    try {
        await apiClient.put(API.settings, { data: state.modelParams });
    } catch (e) { console.warn('保存设置失败', e); }
}

async function loadModels() {
    const data = await apiClient.get(API.models, {
        schema: body => Array.isArray(body && body.models) || '模型列表响应无效',
    });
    const select = document.getElementById('model-select');
    select.innerHTML = '';
    data.models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        select.appendChild(opt);
    });
}

async function loadUser(project = state.currentProject) {
    return apiClient.get(`${API.user}?project=${encodeURIComponent(project)}`, {
        schema: body => Boolean(body && typeof body === 'object' && !Array.isArray(body)) || '用户档案响应无效',
    });
}

async function loadCharacters(project = state.currentProject) {
    const data = await apiClient.get(`${API.characters}?project=${encodeURIComponent(project)}`, {
        schema: body => Array.isArray(body && body.characters) || '角色列表响应无效',
    });
    return data.characters;
}

async function fetchSaveList(project) {
    const data = await apiClient.get(`${API.sessions}?project=${encodeURIComponent(project)}`, {
        schema: body => Array.isArray(body && body.sessions) || '存档列表响应无效',
    });
    return data.sessions;
}

async function fetchSession(ref) {
    if (!ref || !ref.save) throw new ApiError('缺少存档引用', { code: 'invalid_session_ref' });
    const url = `${API.session}?project=${encodeURIComponent(ref.project)}&save=${encodeURIComponent(ref.save)}`;
    const session = await apiClient.get(url, {
        schema: body => Boolean(sessionFromResult(body)) || '存档响应无效',
    });
    if (!sessionBelongsToRef(session, ref)) {
        throw new ApiError('服务端返回了其他存档', { code: 'session_ref_mismatch', payload: session });
    }
    return session;
}

async function loadProjectContext(project, preferredSave = null) {
    if (!project) throw new ApiError('缺少项目 ID', { code: 'invalid_project_ref' });
    if (state.navigationBusy) throw new ApiError('正在切换项目或存档，请稍候', { code: 'navigation_busy' });
    if (isTurnActiveForRef(committedSessionRef)) {
        throw new ApiError('当前存档正在生成，请先取消或等待完成', { code: 'active_turn_client' });
    }

    let candidateRef = beginSessionTransition(project, null);
    try {
        const [characters, saves] = await Promise.all([
            loadCharacters(project),
            fetchSaveList(project),
            loadUser(project),
        ]).then(values => [values[0], values[1]]);
        if (!isCurrentSessionRef(candidateRef)) return false;

        let nextSaves = [...saves];
        let session;
        let saveId = preferredSave && saves.some(item => item.session_id === preferredSave)
            ? preferredSave
            : (saves[0] && saves[0].session_id);
        if (!saveId && shouldCreateDefaultSave(saves, candidateRef, captureSessionRef())) {
            session = await apiClient.post(API.sessionCreate, { project, name: '默认存档' }, {
                schema: body => Boolean(sessionFromResult(body)) || '创建存档响应无效',
            });
            if (!isCurrentSessionRef(candidateRef)) return false;
            saveId = session.session_id;
            nextSaves = [{
                session_id: saveId,
                name: session.name || saveId,
                message_count: Array.isArray(session.message_history) ? session.message_history.length : 0,
                updated_at: session.updated_at || '',
            }];
        }
        if (!saveId) throw new ApiError('存档列表已失效，未创建默认存档', { code: 'stale_session_ref' });

        candidateRef = sessionRefs.advance(project, saveId);
        if (!session) session = await fetchSession(candidateRef);
        if (!isCurrentSessionRef(candidateRef)) return false;

        state.characters = characters;
        state.saveList = nextSaves;
        if (!commitSessionState(session, candidateRef)) return false;
        window.location.hash = `#${project}`;
        const projectName = document.getElementById('project-name');
        if (projectName) projectName.textContent = project;
        const oldProjectSelect = document.getElementById('project-select');
        if (oldProjectSelect) oldProjectSelect.value = project;
        renderSaveListControls();
        return true;
    } catch (error) {
        rollbackSessionTransition(candidateRef);
        throw error;
    }
}

// ===== 存档管理 =====

async function loadSaveList(requestRef = captureSessionRef()) {
    const requestId = ++latestRequest.saves;
    const saves = await fetchSaveList(requestRef.project);
    if (
        !isCurrentSessionRef(requestRef)
        || state.currentProject !== requestRef.project
        || requestId !== latestRequest.saves
    ) return null;
    state.saveList = [...saves];
    renderSaveListControls();
    return state.saveList;
}

function renderSaveListControls() {
    // 兼容旧 select
    const oldSelect = document.getElementById('save-select');
    if (oldSelect) {
        oldSelect.innerHTML = '';
        if (state.saveList.length === 0) {
            const opt = document.createElement('option');
            opt.value = ''; opt.textContent = '— 无存档 —';
            oldSelect.appendChild(opt);
        } else {
            state.saveList.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.session_id;
                opt.textContent = s.name + (s.message_count > 0 ? ` (${s.message_count})` : '');
                oldSelect.appendChild(opt);
            });
            if (state.currentSave) oldSelect.value = state.currentSave;
        }
    }

    // 渲染新下拉面板 + 更新 badge
    renderSaveDropdown();
    const badge = document.getElementById('save-count');
    if (badge) badge.textContent = state.saveList.length;
}

function renderSaveDropdown() {
    const listEl = document.getElementById('save-list');
    if (!listEl) return;
    if (state.saveList.length === 0) {
        listEl.innerHTML = '<div class="dropdown-item" style="color:var(--text-dim);cursor:default">— 无存档 —</div>';
        return;
    }
    listEl.innerHTML = state.saveList.map(s => {
        const active = s.session_id === state.currentSave ? 'active' : '';
        return `<div class="dropdown-item ${active}" data-save="${escapeHtml(s.session_id)}">
            <span class="item-name">💾 ${escapeHtml(s.name)}</span>
            <span class="item-meta">${s.message_count || 0} 条</span>
        </div>`;
    }).join('');
    listEl.querySelectorAll('.dropdown-item[data-save]').forEach(item => {
        item.addEventListener('click', () => {
            const sid = item.dataset.save;
            hideAllDropdowns();
            switchSave(sid);
        });
    });
}

async function loadOrCreateCurrentSave() {
    return loadProjectContext(state.currentProject);
}

async function loadCurrentSession(requestRef = captureSessionRef()) {
    const session = await fetchSession(requestRef);
    if (!isCurrentSessionRef(requestRef)) return null;
    return commitSessionState(session, requestRef) ? session : null;
}

async function switchSave(newSaveId) {
    if (!newSaveId || newSaveId === state.currentSave) return;
    if (state.navigationBusy) {
        showToast('正在切换项目或存档，请稍候');
        return;
    }
    if (isTurnActiveForRef(committedSessionRef)) {
        showToast('当前存档正在生成，请先取消或等待完成');
        return;
    }
    const candidateRef = beginSessionTransition(state.currentProject, newSaveId);
    try {
        const session = await fetchSession(candidateRef);
        if (!isCurrentSessionRef(candidateRef)) return;
        if (!commitSessionState(session, candidateRef)) return;
        document.getElementById('suggestions').innerHTML = '';
        document.getElementById('thinking-panel').classList.add('hidden');
    } catch (error) {
        rollbackSessionTransition(candidateRef);
        showToast(`切换存档失败：${errorDetail(error)}`, 3000);
    }
}

async function createNewSave(name) {
    if (!name || state.navigationBusy || isTurnActiveForRef(committedSessionRef)) {
        showToast(state.navigationBusy ? '正在切换，请稍候' : '当前存档正在生成，请先取消或等待完成');
        return null;
    }
    let candidateRef = beginSessionTransition(state.currentProject, null);
    try {
        const session = await apiClient.post(API.sessionCreate, {
            project: state.currentProject,
            name,
        }, {
            schema: body => Boolean(sessionFromResult(body)) || '创建存档响应无效',
        });
        if (!isCurrentSessionRef(candidateRef)) return null;
        candidateRef = sessionRefs.advance(state.currentProject, session.session_id);
        const saves = await fetchSaveList(candidateRef.project);
        if (!isCurrentSessionRef(candidateRef)) return null;
        state.saveList = saves;
        if (!commitSessionState(session, candidateRef)) return null;
        document.getElementById('suggestions').innerHTML = '';
        return session;
    } catch (error) {
        rollbackSessionTransition(candidateRef);
        showToast(`创建失败：${errorDetail(error)}`, 3000);
        return null;
    }
}

async function renameCurrentSave(newName) {
    if (!state.currentSave) return;
    const requestRef = captureSessionRef();
    let body;
    try {
        body = await sessionWrite(API.sessionRename, 'POST', {
            project: requestRef.project,
            save: requestRef.save,
            new_name: newName,
        }, '重命名', { applyResult: false });
    } catch (error) {
        alert('重命名失败：' + error.message);
        return;
    }
    if (!isCurrentSessionRef(requestRef)) return;
    const session = sessionFromResult(body);
    if (!session || (session.project && session.project !== requestRef.project)) {
        alert('重命名失败：响应缺少有效存档');
        return;
    }
    const candidateRef = beginSessionTransition(requestRef.project, session.session_id);
    state.saveList = state.saveList.map(item => item.session_id === requestRef.save
        ? { ...item, session_id: session.session_id, name: session.name || newName }
        : item);
    if (!commitSessionState(session, candidateRef)) {
        rollbackSessionTransition(candidateRef);
        return;
    }
    renderSaveListControls();
}

async function deleteCurrentSave() {
    if (!state.currentSave) return;
    if (state.saveList.length <= 1) {
        alert('至少保留 1 个存档');
        return;
    }
    const requestRef = captureSessionRef();
    const s = state.saveList.find(x => x.session_id === state.currentSave);
    const name = s ? s.name : state.currentSave;
    if (!confirm(`确认删除存档「${name}」？删除后将移入回收区，可以恢复。`)) return;

    let body;
    try {
        body = await sessionWrite(API.sessionDelete, 'POST', {
            project: requestRef.project,
            save: requestRef.save,
        }, '删除存档', { applyResult: false });
        if (body && Object.prototype.hasOwnProperty.call(body, 'deleted') && body.deleted !== true) {
            throw new Error('服务端未删除该存档');
        }
    } catch (error) {
        alert('删除失败：' + error.message);
        return;
    }
    if (!isCurrentSessionRef(requestRef)) return;
    showToast('存档已移入回收区，可恢复');
    try {
        await loadProjectContext(requestRef.project);
    } catch (error) {
        showToast(`刷新剩余存档失败：${errorDetail(error)}`, 3000);
    }
}

async function exportCurrentSave() {
    if (!state.currentSave) return;
    const requestRef = captureSessionRef();
    const url = `${API.sessionExport}?project=${encodeURIComponent(requestRef.project)}&save=${encodeURIComponent(requestRef.save)}`;
    const data = await apiClient.get(url, {
        schema: body => Boolean(body && typeof body.json_str === 'string') || '导出响应无效',
    });
    if (!isCurrentSessionRef(requestRef)) return;
    const blob = new Blob([data.json_str], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${state.session.name || state.currentSave}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
}

async function importSave(file) {
    const validationError = TavernSecurity.validateImportFile(file);
    if (validationError) throw new Error(validationError);
    const text = await file.text();
    const requestRef = captureSessionRef();
    await apiClient.post(API.sessionImport, {
        project: requestRef.project,
        json_str: text,
        name: file.name.replace(/\.json$/i, ''),
    }, {
        schema: body => Boolean(sessionFromResult(body)) || '导入响应无效',
    });
    if (!isCurrentSessionRef(requestRef)) return false;
    await loadSaveList(requestRef);
    return true;
}

async function switchProject(newProject) {
    if (!newProject || newProject === state.currentProject) return;
    if (state.navigationBusy) {
        showToast('正在切换项目或存档，请稍候');
        return;
    }
    if (isTurnActiveForRef(committedSessionRef)) {
        showToast('当前存档正在生成，请先取消或等待完成');
        return;
    }
    try {
        const switched = await loadProjectContext(newProject);
        if (!switched) return;
        const btn = document.getElementById('project-btn');
        if (btn) {
            btn.classList.add('highlight');
            setTimeout(() => btn.classList.remove('highlight'), 700);
        }
        showToast(`已切换到「${newProject}」`);
        await renderProjectDropdown(captureSessionRef());
    } catch (error) {
        showToast(`切换项目失败：${errorDetail(error)}`, 3000);
    }
}

async function createNewProject(name) {
    if (state.navigationBusy || isTurnActiveForRef(committedSessionRef)) {
        throw new ApiError('当前正在切换或生成，请稍候', { code: 'ui_busy' });
    }
    const requestRef = captureSessionRef();
    const created = await apiClient.post(API.projects, { name }, {
        schema: body => Boolean(body && typeof body.name === 'string') || '创建项目响应无效',
    });
    if (!isCurrentSessionRef(requestRef)) return null;
    await loadProjects();
    await switchProject(created.name);
    return created;
}

// ===== 渲染 =====

function renderSession(session) {
    if (!session) return;
    const meta = session.scene_meta || {};
    const user = session.user_status || {};
    document.getElementById('meta-location').textContent =
        `📍 ${meta.location || '未知'} | ⏱️ ${meta.time || ''} ${meta.weather ? '/ ' + meta.weather : ''}`;
    document.getElementById('meta-quest').textContent = `🎯 ${meta.main_quest || ''}`;
    document.getElementById('meta-goal').textContent = `➡️ ${meta.next_goal || ''}`;
    document.getElementById('meta-user').textContent =
        `👤 ${user.name || ''} | 🆔 ${user.identity || ''} | 💪 ${user.condition || ''} | ✨ ${(user.abilities || []).join(', ')}`;
    renderCharacterPanel(session.characters_state || {});
}

function renderCharacterPanel(charactersState) {
    const list = document.getElementById('character-list');
    const ids = Object.keys(charactersState);
    if (ids.length === 0) { list.innerHTML = '<p class="empty">无角色数据</p>'; return; }
    list.innerHTML = ids.map(cid => {
        const c = charactersState[cid];
        const affinity = TavernSecurity.normalizeAffinity(c.affinity);
        const bar = renderAffinityBar(affinity);
        return `<div class="panel-char">
            <div class="name">${escapeHtml(c.name || cid)}</div>
            <div class="affinity-bar">${bar} ${affinity}%</div>
            ${c.mood ? `<div style="color:var(--text-dim);font-size:11px">心情: ${escapeHtml(c.mood)}</div>` : ''}
        </div>`;
    }).join('');
}

function renderAffinityBar(percent) {
    const filled = Math.round(TavernSecurity.normalizeAffinity(percent) / 10);
    return '█'.repeat(filled) + '░'.repeat(10 - filled);
}

function renderHistory(history) {
    const stream = document.getElementById('chat-stream');
    stream.innerHTML = '';
    history.forEach((msg) => {
        if (msg.role === 'user') appendUserMessage(msg.content, msg);
        else if (msg.role === 'assistant') appendAssistantMessage(msg.content, msg.thinking || '', msg);
    });
    // 重渲染历史时，给最新一条 AI 消息补上「📋 剧情记忆」折叠面板（已有 summaries 才显示）
    const lastAI = stream.querySelector('.msg.assistant:last-of-type');
    if (lastAI) renderSummaryPanel(lastAI, state.session);
    scrollToBottom();
}

// ===== 消息追加 =====

function appendUserMessage(text, msgData = null) {
    const stream = document.getElementById('chat-stream');
    const div = document.createElement('div');
    div.className = 'msg user';
    div.dataset.messageId = msgData && msgData.id ? msgData.id : '';
    div.innerHTML = `
        <input type="checkbox" class="msg-checkbox" ${msgData && msgData.in_prompt === false ? '' : 'checked'} title="勾选 = 进 prompt">
        <div class="msg-actions">
            <button class="msg-action-btn edit" title="编辑">✎</button>
            <button class="msg-action-btn regenerate" title="重新生成（从此条之后）">🔄</button>
            <button class="msg-action-btn pin ${msgData && msgData.pinned ? 'active' : ''}" title="📌 钉选 = 永久常驻 AI 记忆（截断不删）">📌</button>
            <button class="msg-action-btn delete" title="删除">🗑</button>
        </div>
        <div class="msg-role">你</div>
        <div class="content">${escapeHtml(text)}</div>`;
    stream.appendChild(div);
    bindMessageActions(div);
    scrollToBottom();
}

function appendAssistantMessage(content, thinking = '', msgData = null) {
    const stream = document.getElementById('chat-stream');
    const div = document.createElement('div');
    div.className = 'msg assistant';
    div.dataset.messageId = msgData && msgData.id ? msgData.id : '';
    // 给 assistant 节点分配唯一 id，便于 SSE/regenerate 精确锁定目标（兜底 :last-child 选择器）
    div.id = div.id || `msg-assistant-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    div.innerHTML = `
        <input type="checkbox" class="msg-checkbox" ${msgData && msgData.in_prompt === false ? '' : 'checked'} title="勾选 = 进 prompt">
        <div class="msg-actions">
            <button class="msg-action-btn edit" title="编辑">✎</button>
            <button class="msg-action-btn regenerate" title="重新生成">🔄</button>
            <button class="msg-action-btn pin ${msgData && msgData.pinned ? 'active' : ''}" title="📌 钉选 = 永久常驻 AI 记忆（截断不删）">📌</button>
            <button class="msg-action-btn delete" title="删除">🗑</button>
        </div>
        <div class="msg-role">AI · ${new Date().toLocaleTimeString()}</div>
        <div class="content"></div>`;
    stream.appendChild(div);
    const contentEl = div.querySelector('.content');
    contentEl.textContent = content;
    bindMessageActions(div);
    scrollToBottom();
    return contentEl;
}

function appendStreamChunk(targetEl, chunk) {
    if (targetEl) { targetEl.textContent += chunk; scrollToBottom(); }
}

function appendStreamThinking(chunk) {
    const tp = document.getElementById('thinking-panel');
    tp.classList.remove('hidden');
    document.getElementById('thinking-content').textContent += chunk;
}

// ===== 消息操作 =====

function bindMessageActions(msgEl) {
    const messageId = msgEl.dataset.messageId || '';
    if (!messageId) {
        console.warn('消息节点缺少 message UUID，已跳过绑定:', msgEl);
        return;
    }
    const messageRef = { message_id: messageId };
    const checkbox = msgEl.querySelector('.msg-checkbox');
    checkbox.addEventListener('change', async (e) => {
        try {
            await toggleMessageInPrompt(messageRef, e.target.checked);
            msgEl.classList.toggle('disabled-from-prompt', !e.target.checked);
        } catch (error) {
            e.target.checked = !e.target.checked;
            alert('更新失败：' + error.message);
        }
    });
    if (!checkbox.checked) msgEl.classList.add('disabled-from-prompt');

    msgEl.querySelector('.msg-action-btn.delete').addEventListener('click', async () => {
        if (!confirm('确认删除这条消息？')) return;
        try {
            await deleteMessage(messageRef);
            await reloadCurrentSession();
        } catch (error) { alert('删除失败：' + error.message); }
    });
    msgEl.querySelector('.msg-action-btn.edit').addEventListener('click', () => enterMessageEditMode(msgEl, messageRef));
    msgEl.querySelector('.msg-action-btn.regenerate').addEventListener('click', async () => {
        if (!confirm('重新生成？将回滚到这条消息之前重新调用 AI。')) return;
        try { await regenerateFrom(messageRef); }
        catch (error) { alert('重生成准备失败：' + error.message); }
    });
    const pinBtn = msgEl.querySelector('.msg-action-btn.pin');
    if (pinBtn) {
        pinBtn.addEventListener('click', async () => {
            if (pinBtn.disabled) return;  // 防重入
            pinBtn.disabled = true;
            const wasActive = pinBtn.classList.contains('active');
            // 乐观更新（若失败回滚）
            pinBtn.classList.toggle('active');
            try {
                const res = await togglePin(messageRef);
                if (!res || res.error) {
                    // 回滚
                    pinBtn.classList.toggle('active');
                    alert('钉选失败: ' + (res && res.error ? res.error : '网络错误'));
                    return;
                }
                // 常驻超 15 条软上限提示（非阻塞 toast）
                if (res.pinned_count > 15) {
                    showToast(`已钉选 ${res.pinned_count} 条常驻记忆，偏多，建议清理早期钉选`, 3000);
                }
            } finally {
                pinBtn.disabled = false;
            }
        });
    }
}

async function togglePin(messageRef) {
    const url = `${API.session}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    try {
        return await sessionWrite(url, 'PATCH', {
            action: 'toggle_pinned',
            ...messageRef,
        }, '钉选');
    } catch (e) { console.error('toggle_pin 失败', e); return { error: String(e) }; }
}

function enterMessageEditMode(msgEl, messageRef) {
    const contentEl = msgEl.querySelector('.content');
    const original = contentEl.textContent;
    msgEl.classList.add('editing');
    const textarea = document.createElement('textarea');
    textarea.value = original;
    contentEl.replaceWith(textarea);
    textarea.focus();

    let finished = false;   // 防重入：Ctrl+Enter 后 blur 不再触发第二次保存
    const finish = async (save) => {
        if (finished) return;
        finished = true;
        msgEl.classList.remove('editing');
        const newContent = textarea.value;
        textarea.replaceWith(contentEl);
        if (save && newContent !== original) {
            await editMessage(messageRef, newContent);
            contentEl.textContent = newContent;
            await reloadCurrentSession();
        } else { contentEl.textContent = original; }
    };
    textarea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); finish(true); }
        else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    textarea.addEventListener('blur', () => finish(true));
}

async function toggleMessageInPrompt(messageRef, inPrompt) {
    const url = `${API.session}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    return await sessionWrite(url, 'PATCH', {
        action: 'toggle_in_prompt',
        ...messageRef,
        in_prompt: inPrompt,
    }, '更新 Prompt 选择');
}

async function deleteMessage(messageRef) {
    const url = `${API.session}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    return await sessionWrite(url, 'PATCH', { action: 'delete', ...messageRef }, '删除消息');
}

async function editMessage(messageRef, newContent) {
    const url = `${API.session}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    return await sessionWrite(url, 'PATCH', {
        action: 'edit',
        ...messageRef,
        content: newContent,
    }, '编辑消息');
}

async function regenerateFrom(messageRef) {
    if (!messageRef || !messageRef.message_id) {
        throw new ApiError('重生成需要稳定 message_id', { code: 'message_id_required' });
    }
    if (state.navigationBusy || !canPerformAction('regenerate', state.activeTurn && state.activeTurn.status)) return;
    const requestRef = captureSessionRef();
    if (!sessionBelongsToRef(state.session, requestRef)) {
        throw new ApiError('当前存档尚未加载完成', { code: 'stale_session_ref' });
    }
    prepareProvisionalTurn(requestRef);
    await runTurnLifecycle(
        turnClient.regenerate({
            ...buildTurnPayload(requestRef),
            message_id: messageRef.message_id,
        }),
        requestRef,
        { failureLabel: '重生成' },
    );
}

async function reloadCurrentSession(requestRef = captureSessionRef()) {
    return loadCurrentSession(requestRef);
}

// ===== 角色卡渲染 =====

function renderParsedResponse(parsed) {
    const stream = document.getElementById('chat-stream');
    const msgs = stream.querySelectorAll('.msg.assistant');
    const lastAssistant = msgs[msgs.length - 1];
    if (!lastAssistant) return;
    const contentEl = lastAssistant.querySelector('.content');
    // C4：解析失败兜底 — 若 parsed 既无 characters 也无 scene_meta，保留流式累积的原文
    const isParsedEmpty = (!parsed.characters || parsed.characters.length === 0)
                       && (!parsed.scene_meta || !parsed.scene_meta.location)
                       && !parsed.narration;
    if (isParsedEmpty) {
        // 不清空 contentEl，让用户看到流式原文。追加一个降级提示。
        const warn = document.createElement('div');
        warn.className = 'voice-warning';
        warn.textContent = '⚠️ 本轮未解析出结构化内容，已保留原文';
        contentEl.appendChild(warn);
        document.getElementById('thinking-panel').classList.add('hidden');
        return;
    }
    contentEl.innerHTML = '';
    document.getElementById('thinking-panel').classList.add('hidden');

    if (parsed.warnings && parsed.warnings.length > 0) {
        const warnDiv = document.createElement('div');
        warnDiv.className = 'voice-warning';
        warnDiv.innerHTML = '⚠️ 检测到角色语气可能串味：' + parsed.warnings.map(w => escapeHtml(w)).join('；');
        contentEl.appendChild(warnDiv);
    }

    if (parsed.scene_meta && parsed.scene_meta.location) {
        const metaDiv = document.createElement('div');
        metaDiv.className = 'character-card';
        metaDiv.innerHTML = `
            <div><strong>📍 ${escapeHtml(parsed.scene_meta.location)}</strong> | <span style="color:var(--text-dim)">⏱️ ${escapeHtml(parsed.scene_meta.time_weather || '')}</span></div>
            ${parsed.scene_meta.main_quest ? `<div style="font-size:12px;color:var(--text-dim);margin-top:4px">🎯 ${escapeHtml(parsed.scene_meta.main_quest)}</div>` : ''}
            ${parsed.scene_meta.current_scene ? `<div style="font-size:12px;color:var(--text-dim)">📌 ${escapeHtml(parsed.scene_meta.current_scene)}</div>` : ''}
            ${parsed.scene_meta.next_goal ? `<div style="font-size:12px;color:var(--text-dim)">➡️ ${escapeHtml(parsed.scene_meta.next_goal)}</div>` : ''}`;
        contentEl.appendChild(metaDiv);
    }

    (parsed.characters || []).forEach(c => {
        const card = document.createElement('div');
        card.className = 'character-card';
        const affinity = TavernSecurity.normalizeAffinity(c.affinity);
        card.innerHTML = `
            <div class="char-header">
                <span class="char-name">🎭 ${escapeHtml(c.name)}</span>
                <span class="char-affinity">${renderAffinityBar(affinity)} ${affinity}%</span>
            </div>
            ${c.inner_thought ? `<div class="char-row"><strong>💭 内心:</strong> ${escapeHtml(c.inner_thought)}</div>` : ''}
            ${c.outfit ? `<div class="char-row"><strong>👗 穿着:</strong> ${escapeHtml(c.outfit)}</div>` : ''}
            ${c.posture ? `<div class="char-row"><strong>🧍 姿势:</strong> ${escapeHtml(c.posture)}</div>` : ''}
            <div class="char-dialogue">💬 "${escapeHtml(c.dialogue)}"
                ${c.expected_effect ? `<div class="effect">(预期影响: ${escapeHtml(c.expected_effect)})</div>` : ''}
            </div>`;
        contentEl.appendChild(card);
    });

    if (parsed.narration) {
        const narDiv = document.createElement('div');
        narDiv.className = 'msg-narration';
        narDiv.textContent = parsed.narration;
        contentEl.appendChild(narDiv);
    }

    if (parsed.suggestions && parsed.suggestions.length > 0) renderSuggestions(parsed.suggestions);
    else document.getElementById('suggestions').innerHTML = '';

    renderSummaryPanel(lastAssistant, state.session);
    scrollToBottom();
}

function renderSummaryPanel(lastAssistant, sess) {
    // 在该 AI 消息后挂可折叠「📋 剧情记忆」面板，展示最新一段短期总结或失败提示
    if (!lastAssistant || !lastAssistant.parentNode) return;
    const summaries = (sess && sess.summaries) || [];
    const err = (sess && sess.summary_error) || '';
    if (!summaries.length && !err) return;

    // 移除已存在的旧面板（折叠面板位于 AI 消息后的兄弟节点）
    const existing = lastAssistant.nextElementSibling;
    if (existing && existing.classList && existing.classList.contains('summary-panel')) {
        existing.remove();
    }

    const panel = document.createElement('div');
    panel.className = 'summary-panel collapsed';
    lastAssistant.parentNode.insertBefore(panel, lastAssistant.nextSibling);

    const header = document.createElement('div');
    header.className = 'summary-panel-header';
    const toggle = document.createElement('span');
    toggle.className = 'summary-toggle';
    toggle.textContent = '▶';
    header.appendChild(toggle);
    const title = document.createElement('span');
    title.style.cssText = 'flex:1;margin-left:8px;font-size:12px;color:var(--text-dim)';
    title.textContent = err ? '📋 剧情记忆（⚠️ 本轮总结失败）' : '📋 剧情记忆（最近一段梗概）';
    header.appendChild(title);
    if (summaries.length) {
        const newest = summaries[summaries.length - 1];
        const index = summaries.length - 1;
        const btnGroup = document.createElement('span');
        btnGroup.style.cssText = 'display:flex;gap:4px';

        const editBtn = document.createElement('button');
        editBtn.className = 'summary-edit-btn';
        editBtn.title = '编辑这段总结';
        editBtn.textContent = '✏️';
        editBtn.addEventListener('click', () => enterSummaryEditMode(panel, newest, index));
        btnGroup.appendChild(editBtn);

        const regenBtn = document.createElement('button');
        regenBtn.className = 'summary-regen-btn';
        regenBtn.title = '重新生成最新这段总结';
        regenBtn.textContent = '🔄重生成';
        regenBtn.addEventListener('click', () => regenerateLastSummary(panel, sess));
        btnGroup.appendChild(regenBtn);

        header.appendChild(btnGroup);
    }
    header.addEventListener('click', (e) => {
        if (e.target.classList.contains('summary-regen-btn')) return;
        if (e.target.classList.contains('summary-edit-btn')) return;
        panel.classList.toggle('collapsed');
        toggle.textContent = panel.classList.contains('collapsed') ? '▶' : '▼';
    });
    panel.appendChild(header);

    const body = document.createElement('div');
    body.className = 'summary-panel-body';
    if (err) {
        const errDiv = document.createElement('div');
        errDiv.className = 'summary-error';
        errDiv.textContent = '⚠️ ' + err;
        body.appendChild(errDiv);
    }
    if (summaries.length) {
        const newest = summaries[summaries.length - 1];
        if (newest.failed || newest.error) {
            // 失败占位段：明确提示该段总结生成失败、原文已落 trim 快照，不要拿占位文案当正文误导
            const failDiv = document.createElement('div');
            failDiv.className = 'summary-error';
            failDiv.textContent = '⚠️ 该段总结生成失败，原文已存入trim快照（可点🔄重生成重试）';
            body.appendChild(failDiv);
            if (newest.created_at) {
                const t = document.createElement('div');
                t.style.cssText = 'margin-top:6px;font-size:11px;color:var(--text-dim)';
                t.textContent = '生成于 ' + newest.created_at.slice(0, 16).replace('T', ' ');
                body.appendChild(t);
            }
        } else {
            const main = document.createElement('div');
            main.style.cssText = 'margin-bottom:6px';
            main.textContent = '前情提要：' + (newest.text || '（空）');
            body.appendChild(main);
            if (newest.time) body.appendChild(makeSummaryLine('🕐 时间线', newest.time));
            if (newest.facts && newest.facts.length) {
                const f = document.createElement('div'); f.style.cssText = 'margin-top:4px';
                f.textContent = '关键事件：';
                newest.facts.forEach(x => { const li = document.createElement('div'); li.style.cssText = 'padding-left:12px;color:var(--text-dim)'; li.textContent = '· ' + x; f.appendChild(li); });
                body.appendChild(f);
            }
            if (newest.relations && newest.relations.length) {
                const f = document.createElement('div'); f.style.cssText = 'margin-top:4px';
                f.textContent = '角色关系：';
                newest.relations.forEach(x => { const li = document.createElement('div'); li.style.cssText = 'padding-left:12px;color:var(--text-dim)'; li.textContent = '· ' + x; f.appendChild(li); });
                body.appendChild(f);
            }
            if (newest.created_at) {
                const t = document.createElement('div');
                t.style.cssText = 'margin-top:6px;font-size:11px;color:var(--text-dim)';
                t.textContent = '生成于 ' + newest.created_at.slice(0, 16).replace('T', ' ');
                body.appendChild(t);
            }
        }
    }
    panel.appendChild(body);
}

function makeSummaryLine(label, val) {
    const d = document.createElement('div');
    d.style.cssText = 'margin-top:2px;color:var(--text-dim)';
    d.textContent = label + '：' + val;
    return d;
}

function escapeHtml(str) {
    return TavernSecurity.escapeHtml(str);
}

function enterSummaryEditMode(panel, summary, index) {
    const body = panel.querySelector('.summary-panel-body');
    if (!body) return;
    panel.classList.remove('collapsed');

    body.innerHTML = `
        <div style="margin-bottom:6px">
            <label style="font-size:11px;color:var(--text-dim);display:block;margin-bottom:2px">前情提要</label>
            <textarea class="summary-edit-input" data-field="text" style="width:100%;min-height:50px">${escapeHtml(summary.text || '')}</textarea>
        </div>
        <div style="margin-bottom:6px">
            <label style="font-size:11px;color:var(--text-dim);display:block;margin-bottom:2px">时间线</label>
            <input class="summary-edit-input" data-field="time" style="width:100%" value="${escapeHtml(summary.time || '')}" />
        </div>
        <div style="margin-bottom:6px">
            <label style="font-size:11px;color:var(--text-dim);display:block;margin-bottom:2px">关键事件（每行一条）</label>
            <textarea class="summary-edit-input" data-field="facts" style="width:100%;min-height:40px">${escapeHtml((summary.facts || []).join('\n'))}</textarea>
        </div>
        <div style="margin-bottom:6px">
            <label style="font-size:11px;color:var(--text-dim);display:block;margin-bottom:2px">角色关系（每行一条）</label>
            <textarea class="summary-edit-input" data-field="relations" style="width:100%;min-height:40px">${escapeHtml((summary.relations || []).join('\n'))}</textarea>
        </div>
        <div style="display:flex;gap:6px;margin-top:8px">
            <button class="summary-save-btn">💾 保存</button>
            <button class="summary-cancel-btn">取消</button>
        </div>
    `;

    body.querySelector('.summary-save-btn').addEventListener('click', () => saveEdit(panel, index, body));
    body.querySelector('.summary-cancel-btn').addEventListener('click', () => {
        const lastAI = document.querySelector('#chat-stream .msg.assistant:last-of-type');
        if (lastAI && state.session) renderSummaryPanel(lastAI, state.session);
    });
}

async function saveEdit(panel, index, body) {
    const inputs = Array.from(body.querySelectorAll('.summary-edit-input'));
    const text = inputs.find(el => el.dataset.field === 'text')?.value || '';
    const time = inputs.find(el => el.dataset.field === 'time')?.value || '';
    const factsStr = inputs.find(el => el.dataset.field === 'facts')?.value || '';
    const relsStr = inputs.find(el => el.dataset.field === 'relations')?.value || '';
    const facts = factsStr.split('\n').map(s => s.trim()).filter(Boolean);
    const relations = relsStr.split('\n').map(s => s.trim()).filter(Boolean);

    try {
        await sessionWrite('/api/session/summary', 'PATCH', {
            project: state.currentProject,
            save: state.currentSave,
            index: index,
            text: text || '',
            time: time || '',
            facts: facts || [],
            relations: relations || [],
        }, '保存总结');
        await reloadCurrentSession();
        const lastAI = document.querySelector('#chat-stream .msg.assistant:last-of-type');
        if (lastAI) renderSummaryPanel(lastAI, state.session);
    } catch (e) {
        alert('保存失败: ' + e.message);
    }
}

async function regenerateLastSummary(panel, sess) {
    if (!confirm('重新生成最新这段剧情总结？（会调一次本地模型）')) return;
    const btn = panel.querySelector('.summary-regen-btn');
    if (btn) btn.disabled = true;
    try {
        const res = await sessionWrite(API.summaryRegen, 'POST', {
            project: state.currentProject,
            save: state.currentSave,
        }, '重生成总结');
        if (res.error) { alert('重新生成失败: ' + res.error); if (btn) btn.disabled = false; return; }
        // sessionWrite 已原子提交并重绘完整 Session。
        const lastAI = document.querySelector('#chat-stream .msg.assistant:last-of-type');
        if (lastAI) renderSummaryPanel(lastAI, state.session);
    } catch (e) {
        alert('请求失败: ' + e.message);
        if (btn) btn.disabled = false;
    }
}

function renderSuggestions(suggestions) {
    const container = document.getElementById('suggestions');
    container.innerHTML = '';
    suggestions.forEach(s => {
        const btn = document.createElement('button');
        btn.className = 'suggestion-btn';
        btn.textContent = s;
        btn.title = s;
        // C4：点击建议直接填入并发送，无需再点发送按钮
        btn.onclick = () => { document.getElementById('user-input').value = s; sendMessage(); };
        container.appendChild(btn);
    });
}

// ===== 发送消息 =====

const ACTIVE_TURN_STORAGE_KEY = 'local-tavern.active-turn.v1';

function readPersistedTurnPointer() {
    try {
        const value = JSON.parse(sessionStorage.getItem(ACTIVE_TURN_STORAGE_KEY) || 'null');
        if (!value || typeof value.turnId !== 'string') return null;
        if (typeof value.project !== 'string' || typeof value.save !== 'string') return null;
        return {
            turnId: value.turnId,
            project: value.project,
            save: value.save,
            lastEventId: Number.isSafeInteger(value.lastEventId) && value.lastEventId >= 0
                ? value.lastEventId
                : 0,
        };
    } catch (_error) {
        return null;
    }
}

function persistActiveTurn(turn = state.activeTurn) {
    if (!turn || !turn.turnId || !turn.ref) return;
    try {
        sessionStorage.setItem(ACTIVE_TURN_STORAGE_KEY, JSON.stringify({
            turnId: turn.turnId,
            project: turn.ref.project,
            save: turn.ref.save,
            lastEventId: turn.lastEventId || 0,
        }));
    } catch (_error) {}
}

function clearPersistedTurn() {
    try { sessionStorage.removeItem(ACTIVE_TURN_STORAGE_KEY); } catch (_error) {}
}

function setTurnUiState(active, cancelling = false) {
    state.isStreaming = active;
    const sendBtn = document.getElementById('send-btn');
    const cancelBtn = document.getElementById('send-cancel-btn');
    if (sendBtn) {
        sendBtn.disabled = active;
        sendBtn.classList.toggle('hidden', active);
        sendBtn.setAttribute('aria-disabled', String(active));
    }
    if (cancelBtn) {
        cancelBtn.disabled = !active || cancelling;
        cancelBtn.classList.toggle('hidden', !active);
        cancelBtn.setAttribute('aria-busy', String(cancelling));
        cancelBtn.textContent = cancelling ? '正在取消…' : '⏹ 取消';
    }

    const selectors = [
        '#project-btn', '#tab-saves', '#model-select', '#reset-btn',
        '#save-new-inline', '#save-rename-inline', '#save-delete-inline',
        '#save-import-inline', '#history-btn',
        '.msg-action-btn', '.msg-checkbox', '.summary-save-btn', '.summary-regen-btn',
        '.history-restore', '.ce-save', '.ce-delete',
    ];
    document.querySelectorAll(selectors.join(',')).forEach(element => {
        if ('disabled' in element) element.disabled = active;
        element.setAttribute('aria-disabled', String(active));
    });
}

function makeProvisionalTurn(ref) {
    return {
        turnId: null,
        ref,
        status: 'pending',
        lastEventId: 0,
        content: '',
        thinking: '',
        parsed: null,
        error: null,
        terminal: false,
        terminalCount: 0,
        provisional: true,
        cancelling: false,
    };
}

function renderActiveTurnBuffer(turn = state.activeTurn) {
    if (!turn) return;
    if (turn.targetEl && turn.targetEl.isConnected) {
        turn.targetEl.textContent = turn.content || (turn.terminal ? '' : '（生成中…）');
    }
    const thinking = document.getElementById('thinking-content');
    const panel = document.getElementById('thinking-panel');
    if (thinking) thinking.textContent = turn.thinking || '';
    if (panel) panel.classList.toggle('hidden', !turn.thinking);
}

function installActiveTurn(turn, ref, options = {}) {
    const base = createTurnState(turn, ref);
    activeController = new AbortController();
    state.activeTurn = {
        ...base,
        lastEventId: options.lastEventId === undefined ? base.lastEventId : options.lastEventId,
        content: options.content === undefined ? base.content : options.content,
        thinking: options.thinking === undefined ? base.thinking : options.thinking,
        controller: activeController,
        targetEl: options.targetEl || null,
        cancelling: false,
    };
    persistActiveTurn();
    setTurnUiState(true, false);
    return state.activeTurn;
}

function applyTurnEvent(event) {
    const active = state.activeTurn;
    if (!active || active.terminal) return false;
    const reduced = reduceTurnEvent(active, event);
    if (!reduced.accepted) return false;
    if (reduced.gap) {
        throw new ApiError(`turn 事件不连续：期望 ${active.lastEventId + 1}，收到 ${event.id}`, {
            code: 'turn_event_gap', payload: event,
        });
    }
    state.activeTurn = reduced.state;
    persistActiveTurn();
    if (event.type === 'content' || event.type === 'thinking' || event.type === 'started') {
        renderActiveTurnBuffer();
    } else if (event.type === 'parsed') {
        const applied = applySessionResult(event.session, active.ref);
        if (applied && isCurrentSessionRef(active.ref)) renderParsedResponse(event.parsed || {});
        setTurnUiState(true, state.activeTurn.cancelling);
    }
    return true;
}

function updateActiveTurnFromMeta(meta) {
    const active = state.activeTurn;
    if (!active || active.turnId !== meta.turn_id) return false;
    const terminal = TERMINAL_STATUSES.includes(meta.status);
    state.activeTurn = {
        ...active,
        status: meta.status,
        content: terminal && typeof meta.content === 'string' ? meta.content : active.content,
        thinking: terminal && typeof meta.thinking === 'string' ? meta.thinking : active.thinking,
        error: meta.error || null,
        terminal,
        terminalCount: terminal ? Math.max(1, active.terminalCount) : active.terminalCount,
    };
    persistActiveTurn();
    renderActiveTurnBuffer();
    return true;
}

function waitForReconnect(delayMs) {
    return new Promise(resolve => setTimeout(resolve, delayMs));
}

async function consumeActiveTurn(turnId) {
    let reconnects = 0;
    let terminalMeta = null;
    while (state.activeTurn && state.activeTurn.turnId === turnId && !state.activeTurn.terminal) {
        const active = state.activeTurn;
        try {
            for await (const event of turnClient.events(turnId, {
                after: active.lastEventId,
                signal: active.controller.signal,
            })) {
                if (!state.activeTurn || state.activeTurn.turnId !== turnId) return;
                applyTurnEvent(event);
                reconnects = 0;
                if (state.activeTurn.terminal) break;
            }
        } catch (error) {
            if (!state.activeTurn || state.activeTurn.turnId !== turnId) return;
            if (state.activeTurn.terminal) break;
            if (error instanceof ApiError && error.isAbort && state.activeTurn.cancelling) break;
            console.warn('turn 事件流中断，准备按 cursor 重连', error);
        }

        if (!state.activeTurn || state.activeTurn.turnId !== turnId || state.activeTurn.terminal) break;
        try {
            const meta = await turnClient.get(turnId);
            if (TERMINAL_STATUSES.includes(meta.status)) terminalMeta = meta;
            else updateActiveTurnFromMeta(meta);
        } catch (error) {
            console.warn('查询 turn 状态失败', error);
        }
        reconnects += 1;
        if (reconnects > 5) {
            if (terminalMeta) {
                updateActiveTurnFromMeta(terminalMeta);
                break;
            }
            showToast('与生成流的连接已中断；可点击取消重试，或刷新页面恢复持久 turn', 5000);
            return;
        }
        await waitForReconnect(Math.min(1000, reconnects * 200));
    }
}

function terminalMessage(turn) {
    if (turn.status === 'completed') return '生成完成';
    if (turn.status === 'cancelled') return '生成已取消，已保存现有内容';
    const detail = turn.error && (turn.error.message || turn.error.code);
    return `生成失败：${detail || '未知错误'}`;
}

async function finalizeActiveTurn(turnId) {
    const active = state.activeTurn;
    if (!active || active.turnId !== turnId || !active.terminal) return false;
    try {
        await reloadCurrentSession(active.ref);
        if (active.parsed && isCurrentSessionRef(active.ref)) {
            renderParsedResponse(active.parsed.parsed || {});
        }
    } catch (error) {
        showToast(`turn 已终止，但刷新存档失败：${errorDetail(error)}`, 4000);
    }
    showToast(terminalMessage(active), active.status === 'completed' ? 1800 : 3500);
    if (active.controller && !active.controller.signal.aborted) active.controller.abort();
    clearPersistedTurn();
    state.activeTurn = null;
    activeController = null;
    setTurnUiState(false, false);
    return true;
}

async function resumePersistedTurn(pointer = readPersistedTurnPointer()) {
    let turnId = pointer && pointer.turnId;
    if (!turnId && state.session) {
        const pending = [...(state.session.message_history || [])].reverse().find(message =>
            message && message.turn_id && ['pending', 'streaming'].includes(message.status));
        if (pending) turnId = pending.turn_id;
    }
    if (!turnId) return false;
    const ref = captureSessionRef();
    if (pointer && (pointer.project !== ref.project || pointer.save !== ref.save)) return false;
    try {
        const meta = await turnClient.get(turnId);
        if (meta.project !== ref.project || meta.save !== ref.save) {
            clearPersistedTurn();
            return false;
        }
        const active = installActiveTurn(meta, ref, {
            lastEventId: meta.last_event_id || (pointer && pointer.lastEventId) || 0,
        });
        if (!active.terminal) {
            const targetEl = appendAssistantMessage(active.content || '（恢复生成状态…）');
            state.activeTurn = { ...state.activeTurn, targetEl };
            renderActiveTurnBuffer();
            await consumeActiveTurn(turnId);
        }
        if (state.activeTurn && state.activeTurn.terminal) await finalizeActiveTurn(turnId);
        return true;
    } catch (error) {
        clearPersistedTurn();
        console.warn('恢复持久 turn 失败', error);
        return false;
    }
}

async function cancelActiveTurn() {
    const active = state.activeTurn;
    if (!active || !active.turnId || active.terminal || active.cancelling) return;
    state.activeTurn = { ...active, cancelling: true };
    setTurnUiState(true, true);
    try {
        const meta = await turnClient.cancel(active.turnId);
        updateActiveTurnFromMeta(meta);
        if (state.activeTurn && state.activeTurn.terminal && state.activeTurn.controller) {
            state.activeTurn.controller.abort();
        }
    } catch (error) {
        if (state.activeTurn && state.activeTurn.turnId === active.turnId) {
            state.activeTurn = { ...state.activeTurn, cancelling: false };
            setTurnUiState(true, false);
        }
        showToast(`取消失败：${errorDetail(error)}`, 3500);
    }
}

function buildTurnPayload(requestRef, userInput = undefined) {
    const payload = {
        project: requestRef.project,
        save: requestRef.save,
        model: state.session.current_model || document.getElementById('model-select').value || null,
        expected_revision: currentRevision(requestRef),
        temperature: state.modelParams.temperature,
        top_p: state.modelParams.top_p,
        top_k: state.modelParams.top_k,
        num_predict: state.modelParams.num_predict,
        think: state.modelParams.think,
    };
    if (userInput !== undefined) payload.user_input = userInput;
    return payload;
}

function prepareProvisionalTurn(requestRef) {
    const thinking = document.getElementById('thinking-content');
    if (thinking) thinking.textContent = '';
    state.activeTurn = makeProvisionalTurn(requestRef);
    setTurnUiState(true, false);
}

async function runTurnLifecycle(turnRequest, requestRef, options = {}) {
    const input = document.getElementById('user-input');
    const failureLabel = options.failureLabel || '发送';
    try {
        const turn = await turnRequest;
        if (!isCurrentSessionRef(requestRef)) {
            throw new ApiError('发送期间存档引用已失效', { code: 'stale_session_ref' });
        }
        installActiveTurn(turn, requestRef);
        if (options.clearInput && input) input.value = '';
        try { await reloadCurrentSession(requestRef); } catch (error) {
            console.warn('同步 pending user 失败，继续读取持久 turn', error);
        }
        if (!state.activeTurn || state.activeTurn.turnId !== turn.turn_id) return;
        const targetEl = appendAssistantMessage('（生成中…）');
        state.activeTurn = { ...state.activeTurn, targetEl };
        const consumePromise = consumeActiveTurn(turn.turn_id);
        state.activeTurn = { ...state.activeTurn, consumePromise };
        await consumePromise;
        await finalizeActiveTurn(turn.turn_id);
    } catch (error) {
        if (error instanceof ApiError && error.code === 'active_turn_conflict') {
            const detail = error.payload && (error.payload.error || error.payload.detail);
            if (detail && detail.turn_id) {
                clearPersistedTurn();
                try {
                    const meta = await turnClient.get(detail.turn_id);
                    installActiveTurn(meta, requestRef);
                    const targetEl = appendAssistantMessage(meta.content || '（恢复生成状态…）');
                    state.activeTurn = { ...state.activeTurn, targetEl };
                    await consumeActiveTurn(meta.turn_id);
                    await finalizeActiveTurn(meta.turn_id);
                    return;
                } catch (resumeError) {
                    console.warn('附着既有 turn 失败', resumeError);
                }
            }
        }
        if (state.activeTurn && !state.activeTurn.provisional) {
            setTurnUiState(true, Boolean(state.activeTurn.cancelling));
            showToast(`生成流处理异常：${errorDetail(error)}；持久 turn 仍可恢复`, 5000);
            return;
        }
        state.activeTurn = null;
        activeController = null;
        setTurnUiState(false, false);
        showToast(`${failureLabel}失败：${errorDetail(error)}`, 4000);
    }
}

async function sendMessage(text = null) {
    const input = document.getElementById('user-input');
    const userText = text !== null ? String(text).trim() : input.value.trim();
    if (!userText || state.navigationBusy || !canPerformAction('send', state.activeTurn && state.activeTurn.status)) return;
    const requestRef = captureSessionRef();
    if (!sessionBelongsToRef(state.session, requestRef)) {
        showToast('当前存档尚未加载完成');
        return;
    }
    prepareProvisionalTurn(requestRef);
    await runTurnLifecycle(
        turnClient.create(buildTurnPayload(requestRef, userText)),
        requestRef,
        { clearInput: true, failureLabel: '发送' },
    );
}

// ===== Modal 框架 =====

function showModal({ title, body, footer = null }) {
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').innerHTML = '';
    if (typeof body === 'string') document.getElementById('modal-body').innerHTML = body;
    else if (body instanceof Node) document.getElementById('modal-body').appendChild(body);
    const footerEl = document.getElementById('modal-footer');
    const confirmBtn = document.getElementById('modal-confirm');
    confirmBtn.onclick = null;
    if (footer) {
        footerEl.classList.remove('hidden');
        document.getElementById('modal-cancel').textContent = footer.cancelText || '取消';
        confirmBtn.textContent = footer.confirmText || '确定';
        footerEl.dataset.handler = '1';
        if (footer.onConfirm) confirmBtn.onclick = () => { footer.onConfirm(); hideModal(); };
    } else {
        footerEl.classList.add('hidden');
        footerEl.dataset.handler = '';
    }
    document.getElementById('modal-backdrop').classList.remove('hidden');
}

function hideModal() {
    document.getElementById('modal-backdrop').classList.add('hidden');
    document.getElementById('modal-body').innerHTML = '';   // 断开所有子节点引用，防闭包泄漏
    document.getElementById('modal-title').textContent = '';
    const confirmBtn = document.getElementById('modal-confirm');
    if (confirmBtn) confirmBtn.onclick = null;
}

// ===== 角色 / 世界书 / 用户档案 编辑器 =====

function cardInput(label, id, value = '', type = 'text', opts = {}) {
    const ph = opts.placeholder || '';
    const rows = opts.rows || 3;
    if (type === 'textarea') {
        return `<div class="card-field"><label>${label}</label><textarea id="${id}" rows="${rows}" placeholder="${ph}">${escapeHtml(value)}</textarea></div>`;
    }
    if (type === 'checkbox') {
        return `<div class="card-field inline"><label><input type="checkbox" id="${id}" ${value ? 'checked' : ''}> ${label}</label></div>`;
    }
    if (type === 'number') {
        return `<div class="card-field"><label>${label}</label><input type="number" id="${id}" value="${escapeHtml(value)}" min="${opts.min||0}" max="${opts.max||100}"></div>`;
    }
    return `<div class="card-field"><label>${label}</label><input type="text" id="${id}" value="${escapeHtml(value)}" placeholder="${ph}"></div>`;
}

function collectTextareaLines(id) {
    const el = document.getElementById(id);
    if (!el) return [];
    return el.value.split('\n').map(s => s.trim()).filter(Boolean);
}

// ============================================================
// 通用卡片编辑器引擎 (v3) — 由 openCharactersEditor/openWorldbookEditor/openUserEditor 调用
// ============================================================
// config 字段：
//   title:       modal 标题
//   listApi:     列表 API URL（GET），返回 {characters|entries|data:[]}
//   listKey:     列表数据在响应里的字段名（'characters' / 'entries' / 'data'）
//   saveApi:     (id) => 保存 API URL（PUT），单条 user 时传 null
//   deleteApi:   (id) => 删除 API URL（DELETE），不提供则隐藏删除按钮
//   allowNew:    bool — 是否允许多条（user=false；角色/世界书=true）
//   idField:     'id'（角色/世界书）/ null（用户）
//   idLabel:     ID 字段标签
//   buildGroups: (item) => groups[]，定义该类型的字段
//   postSave:    可选，保存成功后的额外处理
//   postDelete:  可选，删除成功后的额外处理
//   project:     编辑器打开时的项目 ID
//   sessionRef:  角色/用户删除绑定的 SessionRef
//   returnsSession: 删除成功时是否接受 active session
//   prefix:      CSS class 前缀（cf/wf/uf）— 防止冲突
// ============================================================

async function createCardEditor(config) {
    const editorRef = config.sessionRef || captureSessionRef();
    config.sessionRef = editorRef;
    if (!isCurrentSessionRef(editorRef)) return;
    // 1. 加载数据
    let items = [];
    let extraData = null;
    try {
        if (config.listApi) {
            const json = await apiClient.get(config.listApi, {
                schema: body => Array.isArray(body && (body[config.listKey] || body.data)) || '编辑器列表响应无效',
            });
            items = json[config.listKey] || json.data;
        }
        if (config.extraApi) {
            extraData = await apiClient.get(config.extraApi, {
                schema: body => Boolean(body && typeof body === 'object' && !Array.isArray(body)) || '编辑器档案响应无效',
            });
        }
    } catch (error) {
        showToast(`加载编辑器失败：${errorDetail(error)}`, 3500);
        return;
    }
    if (!isCurrentSessionRef(editorRef)) return;

    // 2. 构造 modal HTML
    const body = document.createElement('div');
    body.className = `card-editor card-editor-${config.prefix}`;
    body.innerHTML = `
        <div class="cards-panel-inner">
            ${config.allowNew ? '<div class="cards-list" id="ce-list"></div>' : ''}
            <div class="cards-form" id="ce-form"></div>
        </div>`;
    showModal({ title: config.title, body });

    const listEl = body.querySelector('#ce-list');
    const formEl = body.querySelector('#ce-form');
    let currentItem = null;

    // ===== 列表渲染（仅 allowNew）=====
    function renderList() {
        if (!listEl) return;
        listEl.innerHTML = `
            <button class="modal-btn new-card-btn" id="ce-new">＋ 新建</button>
            ${items.map(it => `
                <div class="card-row ${currentItem && currentItem[config.idField] === it[config.idField] ? 'active' : ''}" data-id="${escapeHtml(it[config.idField])}">
                    <span class="card-row-name">${escapeHtml(it.name || it[config.idField] || it.id || '')}</span>
                </div>`).join('')}`;
        listEl.querySelector('#ce-new').addEventListener('click', () => {
            currentItem = null; renderList(); renderForm();
        });
        listEl.querySelectorAll('.card-row').forEach(row => {
            row.addEventListener('click', () => {
                currentItem = items.find(it => it[config.idField] === row.dataset.id) || null;
                renderList(); renderForm();
            });
        });
    }

    // ===== 字段工具 =====
    function getVal(obj, path, fieldDef) {
        if (fieldDef.type === 'custom') return fieldDef.value || '';
        if (fieldDef.array) return (path.split('.').reduce((o,k) => (o||{})[k], obj) || []).join('\n');
        if (fieldDef.type === 'checkbox') return path.split('.').reduce((o,k) => (o||{})[k], obj) !== false;
        return path.split('.').reduce((o,k) => (o!=null?o[k]:''), obj) ?? '';
    }

    function setVal(obj, path, val, fieldDef) {
        if (fieldDef.array) val = val.split('\n').map(s => s.trim()).filter(Boolean);
        if (fieldDef.type === 'number') val = Number(val) || 0;
        if (fieldDef.type === 'checkbox') val = Boolean(val);
        const keys = path.split('.');
        const last = keys.pop();
        let cur = obj;
        for (const k of keys) { if (!cur[k] || typeof cur[k] !== 'object') cur[k] = {}; cur = cur[k]; }
        cur[last] = val;
    }

    // ===== 字段行 =====
    function makeFieldRow(fd) {
        const row = document.createElement('div');
        row.className = 'fld-row';
        row.dataset.key = fd.key;
        row.dataset.type = fd.type;
        if (fd.array) row.dataset.array = '1';
        if (fd.required) row.dataset.required = '1';

        const handle = document.createElement('span');
        handle.className = 'fld-handle';
        handle.title = '拖拽排序'; handle.textContent = '⠿'; handle.draggable = true;
        row.appendChild(handle);

        const body = document.createElement('div'); body.className = 'fld-body';

        if (fd.type === 'custom') {
            const keyWrap = document.createElement('div'); keyWrap.className = 'fld-cell grow';
            const keyLbl = document.createElement('label'); keyLbl.textContent = '字段名';
            const keyInp = document.createElement('input'); keyInp.type = 'text'; keyInp.className = 'ce-field-key';
            keyInp.placeholder = '例：所属势力'; keyInp.value = fd.key || '';
            keyWrap.appendChild(keyLbl); keyWrap.appendChild(keyInp); body.appendChild(keyWrap);
            const valWrap = document.createElement('div'); valWrap.className = 'fld-cell grow';
            const valLbl = document.createElement('label'); valLbl.textContent = '值';
            const valInp = document.createElement('input'); valInp.type = 'text'; valInp.className = 'ce-field-val';
            valInp.placeholder = '例：青龙商会'; valInp.value = fd.value || '';
            valWrap.appendChild(valLbl); valWrap.appendChild(valInp); body.appendChild(valWrap);
        } else if (fd.type === 'checkbox') {
            const lbl = document.createElement('label');
            lbl.style.cssText = 'font-size:12px;color:var(--text);cursor:pointer;display:flex;align-items:center;gap:6px';
            const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'ce-field-val';
            cb.checked = getVal(currentItem || {}, fd.key, fd);
            lbl.appendChild(cb); lbl.appendChild(document.createTextNode(fd.checkboxLabel || fd.label));
            body.appendChild(lbl);
        } else if (fd.type === 'textarea') {
            const lbl = document.createElement('label'); lbl.textContent = fd.label; lbl.className = 'fld-label'; body.appendChild(lbl);
            const ta = document.createElement('textarea'); ta.className = 'ce-field-val'; ta.rows = fd.rows || 4;
            if (fd.placeholder) ta.placeholder = fd.placeholder;
            ta.value = getVal(currentItem || {}, fd.key, fd); body.appendChild(ta);
        } else if (fd.type === 'number') {
            const lbl = document.createElement('label'); lbl.textContent = fd.label; lbl.className = 'fld-label'; body.appendChild(lbl);
            const inp = document.createElement('input'); inp.type = 'number'; inp.className = 'ce-field-val';
            if (fd.min !== undefined) inp.min = fd.min; if (fd.max !== undefined) inp.max = fd.max;
            inp.value = getVal(currentItem || {}, fd.key, fd); body.appendChild(inp);
        } else {
            const lbl = document.createElement('label'); lbl.textContent = fd.label; lbl.className = 'fld-label'; body.appendChild(lbl);
            const inp = document.createElement('input'); inp.type = 'text'; inp.className = 'ce-field-val';
            if (fd.placeholder) inp.placeholder = fd.placeholder;
            inp.value = getVal(currentItem || {}, fd.key, fd); body.appendChild(inp);
        }
        row.appendChild(body);
        if (!fd.builtin) {
            const del = document.createElement('button'); del.className = 'fld-del'; del.title = '删除此字段';
            del.textContent = '✕'; del.addEventListener('click', () => row.remove()); row.appendChild(del);
        } else { const spacer = document.createElement('span'); spacer.className = 'fld-del-spacer'; row.appendChild(spacer); }
        return row;
    }

    // ===== 分组 =====
    function makeGroup(g, gidx) {
        const container = document.createElement('div');
        container.className = 'grp-container';
        container.dataset.grpKey = g.key; container.dataset.grpIdx = gidx;

        const titleBar = document.createElement('div'); titleBar.className = 'grp-titlebar';
        const titleHandle = document.createElement('span'); titleHandle.className = 'grp-handle';
        titleHandle.title = '拖拽整组排序'; titleHandle.textContent = '⠿'; titleHandle.draggable = true;
        titleBar.appendChild(titleHandle);
        const titleLabel = document.createElement('span'); titleLabel.className = 'grp-label';
        titleLabel.textContent = g.label; titleBar.appendChild(titleLabel);
        if (!g.builtin) {
            const grpDel = document.createElement('button'); grpDel.className = 'grp-del'; grpDel.title = '删除此分组';
            grpDel.textContent = '✕'; grpDel.addEventListener('click', () => { container.remove(); });
            titleBar.appendChild(grpDel);
        }
        container.appendChild(titleBar);

        const fieldList = document.createElement('div'); fieldList.className = 'fld-list';
        g.fields.forEach(fd => fieldList.appendChild(makeFieldRow(fd)));
        container.appendChild(fieldList);

        const addBtn = document.createElement('button'); addBtn.className = 'modal-btn fld-add-btn';
        addBtn.textContent = '＋ 添加字段';
        addBtn.addEventListener('click', () => {
            const row = makeFieldRow({ key: '', label: '', value: '', type: 'custom', builtin: false });
            fieldList.appendChild(row); row.scrollIntoView({ behavior: 'smooth' });
        });
        container.appendChild(addBtn);

        // 组拖拽
        titleHandle.addEventListener('dragstart', (e) => {
            container.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('drag-type', 'group');
        });
        container.addEventListener('dragover', (e) => {
            e.preventDefault();
            if (e.dataTransfer.types.includes('drag-type') && e.dataTransfer.getData('drag-type') === 'group') {
                const src = groupsEl.querySelector('.dragging');
                if (src && src !== container) container.classList.add('drag-over');
            }
        });
        container.addEventListener('dragleave', () => container.classList.remove('drag-over'));
        container.addEventListener('drop', (e) => {
            e.preventDefault(); container.classList.remove('drag-over');
            if (e.dataTransfer.getData('drag-type') !== 'group') return;
            const src = groupsEl.querySelector('.dragging'); if (!src || src === container) return;
            const rect = container.getBoundingClientRect();
            if (e.clientY < rect.top + rect.height/2) groupsEl.insertBefore(src, container);
            else groupsEl.insertBefore(src, container.nextSibling);
        });

        // 字段拖拽
        fieldList.addEventListener('dragstart', (e) => {
            const row = e.target.closest('.fld-row');
            if (!row || e.target.closest('.grp-handle')) return;
            row.classList.add('fld-dragging'); e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('drag-type', 'field');
        });
        fieldList.addEventListener('dragover', (e) => {
            e.preventDefault(); const row = e.target.closest('.fld-row'); if (!row) return;
            const src = fieldList.querySelector('.fld-dragging');
            if (src && src !== row) row.classList.add('fld-drag-over');
        });
        fieldList.addEventListener('dragleave', (e) => {
            const row = e.target.closest('.fld-row'); if (!row) return;
            const rel = e.relatedTarget; if (!rel || !row.contains(rel)) row.classList.remove('fld-drag-over');
        });
        fieldList.addEventListener('drop', (e) => {
            e.preventDefault(); const row = e.target.closest('.fld-row'); if (!row) return;
            row.classList.remove('fld-drag-over');
            const src = fieldList.querySelector('.fld-dragging'); if (!src || src === row) return;
            const rect = row.getBoundingClientRect();
            if (e.clientY < rect.top + rect.height/2) row.parentNode.insertBefore(src, row);
            else row.parentNode.insertBefore(src, row.nextSibling);
        });
        return container;
    }

    // ===== 表单渲染 =====
    function renderForm() {
        const isNew = !currentItem && config.allowNew;
        const canDelete = !isNew && Boolean(config.deleteApi);
        const groups = config.buildGroups(currentItem || extraData || {});

        const titleText = !config.allowNew
            ? config.title.replace(/^[^—]+—/, '').trim()  // 单条模式不显示"新建/编辑"
            : (isNew ? '新建' : `编辑：${escapeHtml(currentItem.name || currentItem[config.idField])}`);

        formEl.innerHTML = `
            <h4 class="form-title">${titleText}</h4>
            <div class="ce-groups"></div>
            <button class="modal-btn ce-group-add" style="margin-top:8px;width:100%">＋ 添加分组</button>
            <div class="form-actions" style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                <span class="ce-status" style="color:var(--text-dim);font-size:12px;flex:1"></span>
                ${canDelete ? `<button class="modal-btn danger ce-delete">删除</button>` : ''}
                <button class="modal-btn primary ce-save">保存</button>
            </div>`;

        const groupsEl = formEl.querySelector('.ce-groups');
        const statusEl = formEl.querySelector('.ce-status');

        groups.forEach((g, i) => groupsEl.appendChild(makeGroup(g, i)));

        groupsEl.addEventListener('dragend', () => {
            fieldList.querySelectorAll('.dragging,.fld-dragging,.drag-over,.fld-drag-over').forEach(el => {
                el.classList.remove('dragging','fld-dragging','drag-over','fld-drag-over');
            });
        });

        formEl.querySelector('.ce-group-add').addEventListener('click', () => {
            const g = { key: '_user_'+Date.now(), label: '新分组', builtin: false, fields: [
                { key: '', label: '', value: '', type: 'custom', builtin: false }]};
            groupsEl.appendChild(makeGroup(g, groupsEl.querySelectorAll('.grp-container').length));
        });

        formEl.querySelector('.ce-save').addEventListener('click', async (event) => {
            const saveButton = event.currentTarget;
            if (!isCurrentSessionRef(editorRef)) {
                statusEl.textContent = '✗ 当前项目或存档已切换，请重新打开编辑器';
                return;
            }
            if (!canPerformAction('card_write', state.activeTurn && state.activeTurn.status)) {
                statusEl.textContent = '✗ 当前存档正在生成，请先取消或等待完成';
                return;
            }
            saveButton.disabled = true;
            const data = { custom: {} };
            let idVal = '';
            groupsEl.querySelectorAll('.grp-container').forEach(grp => {
                grp.querySelectorAll('.fld-row').forEach(row => {
                    const key = row.dataset.key; const type = row.dataset.type;
                    if (!key) return;
                    const keyInp = row.querySelector('.ce-field-key');
                    const valInp = row.querySelector('.ce-field-val');
                    if (keyInp && valInp) { const k = keyInp.value.trim(); const v = valInp.value.trim(); if (k) data.custom[k] = v; }
                    else if (valInp) {
                        const raw = valInp.type === 'checkbox' ? valInp.checked : valInp.value;
                        const isArray = row.dataset.array === '1';
                        setVal(data, key, raw, { array: isArray, type: type, number: type==='number', checkbox: type==='checkbox' });
                        if (key === config.idField) idVal = raw;
                    }
                });
            });
            if (config.idField && !idVal) {
                saveButton.disabled = false;
                alert(`请填 ${config.idLabel || 'ID'}`);
                return;
            }
            if (!data.custom || Object.keys(data.custom).length === 0) delete data.custom;
            // 单条模式（用户档案）固定 id='user'
            if (!config.idField) data.id = 'user';

            const saveUrl = config.saveApi ? config.saveApi(idVal || 'user') : null;
            if (!saveUrl) {
                saveButton.disabled = false;
                statusEl.textContent = '✗ 缺少保存 API';
                return;
            }

            statusEl.textContent = '保存中…';
            try {
                await apiClient.put(saveUrl, { data });
                if (!isCurrentSessionRef(editorRef)) return;
                statusEl.textContent = '✓ 已保存';
                // 刷新列表
                if (config.allowNew) {
                    const fresh = await apiClient.get(config.listApi, {
                        schema: body => Array.isArray(body && (body[config.listKey] || body.data)) || '编辑器列表响应无效',
                    });
                    if (!isCurrentSessionRef(editorRef)) return;
                    items = fresh[config.listKey] || fresh.data;
                    currentItem = items.find(it => it[config.idField] === idVal) || data;
                    renderList(); renderForm();
                }
                if (config.postSave) await config.postSave(data);
            } catch (e) {
                statusEl.textContent = '✗ ' + e.message;
            } finally {
                if (saveButton.isConnected) saveButton.disabled = isTurnActiveForRef(editorRef);
            }
        });

        const delBtn = formEl.querySelector('.ce-delete');
        if (delBtn) {
            delBtn.addEventListener('click', async () => {
                const deleteId = config.idField ? (currentItem && currentItem[config.idField]) : 'user';
                const displayName = config.idField ? (currentItem && (currentItem.name || currentItem[config.idField])) : '用户档案';
                if (!deleteId) return;
                const requestRef = editorRef;
                if (!isCurrentSessionRef(requestRef)) {
                    statusEl.textContent = '✗ 当前存档已切换，请重新打开编辑器';
                    return;
                }
                if (config.project && state.currentProject !== config.project) {
                    statusEl.textContent = '✗ 当前项目已切换，请重新打开编辑器';
                    return;
                }
                if (!canPerformAction('card_write', state.activeTurn && state.activeTurn.status)) {
                    statusEl.textContent = '✗ 当前存档正在生成，请先取消或等待完成';
                    return;
                }
                if (!confirm(`确定要删除「${displayName}」吗？删除后将移入回收区，可以恢复。`)) return;

                const deleteUrl = config.deleteApi(deleteId);
                statusEl.textContent = '删除中…';
                try {
                    let result;
                    try {
                        result = await apiClient.delete(deleteUrl);
                    } catch (error) {
                        if (error instanceof ApiError && error.status === 409) {
                            if (isCurrentSessionRef(requestRef)) {
                                try { await reloadCurrentSession(requestRef); } catch (_reloadError) {}
                            }
                        }
                        throw error;
                    }
                    if (!isCurrentSessionRef(requestRef)) return;
                    if (result.deleted !== true) throw new Error('删除响应无效：deleted 必须为 true');

                    const recoveryId = typeof result.recovery_id === 'string'
                        ? result.recovery_id
                        : '';
                    const affectedCount = Array.isArray(result.affected_saves)
                        ? result.affected_saves.length
                        : (Number.isInteger(result.affected_saves) ? result.affected_saves : 0);
                    const impactText = affectedCount > 0 ? `（影响 ${affectedCount} 个存档）` : '';

                    if (config.returnsSession) {
                        const activeSession = sessionFromResult(result);
                        if (
                            activeSession
                            && isCurrentSessionRef(requestRef)
                            && sessionBelongsToRef(activeSession, requestRef)
                        ) {
                            commitSessionState(activeSession, requestRef);
                        }
                    }

                    let refreshedItems = null;
                    let refreshedExtra = null;
                    try {
                        if (config.allowNew && config.listApi) {
                            const fresh = await apiClient.get(config.listApi);
                            const freshItems = fresh[config.listKey] || fresh.data;
                            if (!Array.isArray(freshItems)) throw new Error('列表响应格式无效');
                            refreshedItems = freshItems;
                        } else if (!config.allowNew && config.extraApi) {
                            refreshedExtra = await apiClient.get(config.extraApi);
                            if (!refreshedExtra || typeof refreshedExtra !== 'object' || Array.isArray(refreshedExtra)) {
                                throw new Error('档案响应格式无效');
                            }
                        }
                    } catch (refreshError) {
                        if (!isCurrentSessionRef(requestRef)) return;
                        statusEl.textContent = `✓ 已移入回收区，可恢复${impactText}；列表刷新失败：${refreshError.message}`;
                        if (recoveryId) statusEl.title = `恢复记录：${recoveryId}`;
                        showToast('已移入回收区，可恢复');
                        return;
                    }

                    if (!isCurrentSessionRef(requestRef)) return;
                    if (config.allowNew) {
                        if (refreshedItems !== null) items = refreshedItems;
                        currentItem = null;
                        renderList(); renderForm();
                    } else {
                        currentItem = null;
                        extraData = refreshedExtra || {};
                        renderForm();
                    }
                    const nextStatusEl = formEl.querySelector('.ce-status');
                    if (nextStatusEl) {
                        nextStatusEl.textContent = `✓ 已移入回收区，可恢复${impactText}`;
                        if (recoveryId) nextStatusEl.title = `恢复记录：${recoveryId}`;
                    }
                    showToast('已移入回收区，可恢复');
                    if (config.postDelete) {
                        try { await config.postDelete(deleteId, result); }
                        catch (postDeleteError) { console.warn('删除后刷新失败', postDeleteError); }
                    }
                } catch (e) { statusEl.textContent = '✗ ' + e.message; }
            });
        }
    }

    renderList();
    renderForm();
}

// ============================================================
// 3 个独立 modal 入口（v3）— 调用通用引擎
// ============================================================

async function openCharactersEditor() {
    const editorRef = captureSessionRef();
    // D2：角色卡字段由后端 schema 单一来源驱动
    const schema = await apiClient.get(API.characterSchema, {
        schema: body => Boolean(body && Array.isArray(body.groups)) || '角色卡 schema 响应无效',
    }).catch(error => {
        console.warn('加载角色卡 schema 失败', error);
        return null;
    });
    if (!schema || !schema.groups) {
        showToast('角色卡 schema 加载失败，请刷新重试');
        return;
    }
    if (!isCurrentSessionRef(editorRef)) return;
    await createCardEditor({
        title: '👥 角色卡 — 当前世界观的演员',
        listApi: `${API.characters}?project=${encodeURIComponent(editorRef.project)}`,
        listKey: 'characters',
        saveApi: (id) => `${API.characterSave(id)}?project=${encodeURIComponent(editorRef.project)}`,
        deleteApi: (id) => `${API.characterDelete(id)}?project=${encodeURIComponent(editorRef.project)}&save=${encodeURIComponent(editorRef.save)}&expected_revision=${currentRevision()}`,
        project: editorRef.project,
        sessionRef: editorRef,
        returnsSession: true,
        allowNew: true,
        idField: 'id',
        idLabel: '唯一 ID（文件名）',
        prefix: 'char',
        buildGroups: (c) => {
            c = c || {};
            const groups = schema.groups.map(g => ({ ...g, fields: g.fields.map(f => ({ ...f, builtin: true })) }));
            const custom = (c.custom && typeof c.custom === 'object') ? c.custom : {};
            const customEntries = Object.entries(custom);
            if (customEntries.length > 0) {
                groups.push({
                    key: schema.customGroup.key,
                    label: schema.customGroup.label,
                    builtin: false,
                    fields: customEntries.map(([k, v]) => ({ key: k, label: k, value: v, type: 'custom', builtin: false })),
                });
            }
            return groups;
        },
        postSave: async () => { showToast('角色已保存，点「重置」让新角色进场景'); },
    });
}

async function openWorldbookEditor() {
    const editorRef = captureSessionRef();
    await createCardEditor({
        title: '📖 世界书 — 当前世界观的设定',
        listApi: `${API.worldbook}?project=${encodeURIComponent(editorRef.project)}`,
        listKey: 'entries',
        saveApi: (id) => `${API.worldbookSave(id)}?project=${encodeURIComponent(editorRef.project)}`,
        deleteApi: (id) => `${API.worldbookDelete(id)}?project=${encodeURIComponent(editorRef.project)}`,
        project: editorRef.project,
        sessionRef: editorRef,
        allowNew: true,
        idField: 'id',
        idLabel: '条目名（英文）',
        prefix: 'world',
        buildGroups: (w) => {
            w = w || {};
            return [
                { key: '_basic', label: '基本信息', builtin: true, fields: [
                    { key: 'id', label: '条目名（英文）', type: 'text', builtin: true, required: true, placeholder: '比如：qinglong_shanghui' },
                    { key: 'enabled', label: '启用此设定', type: 'checkbox', builtin: true, checkboxLabel: '勾选后 AI 就会加载这条设定' },
                    { key: 'content', label: '设定内容', type: 'textarea', builtin: true, rows: 6, placeholder: 'AI 开局就会知道的设定...' },
                ]},
            ];
        },
    });
}

async function openUserEditor() {
    const editorRef = captureSessionRef();
    await createCardEditor({
        title: '👤 用户档案 — 当前世界的观众设定',
        listApi: null,
        allowNew: false,
        idField: null,
        saveApi: () => `${API.userSave}?project=${encodeURIComponent(editorRef.project)}`,
        deleteApi: (id) => `${API.userDelete}?project=${encodeURIComponent(editorRef.project)}&save=${encodeURIComponent(editorRef.save)}&expected_revision=${currentRevision()}`,
        project: editorRef.project,
        sessionRef: editorRef,
        returnsSession: true,
        prefix: 'user',
        extraApi: `${API.user}?project=${encodeURIComponent(editorRef.project)}`,
        buildGroups: (ud) => {
            ud = ud || {};
            return [
                { key: '_identity', label: '身份', builtin: true, fields: [
                    { key: 'name', label: '用户名', type: 'text', builtin: true },
                    { key: 'identity', label: '身份设定', type: 'textarea', builtin: true, rows: 5 },
                ]},
                { key: '_status', label: '当前状态', builtin: true, fields: [
                    { key: 'status.condition', label: '身体状态', type: 'text', builtin: true },
                    { key: 'status.abilities', label: '能力（每行一个）', type: 'textarea', builtin: true, rows: 2, array: true },
                ]},
                { key: '_scene', label: '初始场景', builtin: true, fields: [
                    { key: 'scene_meta.location', label: '地点', type: 'text', builtin: true },
                    { key: 'scene_meta.time', label: '时间/天气', type: 'text', builtin: true },
                    { key: 'scene_meta.main_quest', label: '主线任务', type: 'text', builtin: true },
                    { key: 'scene_meta.current_scene', label: '当前场景描述', type: 'textarea', builtin: true, rows: 3 },
                    { key: 'scene_meta.next_goal', label: '下一步目标', type: 'textarea', builtin: true, rows: 2 },
                ]},
            ];
        },
        postSave: async () => { showToast('用户档案已保存，点「重置」生效'); },
    });
}


// ===== 模型参数面板 =====

function slider(label, id, min, max, step, value, hint) {
    return `<div class="form-slider">
      <div class="slider-top"><span class="slider-label">${label}</span><span class="slider-value" id="val-${id}">${value}</span></div>
      <input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${value}">
      <div class="slider-hint">${hint}</div></div>`;
}

function showModelParamsEditor() {
    const p = state.modelParams;
    const body = document.createElement('div');
    body.innerHTML = `
        <div class="param-intro">这里的滑块控制 AI 这轮"说话的风格"。调完点「保存」，下次发消息生效。<b>往左调小 = 更稳、更老实跟你设定走；往右调大 = 更天马行空、更"放"。</b></div>
        ${slider('活跃度（温度）', 'p-temp', 0, 2, 0.1, p.temperature, '越小越老实，越大越奔放。常用 0.6~1.0')}
        ${slider('收口（top_p）', 'p-topp', 0.1, 1, 0.05, p.top_p, '越小AI越只挑最有把握的词，越稳。')}
        ${slider('候选（top_k）', 'p-topk', 0, 200, 1, p.top_k, '0=不限。越小越保守。')}
        ${slider('最多字数', 'p-nump', 256, 16384, 256, p.num_predict, 'AI 一轮最多写多少字。太短会被截断。')}
        <div class="form-slider">
          <div class="slider-top"><span class="slider-label">开"内心思考"</span>
            <label class="param-switch"><input type="checkbox" id="p-think" ${p.think ? 'checked' : ''}><span class="switch-slider"></span></label>
          </div>
          <div class="slider-hint">开了AI先想再写（质量好但慢几秒）；关了直接写（快、但略糙）。</div>
        </div>`;

    const saveParams = async () => {
        state.modelParams.temperature = Number(body.querySelector('#p-temp').value);
        state.modelParams.top_p = Number(body.querySelector('#p-topp').value);
        state.modelParams.top_k = Number(body.querySelector('#p-topk').value);
        state.modelParams.num_predict = Number(body.querySelector('#p-nump').value);
        state.modelParams.think = body.querySelector('#p-think').checked;
        await saveSettings();
    };

    // 只调用一次，footer 一次性到位
    showModal({ title: '🎛 AI 说话风格', body, footer: { confirmText: '保存', cancelText: '取消', onConfirm: saveParams } });

    // showModal 之后 body 已挂 DOM，此时挂 input 监听
    const upd = (id, fmt) => {
        const el = body.querySelector(`#val-${id}`);
        const inp = body.querySelector(`#${id}`);
        const f = () => { el.textContent = fmt ? fmt(inp.value) : inp.value; };
        inp.addEventListener('input', f); f();
    };
    upd('p-temp', v => Number(v).toFixed(1));
    upd('p-topp', v => Number(v).toFixed(2));
    upd('p-topk');
    upd('p-nump');
}

// ===== 存档历史 =====

async function showHistoryEditor() {
    const historyRef = captureSessionRef();
    const body = document.createElement('div');
    body.innerHTML = `<div class="param-intro">这里存着之前几次的存档快照（每次"重新生成"前会自动存一份）。点某个版本的「恢复」就回到那一次；点「预览」只读查看快照内容，不会覆盖当前存档。</div>
        <div id="history-list" class="history-list"><p style="color:var(--text-dim)">加载中…</p></div>`;
    showModal({ title: '🕐 历史存档', body });

    const listEl = body.querySelector('#history-list');
    try {
        const url = `${API.sessionHistory}?project=${encodeURIComponent(historyRef.project)}&save=${encodeURIComponent(historyRef.save)}`;
        const data = await apiClient.get(url, {
            schema: body => Array.isArray(body && body.snapshots) || '历史快照列表响应无效',
        });
        if (!isCurrentSessionRef(historyRef)) {
            listEl.innerHTML = '<p class="empty">当前存档已切换，请重新打开历史存档。</p>';
            return;
        }
        const snaps = data.snapshots || [];
        if (snaps.length === 0) { listEl.innerHTML = `<p class="empty">还没有历史快照。点一轮对话的「🔄」重新生成，或先聊一会再回来看。</p>`; return; }
        listEl.innerHTML = snaps.map(s => {
            const typeLabel = s.type === 'trim' ? ' 📄 trim'
                : s.type === 'reset' ? ' 🔄 重置'
                : '';
            const isTrim = s.type === 'trim';
            return `<div class="history-item ${isTrim ? 'history-item-trim' : ''}"><span class="history-time">🕐 ${escapeHtml(s.timestamp || s.modified_at || s.filename)}${typeLabel}</span>
                <div class="history-actions">
                    <button class="modal-btn history-preview" data-fn="${escapeHtml(s.filename)}">预览</button>
                    ${isTrim ? '' : `<button class="modal-btn history-restore" data-fn="${escapeHtml(s.filename)}">恢复</button>`}
                </div></div>`;
        }).join('');
        listEl.querySelectorAll('.history-preview').forEach(btn => {
            btn.addEventListener('click', () => showSnapshotPreview(btn.dataset.fn, historyRef));
        });
        listEl.querySelectorAll('.history-restore').forEach(btn => {
            btn.addEventListener('click', async () => {
                const fn = btn.dataset.fn;
                if (!confirm('恢复这份快照？当前存档内容会被这份覆盖。')) return;
                if (!isCurrentSessionRef(historyRef)) {
                    hideModal();
                    alert('当前存档已切换，请重新打开历史存档');
                    return;
                }
                let result;
                try {
                    result = await sessionWrite(API.sessionRestore, 'POST', {
                        project: historyRef.project,
                        save: historyRef.save,
                        filename: fn,
                    }, '恢复快照', { applyResult: false });
                } catch (error) {
                    alert('恢复失败：' + error.message);
                    return;
                }
                if (!isCurrentSessionRef(historyRef)) return;
                const session = sessionFromResult(result);
                if (!sessionBelongsToRef(session, historyRef)) {
                    alert('恢复失败：响应存档与当前存档不一致');
                    return;
                }
                if (!commitSessionState(session, historyRef)) return;
                hideModal();
                alert('已恢复到该快照');
            });
        });
    } catch (e) { listEl.innerHTML = `<p class="empty">读取历史失败：${escapeHtml(e.message)}</p>`; }
}

async function showSnapshotPreview(filename, snapshotRef = captureSessionRef()) {
    const body = document.createElement('div');
    body.innerHTML = '<div class="snapshot-preview"><p style="color:var(--text-dim)">加载中…</p></div>';
    showModal({ title: '🔍 快照预览', body });

    const previewEl = body.querySelector('.snapshot-preview');
    try {
        const url = `${API.sessionSnapshot(filename)}&project=${encodeURIComponent(snapshotRef.project)}&save=${encodeURIComponent(snapshotRef.save)}`;
        const data = await apiClient.get(url, {
            schema: body => Boolean(body && typeof body.filename === 'string' && Array.isArray(body.messages)) || '快照响应无效',
        });
        if (!isCurrentSessionRef(snapshotRef)) {
            previewEl.innerHTML = '<p class="empty">当前存档已切换，请重新打开快照。</p>';
            return;
        }
        const typeBadge = data.snapshot_type === 'trim' ? '📄 trim（被截消息）'
            : data.snapshot_type === 'reset' ? '🔄 重置归档'
            : '💾 快照';
        const msgs = data.messages || [];
        previewEl.innerHTML = `
            <div class="snapshot-meta">
                <span class="snapshot-badge">${typeBadge}</span>
                <span class="snapshot-filename">${escapeHtml(data.filename)}</span>
                <span class="snapshot-time">${escapeHtml(data.modified_at || '')}</span>
            </div>
            <div class="snapshot-count">共 ${msgs.length} 条消息</div>
            <div class="snapshot-list">${msgs.map((m, i) => {
                const roleLabel = m.role === 'user' ? '你' : 'AI';
                const roleClass = m.role === 'user' ? 'user' : 'assistant';
                return `<div class="snapshot-msg ${roleClass}">
                    <div class="snapshot-msg-idx">#${i + 1}</div>
                    <div class="snapshot-msg-role">${roleLabel}</div>
                    <div class="snapshot-msg-content">${escapeHtml(m.content || '')}</div>
                </div>`;
            }).join('') || '<p class="empty" style="margin-top:12px">无消息内容</p>'}</div>`;
    } catch (e) { previewEl.innerHTML = `<p class="empty">读取失败：${escapeHtml(e.message)}</p>`; }
}

// ===== 提示词编辑器 =====

async function showPromptsEditor() {
    try {
        const data = await apiClient.get(API.prompts, {
            schema: body => Boolean(body && typeof body.system === 'string' && typeof body.group_chat === 'string') || '提示词响应无效',
        });
        const body = document.createElement('div');
        body.innerHTML = `
            <div class="modal-tabs">
                <button class="modal-tab active" data-tab="system">系统提示</button>
                <button class="modal-tab" data-tab="group_chat">群聊模板</button>
            </div>
            <div class="form-row"><textarea id="prompt-textarea" rows="18">${escapeHtml(data.system || '')}</textarea></div>
            <div class="form-actions">
                <button class="modal-btn" id="prompt-save">保存</button>
                <button class="modal-btn" id="prompt-reset">恢复默认</button>
                <span style="color:var(--text-dim);font-size:11px;margin-left:auto" id="prompt-status"></span>
            </div>`;
        showModal({ title: '编辑提示词', body });

        const tabs = body.querySelectorAll('.modal-tab');
        const textarea = body.querySelector('#prompt-textarea');
        let currentTab = 'system';

        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                tabs.forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                currentTab = tab.dataset.tab;
                textarea.value = data[currentTab] || '';
            });
        });

        body.querySelector('#prompt-save').addEventListener('click', async () => {
            const statusEl = body.querySelector('#prompt-status');
            statusEl.textContent = '保存中…';
            try {
                await apiClient.put(API.promptSave(currentTab), { content: textarea.value });
                statusEl.textContent = '✓ 已保存，下次对话生效';
                setTimeout(() => { statusEl.textContent = ''; }, 3000);
            } catch (error) {
                statusEl.textContent = '✗ ' + errorDetail(error);
            }
        });

        body.querySelector('#prompt-reset').addEventListener('click', async () => {
            if (!confirm('恢复为默认提示词？当前编辑内容会丢失。')) return;
            try {
                await apiClient.post(API.promptReset(currentTab));
                const fresh = await apiClient.get(API.prompts, {
                    schema: body => Boolean(body && typeof body.system === 'string' && typeof body.group_chat === 'string') || '提示词响应无效',
                });
                data[currentTab] = fresh[currentTab]; textarea.value = fresh[currentTab] || '';
                const statusEl = body.querySelector('#prompt-status');
                statusEl.textContent = '✓ 已恢复默认'; setTimeout(() => { statusEl.textContent = ''; }, 3000);
            } catch (error) {
                body.querySelector('#prompt-status').textContent = '✗ ' + errorDetail(error);
            }
        });
    } catch (error) {
        showToast(`加载提示词失败：${errorDetail(error)}`, 3500);
    }
}

// ===== 事件绑定 =====

function bindUI() {
    // ===== 新顶栏 v3 =====

    // 项目按钮 → 弹出项目下拉
    const projectBtn = document.getElementById('project-btn');
    const projectDropdown = document.getElementById('project-dropdown');
    projectBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (state.navigationBusy || isTurnActiveForRef(committedSessionRef)) {
            showToast('当前正在切换或生成，请稍候');
            return;
        }
        const wasHidden = projectDropdown.classList.contains('hidden');
        hideAllDropdowns();
        if (wasHidden) {
            positionDropdown(projectDropdown, projectBtn);
            projectDropdown.classList.remove('hidden');
        }
    });

    // 项目下拉：新建项目
    document.getElementById('project-new-inline').addEventListener('click', () => {
        hideAllDropdowns();
        promptForNewProject();
    });

    // 4 个 tab：角色 / 世界书 / 用户 / 存档 (v3: 拆成 3 个独立 modal)
    document.getElementById('tab-chars').addEventListener('click', () => {
        hideAllDropdowns();
        openCharactersEditor();  // v3: 独立 modal
    });
    document.getElementById('tab-world').addEventListener('click', () => {
        hideAllDropdowns();
        openWorldbookEditor();  // v3: 独立 modal
    });
    document.getElementById('tab-user').addEventListener('click', () => {
        hideAllDropdowns();
        openUserEditor();  // v3: 独立 modal
    });
    const saveTab = document.getElementById('tab-saves');
    const saveDropdown = document.getElementById('save-dropdown');
    saveTab.addEventListener('click', (e) => {
        e.stopPropagation();
        if (state.navigationBusy || isTurnActiveForRef(committedSessionRef)) {
            showToast('当前正在切换或生成，请稍候');
            return;
        }
        const wasHidden = saveDropdown.classList.contains('hidden');
        hideAllDropdowns();
        if (wasHidden) {
            positionDropdown(saveDropdown, saveTab);
            saveDropdown.classList.remove('hidden');
        }
    });

    // 存档下拉内的 5 个操作按钮
    document.getElementById('save-new-inline').addEventListener('click', () => { hideAllDropdowns(); promptForNewSave(); });
    document.getElementById('save-rename-inline').addEventListener('click', () => { hideAllDropdowns(); promptForRenameSave(); });
    document.getElementById('save-delete-inline').addEventListener('click', () => { hideAllDropdowns(); deleteCurrentSave(); });
    document.getElementById('save-export-inline').addEventListener('click', () => { hideAllDropdowns(); exportCurrentSave(); });
    document.getElementById('save-import-inline').addEventListener('click', () => { hideAllDropdowns(); promptForImportSave(); });

    // 点击空白处关闭所有下拉
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.dropdown-panel') && !e.target.closest('#project-btn') && !e.target.closest('#tab-saves')) {
            hideAllDropdowns();
        }
    });

    // ===== 旧绑定（保留兜底，节点已被 legacy-hide） =====
    const oldProjSel = document.getElementById('project-select');
    if (oldProjSel) oldProjSel.addEventListener('change', (e) => switchProject(e.target.value));
    const oldSaveSel = document.getElementById('save-select');
    if (oldSaveSel) oldSaveSel.addEventListener('change', (e) => switchSave(e.target.value));

    // 模型切换
    document.getElementById('model-select').addEventListener('change', async (e) => {
        const model = e.target.value;
        if (!model) return;
        try {
            await sessionWrite(API.switchModel, 'POST', {
                project: state.currentProject,
                save: state.currentSave,
                model,
            }, '切换模型');
        } catch (error) {
            alert('切换模型失败：' + error.message);
            if (state.session && state.session.current_model) e.target.value = state.session.current_model;
        }
    });

    // 重置
    document.getElementById('reset-btn').addEventListener('click', async () => {
        if (!confirm('重置当前存档？将清空对话历史和角色状态，但保留存档本身。')) return;
        const requestRef = captureSessionRef();
        let result;
        try {
            result = await sessionWrite(API.reset, 'POST', {
                project: requestRef.project,
                save: requestRef.save,
            }, '重置存档', { applyResult: false });
        } catch (error) {
            alert('重置失败：' + error.message);
            return;
        }
        if (!isCurrentSessionRef(requestRef)) return;
        const session = sessionFromResult(result);
        if (!sessionBelongsToRef(session, requestRef)) {
            alert('重置失败：响应存档与当前存档不一致');
            return;
        }
        if (!commitSessionState(session, requestRef)) return;
        document.getElementById('suggestions').innerHTML = '';
    });

    // 工具按钮（顶栏右侧）
    document.getElementById('prompts-btn').addEventListener('click', showPromptsEditor);
    document.getElementById('model-params-btn').addEventListener('click', showModelParamsEditor);
    document.getElementById('history-btn').addEventListener('click', showHistoryEditor);

    // 发送
    document.getElementById('send-btn').addEventListener('click', () => sendMessage());
    // 业务取消必须由服务端收口 turn；POST 完成后才关闭本地 SSE。
    const cancelGenBtn = document.getElementById('send-cancel-btn');
    cancelGenBtn.addEventListener('click', () => cancelActiveTurn());
    document.getElementById('user-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });

    // thinking 关闭
    document.getElementById('thinking-close').addEventListener('click', () => {
        document.getElementById('thinking-panel').classList.add('hidden');
    });

    // Modal 关闭
    document.getElementById('modal-close').addEventListener('click', hideModal);
    document.getElementById('modal-cancel').addEventListener('click', hideModal);
    document.getElementById('modal-backdrop').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) hideModal();
    });
}

// ===== 新顶栏所需的弹窗辅助函数 =====

function promptForNewProject() {
    const input = document.createElement('input');
    input.type = 'text'; input.placeholder = '新项目名（如：修仙世界）';
    input.style.cssText = 'width:100%;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text)';
    showModal({ title: '新建项目（世界观）', body: input, footer: { confirmText: '创建', onConfirm: async () => {
        const name = input.value.trim();
        if (name) await createNewProject(name);
    }}});
    setTimeout(() => input.focus(), 100);
}

function promptForNewSave() {
    const input = document.createElement('input');
    input.type = 'text'; input.placeholder = '存档名（如：主线剧情 / 支线A）';
    input.style.cssText = 'width:100%;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text)';
    showModal({ title: '新建存档', body: input, footer: { confirmText: '创建', onConfirm: async () => {
        const name = input.value.trim() || '新存档';
        await createNewSave(name);
    }}});
    setTimeout(() => input.focus(), 100);
}

function promptForRenameSave() {
    const current = state.saveList.find(s => s.session_id === state.currentSave);
    const input = document.createElement('input');
    input.type = 'text'; input.value = current ? current.name : '';
    input.style.cssText = 'width:100%;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text)';
    showModal({ title: '重命名存档', body: input, footer: { confirmText: '保存', onConfirm: async () => {
        const name = input.value.trim();
        if (name) await renameCurrentSave(name);
    }}});
}

function promptForImportSave() {
    const body = document.createElement('div');
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = '.json';
    inp.setAttribute('aria-describedby', 'save-import-status');
    const status = document.createElement('p');
    status.id = 'save-import-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.style.cssText = 'min-height:20px;margin-top:8px;color:var(--text-dim)';
    body.appendChild(inp);
    body.appendChild(status);
    inp.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        inp.disabled = true;
        status.textContent = '正在验证并导入…';
        try {
            await importSave(file);
            status.textContent = '导入成功';
            showToast('存档导入成功');
            hideModal();
        } catch (error) {
            status.textContent = '导入失败：' + error.message;
            inp.disabled = false;
            inp.value = '';
            inp.focus();
        }
    });
    showModal({ title: '导入存档', body });
    inp.click();
}

function scrollToBottom() {
    const stream = document.getElementById('chat-stream');
    requestAnimationFrame(() => { stream.scrollTop = stream.scrollHeight; });
}

// 启动
init();
