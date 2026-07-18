import { createModalController, modalElementsFromDocument } from './modal.mjs';
import { enterMessageEditor } from './message-editor.mjs';
import {
    clearDragState,
    collectCardEditorData,
    getPathValue,
    groupsWithCustomFields,
    mergeCardEditorData,
} from './card-editor.mjs';
import { createProjectService } from './projects.mjs';
import { createSaveService } from './saves.mjs';
import { createTurnPayload, createTurnPersistence } from './chat.mjs';
import { createSummaryService, summaryPatchFromForm } from './summaries.mjs';
import { renderSummaryPanelView } from './summary-panel.mjs';
import { createPromptService, promptTabTargetIndex } from './prompt-editor.mjs';
import { clampAnchoredLeft, createListboxController } from './listbox.mjs';
import { createFrameRenderer } from './frame-renderer.mjs';
import { affinityBar, createMessageElement, mountMessageHistory } from './render.mjs';
import {
    createWorldbookEditor,
    createWorldbookService,
    sanitizeWorldbookDiagnostics,
} from './worldbook.mjs';
import {
    createRoleplayPanel,
    createRoleplayService,
    renderRoleplayWarnings,
} from './roleplay.mjs';

// 本地酒馆 — 前端逻辑 v2（项目+存档双层架构）
// 流式对话、角色卡渲染、行动建议、会话管理、提示词编辑

if (!globalThis.TavernSecurity) throw new Error('安全渲染模块未加载');
if (!globalThis.TavernApi) throw new Error('ApiClient 模块未加载');
if (!globalThis.TavernSessionRef) throw new Error('SessionRef 模块未加载');
if (!globalThis.TavernTurn) throw new Error('TurnClient 模块未加载');

const TavernSecurity = globalThis.TavernSecurity;

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
    turnLocksSession,
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
    projectStats: '/api/projects/stats',
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
    summary: '/api/session/summary',
    summaryRegen: '/api/session/summary/regenerate',
    // 角色/世界书/用户
    characterSchema: '/api/schema/character',
    worldbookSchema: '/api/schema/worldbook',
    characterSave: (id) => `/api/characters/${encodeURIComponent(id)}`,
    characterDelete: (id) => `/api/characters/${encodeURIComponent(id)}`,
    worldbookSave: (id) => `/api/worldbook/${encodeURIComponent(id)}`,
    worldbookDelete: (id) => `/api/worldbook/${encodeURIComponent(id)}`,
    worldbookManual: '/api/session/worldbook/manual',
    roleplaySilence: (id) => `/api/session/characters/${encodeURIComponent(id)}/silence`,
    roleplayPolicy: '/api/session/roleplay-policy',
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
    selectedSummaryId: null,
    summaryPanelExpanded: false,
    lastWorldbookDiagnostics: null,
    roleplayPanel: null,
    modelParams: {
        temperature: 0.8,
        top_p: 0.9,
        top_k: 40,
        num_predict: 4096,
        think: true,
    },
};

const projectService = createProjectService(apiClient, API);
const saveService = createSaveService(apiClient, API);
const promptService = createPromptService(apiClient, API);
const summaryService = createSummaryService(sessionWrite, API);
const worldbookService = createWorldbookService(apiClient, sessionWrite, API);
const roleplayService = createRoleplayService(sessionWrite, API);
const turnPersistence = createTurnPersistence(sessionStorage);
const modalController = createModalController(
    modalElementsFromDocument(document),
    { documentRef: document },
);
modalController.bind();
const showModal = config => modalController.show(config);
const hideModal = options => modalController.hide(options);

const sessionRefs = new SessionRefTracker(state.currentProject, state.currentSave);
let committedSessionRef = sessionRefs.capture();
let activeController = null;   // 当前 turn SSE 的 AbortController；业务取消必须调用服务端 cancel API
let activeFrameRenderer = null;
let projectListboxController = null;
let saveListboxController = null;
const latestRequest = { projects: 0, projectStats: 0, saves: 0 };
const summaryWatchers = new Map();

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

function rememberWorldbookDiagnostics(ref, promptDiagnostics) {
    if (!ref || !isCurrentSessionRef(ref)) return [];
    const entries = sanitizeWorldbookDiagnostics(promptDiagnostics);
    state.lastWorldbookDiagnostics = Object.freeze({
        project: ref.project,
        save: ref.save,
        epoch: ref.epoch,
        entries: Object.freeze(entries),
    });
    return entries;
}

function worldbookDiagnosticsForRef(ref) {
    const cached = state.lastWorldbookDiagnostics;
    if (!cached || !ref) return [];
    if (cached.project !== ref.project || cached.save !== ref.save || cached.epoch !== ref.epoch) return [];
    return cached.entries;
}

function hasWorldbookDiagnosticsForRef(ref) {
    const cached = state.lastWorldbookDiagnostics;
    return Boolean(cached && ref
        && cached.project === ref.project
        && cached.save === ref.save
        && cached.epoch === ref.epoch);
}

async function loadLatestWorldbookDiagnostics(ref) {
    if (hasWorldbookDiagnosticsForRef(ref)) return worldbookDiagnosticsForRef(ref);
    if (!isCurrentSessionRef(ref) || !sessionBelongsToRef(state.session, ref)) return [];
    const latest = [...(state.session.message_history || [])].reverse().find(message => (
        message && typeof message.turn_id === 'string' && message.turn_id
    ));
    if (!latest) return [];
    try {
        const turn = await turnClient.get(latest.turn_id);
        if (!isCurrentSessionRef(ref) || turn.project !== ref.project || turn.save !== ref.save) return [];
        return rememberWorldbookDiagnostics(ref, turn.prompt_diagnostics);
    } catch (error) {
        console.warn('读取上轮世界书诊断失败', error);
        return [];
    }
}

function setNavigationUiState(active) {
    state.navigationBusy = active;
    const blocked = active || Boolean(state.activeTurn && !state.activeTurn.terminal);
    for (const id of ['project-btn', 'tab-world', 'tab-saves', 'model-select', 'reset-btn']) {
        const element = document.getElementById(id);
        if (!element) continue;
        if ('disabled' in element) element.disabled = blocked;
        element.setAttribute('aria-disabled', String(blocked));
        element.setAttribute('aria-busy', String(active));
    }
    if (state.roleplayPanel) state.roleplayPanel.setDisabled(blocked);
}

function beginSessionTransition(project, save = null) {
    cancelActiveTurnFrame({ flush: true });
    cancelSummaryWatchers();
    state.selectedSummaryId = null;
    state.summaryPanelExpanded = false;
    state.lastWorldbookDiagnostics = null;
    setNavigationUiState(true);
    return sessionRefs.advance(project, save);
}

function rollbackSessionTransition(candidateRef) {
    if (isCurrentSessionRef(candidateRef)) {
        const restoredRef = sessionRefs.advance(
            committedSessionRef.project,
            committedSessionRef.save,
        );
        committedSessionRef = restoredRef;
        setNavigationUiState(false);
        if (sessionBelongsToRef(state.session, restoredRef)) {
            watchPendingSummaries(restoredRef, state.session);
        }
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
    watchPendingSummaries(ref, commit.session);
    if (isTurnActiveForRef(ref)) setTurnUiState(true, Boolean(state.activeTurn && state.activeTurn.cancelling));
    return true;
}

function commitSummaryRefresh(session, ref) {
    const commit = buildSessionCommit({
        session,
        ref,
        currentRef: captureSessionRef(),
        currentSession: state.session,
        saveList: state.saveList,
    });
    if (!commit.accepted) return false;
    const summariesUnchanged = state.session
        && state.session.revision === commit.session.revision
        && JSON.stringify(state.session.summaries || [])
            === JSON.stringify(commit.session.summaries || []);
    if (summariesUnchanged) return true;
    state.session = commit.session;
    state.saveList = commit.saveList;
    const stream = document.getElementById('chat-stream');
    const lastAssistant = stream && stream.querySelector('.msg.assistant:last-of-type');
    if (lastAssistant) renderSummaryPanel(lastAssistant, commit.session);
    watchPendingSummaries(ref, commit.session);
    return true;
}

function isTurnActiveForRef(ref = captureSessionRef()) {
    return turnLocksSession(state.activeTurn, ref);
}

function canPerformTurnAction(action, ref = captureSessionRef()) {
    return !isTurnActiveForRef(ref)
        && canPerformAction(action, state.activeTurn && state.activeTurn.status);
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
    const projects = await projectService.list();
    if (!isCurrentSessionRef(requestRef) || requestId !== latestRequest.projects) return null;
    if (projects.length === 0) {
        throw new ApiError('服务端没有可用项目', { code: 'empty_project_list' });
    }
    state.projectList = projects;
    return state.projectList;
}

// ===== 项目下拉渲染（含元信息） =====
function projectStatValue(stat, field, errorCode) {
    return Array.isArray(stat.errors) && stat.errors.includes(errorCode) ? '—' : stat[field];
}

async function renderProjectDropdown(requestRef = captureSessionRef()) {
    const listEl = document.getElementById('project-list');
    if (!listEl) return;
    const requestId = ++latestRequest.projectStats;

    const projectList = [...state.projectList];
    const stats = await projectService.loadStats(projectList);
    for (const [project, value] of Object.entries(stats)) {
        if (value.status === 'partial') {
            console.warn(`项目 ${project} 统计不完整`, value.errors.join(','));
        }
    }

    if (!isCurrentSessionRef(requestRef) || requestId !== latestRequest.projectStats) return;

    listEl.innerHTML = projectList.map(p => {
        const st = stats[p] || { characters:0, worldbook:0, saves:0 };
        const active = p === state.currentProject ? 'active' : '';
        return `<div class="dropdown-item ${active}" data-project="${escapeHtml(p)}">
            <span class="item-name">📁 ${escapeHtml(p)}</span>
            <span class="project-item-stats">👥${projectStatValue(st, 'characters', 'characters_unavailable')} 📖${projectStatValue(st, 'worldbook', 'worldbook_unavailable')} 💾${projectStatValue(st, 'saves', 'sessions_unavailable')}</span>
        </div>`;
    }).join('');
    if (projectListboxController) projectListboxController.refresh();

    // 更新顶栏项目名 + 元信息
    updateProjectButton(stats);
}

function updateProjectButton(statsMap) {
    const nameEl = document.getElementById('project-name');
    const statsEl = document.getElementById('project-stats');
    if (nameEl) nameEl.textContent = state.currentProject;
    if (statsEl && statsMap) {
        const st = statsMap[state.currentProject] || { characters:0, worldbook:0, saves:0 };
        statsEl.innerHTML = `<span class="stat">👥${projectStatValue(st, 'characters', 'characters_unavailable')}</span><span class="stat">📖${projectStatValue(st, 'worldbook', 'worldbook_unavailable')}</span><span class="stat">💾${projectStatValue(st, 'saves', 'sessions_unavailable')}</span>`;
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
    if (projectListboxController) projectListboxController.close();
    if (saveListboxController) saveListboxController.close();
    document.querySelectorAll('.dropdown-panel').forEach(p => p.classList.add('hidden'));
}

function positionDropdown(panel, anchor) {
    const r = anchor.getBoundingClientRect();
    const panelWidth = panel.getBoundingClientRect().width;
    const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
    panel.style.top = (r.bottom + 4) + 'px';
    panel.style.left = clampAnchoredLeft(r.left, panelWidth, viewportWidth) + 'px';
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

async function saveSettings(params = state.modelParams) {
    await apiClient.put(API.settings, { data: params });
    return params;
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
    return saveService.list(project);
}

async function fetchSession(ref) {
    if (!ref || !ref.save) throw new ApiError('缺少存档引用', { code: 'invalid_session_ref' });
    const session = await saveService.get(ref.project, ref.save);
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
            session = await saveService.create(project, '默认存档');
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
    renderSaveDropdown();
    const badge = document.getElementById('save-count');
    if (badge) badge.textContent = state.saveList.length;
}

function renderSaveDropdown() {
    const listEl = document.getElementById('save-list');
    if (!listEl) return;
    if (state.saveList.length === 0) {
        listEl.innerHTML = '<div class="dropdown-item" style="color:var(--text-dim);cursor:default">— 无存档 —</div>';
        if (saveListboxController) saveListboxController.refresh();
        return;
    }
    listEl.innerHTML = state.saveList.map(s => {
        const active = s.session_id === state.currentSave ? 'active' : '';
        return `<div class="dropdown-item ${active}" data-save="${escapeHtml(s.session_id)}">
            <span class="item-name">💾 ${escapeHtml(s.name)}</span>
            <span class="item-meta">${s.message_count || 0} 条</span>
        </div>`;
    }).join('');
    if (saveListboxController) saveListboxController.refresh();
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
        const session = await saveService.create(state.currentProject, name);
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
        throw error;
    }
}

async function renameCurrentSave(newName) {
    if (!state.currentSave) return false;
    const requestRef = captureSessionRef();
    let body;
    try {
        body = await sessionWrite(API.sessionRename, 'POST', {
            project: requestRef.project,
            save: requestRef.save,
            new_name: newName,
        }, '重命名', { applyResult: false });
    } catch (error) {
        throw error;
    }
    if (!isCurrentSessionRef(requestRef)) return false;
    const session = sessionFromResult(body);
    if (!session || (session.project && session.project !== requestRef.project)) {
        throw new ApiError('重命名失败：响应缺少有效存档', { code: 'invalid_response_schema' });
    }
    const candidateRef = beginSessionTransition(requestRef.project, session.session_id);
    const previousSaveList = state.saveList;
    state.saveList = state.saveList.map(item => item.session_id === requestRef.save
        ? { ...item, session_id: session.session_id, name: session.name || newName }
        : item);
    if (!commitSessionState(session, candidateRef)) {
        state.saveList = previousSaveList;
        rollbackSessionTransition(candidateRef);
        throw new ApiError('重命名结果已过期，请重新打开存档', { code: 'stale_session_ref' });
    }
    renderSaveListControls();
    return session;
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
    const data = await saveService.exportJson(requestRef.project, requestRef.save);
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
    await saveService.importJson(requestRef.project, text, file.name.replace(/\.json$/i, ''));
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
    const created = await projectService.create(name);
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
    renderCharacterPanel(session);
}

function renderCharacterPanel(session) {
    const list = document.getElementById('character-list');
    if (!list) return;
    const panelRef = captureSessionRef();
    state.roleplayPanel = createRoleplayPanel({
        documentRef: document,
        container: list,
        session,
        sessionRef: panelRef,
        service: roleplayService,
        disabled: state.navigationBusy || isTurnActiveForRef(panelRef),
        isCurrent: () => isCurrentSessionRef(panelRef) && sessionBelongsToRef(state.session, panelRef),
        isTurnActive: () => isTurnActiveForRef(panelRef),
        recoverDraft: draft => {
            if (!isCurrentSessionRef(panelRef) || !state.roleplayPanel) return;
            const controls = draft.kind === 'policy'
                ? state.roleplayPanel.elements.policy
                : state.roleplayPanel.elements.characters.get(draft.characterId);
            if (!controls) return;
            const input = draft.kind === 'policy' ? controls.checkbox : controls.input;
            if (draft.kind === 'policy') input.checked = draft.value === true;
            else input.value = String(draft.value);
            controls.status.textContent = draft.message;
            input.focus();
        },
    });
    // 面板构造期间 turn 可以开始；新控件进入 DOM 后再检查一次。
    state.roleplayPanel.setDisabled(state.navigationBusy || isTurnActiveForRef(panelRef));
}

function renderAffinityBar(percent) {
    return affinityBar(percent, TavernSecurity.normalizeAffinity);
}

function renderHistory(history) {
    const stream = document.getElementById('chat-stream');
    mountMessageHistory(document, stream, history, msg => {
        if (msg.role === 'user') return buildUserMessage(msg.content, msg);
        if (msg.role === 'assistant') return buildAssistantMessage(msg.content, msg.thinking || '', msg).container;
        return null;
    });
    // 重渲染历史时，给最新一条 AI 消息补上「📋 剧情记忆」折叠面板（已有 summaries 才显示）
    const lastAI = stream.querySelector('.msg.assistant:last-of-type');
    if (lastAI) renderSummaryPanel(lastAI, state.session);
    scrollToBottom();
}

// ===== 消息追加 =====

function buildUserMessage(text, msgData = null) {
    const { container: div } = createMessageElement(document, {
        role: 'user',
        content: text,
        message: msgData || {},
    });
    bindMessageActions(div);
    return div;
}

function appendUserMessage(text, msgData = null) {
    const stream = document.getElementById('chat-stream');
    const div = buildUserMessage(text, msgData);
    stream.appendChild(div);
    scrollToBottom();
}

function buildAssistantMessage(content, thinking = '', msgData = null) {
    const rendered = createMessageElement(document, {
        role: 'assistant',
        content,
        message: msgData || {},
        timeText: new Date().toLocaleTimeString(),
    });
    const div = rendered.container;
    // 给 assistant 节点分配唯一 id，便于 SSE/regenerate 精确锁定目标（兜底 :last-child 选择器）
    div.id = div.id || `msg-assistant-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    renderRoleplayWarnings(document, div, msgData);
    bindMessageActions(div);
    return rendered;
}

function appendAssistantMessage(content, thinking = '', msgData = null) {
    const stream = document.getElementById('chat-stream');
    const rendered = buildAssistantMessage(content, thinking, msgData);
    stream.appendChild(rendered.container);
    scrollToBottom();
    return rendered.contentElement;
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

function recoverDetachedMessageDraft({ messageRef, draft, error }) {
    const replacement = [...document.querySelectorAll('#chat-stream .msg')].find(
        element => element.dataset.messageId === messageRef.message_id,
    );
    const failure = `保存失败：${errorDetail(error)}`;
    if (replacement) {
        enterMessageEditMode(replacement, messageRef, { draft, error: failure });
        return true;
    }

    const stream = document.getElementById('chat-stream');
    const recovery = document.createElement('section');
    recovery.className = 'message-draft-recovery';
    recovery.setAttribute('role', 'alert');
    const label = document.createElement('strong');
    label.textContent = `${failure}；原消息已不在当前历史中，草稿保留如下：`;
    const textarea = document.createElement('textarea');
    textarea.value = draft;
    textarea.setAttribute('aria-label', '未保存的消息草稿');
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.textContent = '我已复制，关闭';
    dismiss.addEventListener('click', () => recovery.remove());
    recovery.append(label, textarea, dismiss);
    stream.appendChild(recovery);
    textarea.focus();
    scrollToBottom();
    return true;
}

function enterMessageEditMode(msgEl, messageRef, initial = {}) {
    return enterMessageEditor({
        documentRef: document,
        messageElement: msgEl,
        messageRef,
        save: editMessage,
        initialValue: initial.draft,
        initialError: initial.error,
        recover: recoverDetachedMessageDraft,
    });
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
    const requestRef = captureSessionRef();
    if (state.navigationBusy || !canPerformTurnAction('regenerate', requestRef)) return;
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

function renderParsedResponse(parsed, warningSource = parsed) {
    const stream = document.getElementById('chat-stream');
    const msgs = stream.querySelectorAll('.msg.assistant');
    const lastAssistant = msgs[msgs.length - 1];
    if (!lastAssistant) return;
    const contentEl = lastAssistant.querySelector('.content');
    const previousRoleplayWarnings = lastAssistant.querySelector('.roleplay-warning-panel');
    if (previousRoleplayWarnings) previousRoleplayWarnings.remove();
    renderRoleplayWarnings(document, lastAssistant, warningSource);
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
    const result = renderSummaryPanelView({
        document,
        anchor: lastAssistant,
        session: sess,
        selectedId: state.selectedSummaryId,
        expanded: state.summaryPanelExpanded,
        disabled: state.navigationBusy || state.isStreaming || !canPerformTurnAction('summary'),
        onSelect: summaryId => {
            state.selectedSummaryId = summaryId;
            state.summaryPanelExpanded = true;
            renderSummaryPanel(lastAssistant, state.session);
        },
        onExpandedChange: expanded => { state.summaryPanelExpanded = expanded; },
        onEdit: summary => showSummaryEditor(summary),
        onRegenerate: summary => { void regenerateSummary(summary); },
        onOpenSource: summary => {
            if (summary.source_snapshot_id) {
                void showSnapshotPreview(summary.source_snapshot_id, captureSessionRef());
            }
        },
    });
    state.selectedSummaryId = result ? result.selectedId : null;
}

function escapeHtml(str) {
    return TavernSecurity.escapeHtml(str);
}

function appendSummaryEditorField(form, summaryId, config) {
    const row = document.createElement('div');
    row.className = 'summary-edit-row';
    const id = `summary-${summaryId}-${config.field}`;
    const label = document.createElement('label');
    label.className = 'summary-edit-label';
    label.htmlFor = id;
    label.textContent = config.label;
    const input = document.createElement(config.multiline ? 'textarea' : 'input');
    input.id = id;
    input.className = 'summary-edit-input';
    input.dataset.field = config.field;
    input.value = config.value || '';
    if (config.multiline) input.rows = config.rows || 3;
    if (config.maxLength) input.maxLength = config.maxLength;
    row.appendChild(label);
    row.appendChild(input);
    form.appendChild(row);
}

function showSummaryEditor(summary) {
    if (!summary || !summary.id) return;
    if (!canPerformTurnAction('summary')) {
        showToast('当前对话正在生成，摘要暂不可编辑');
        return;
    }
    state.selectedSummaryId = summary.id;
    const form = document.createElement('div');
    form.className = 'summary-edit-form';
    const safeId = summary.id.replace(/[^a-zA-Z0-9_-]/g, '');
    appendSummaryEditorField(form, safeId, {
        field: 'text', label: '前情提要（必填，最多 2000 字）',
        value: summary.text || '', multiline: true, rows: 5, maxLength: 2000,
    });
    appendSummaryEditorField(form, safeId, {
        field: 'time', label: '时间线（最多 300 字）',
        value: summary.time || '', multiline: false, maxLength: 300,
    });
    appendSummaryEditorField(form, safeId, {
        field: 'facts', label: '关键事件（每行一条，最多 5 条）',
        value: (summary.facts || []).join('\n'), multiline: true, rows: 4,
    });
    appendSummaryEditorField(form, safeId, {
        field: 'relations', label: '角色关系（每行一条，最多 5 条）',
        value: (summary.relations || []).join('\n'), multiline: true, rows: 4,
    });
    showModal({
        title: '编辑剧情记忆',
        body: form,
        footer: {
            confirmText: '保存',
            pendingText: '保存中…',
            cancelText: '取消',
            onConfirm: async () => {
                const ref = captureSessionRef();
                await summaryService.update(ref, summary.id, summaryPatchFromForm(form));
                return true;
            },
        },
    });
}

function cancelSummaryWatchers() {
    for (const watcher of summaryWatchers.values()) watcher.cancelled = true;
    summaryWatchers.clear();
}

function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function watchSummaryGeneration(ref, summaryId, generationId) {
    const previous = summaryWatchers.get(summaryId);
    if (previous) previous.cancelled = true;
    const watcher = { cancelled: false };
    summaryWatchers.set(summaryId, watcher);
    const deadline = Date.now() + 180000;
    try {
        while (!watcher.cancelled && isCurrentSessionRef(ref)) {
            await delay(400);
            if (watcher.cancelled || !isCurrentSessionRef(ref)) return;
            const fresh = await fetchSession(ref);
            if (watcher.cancelled || !isCurrentSessionRef(ref)) return;
            commitSummaryRefresh(fresh, ref);
            const summary = (fresh.summaries || []).find(item => item.id === summaryId);
            if (!summary || summary.generation_id !== generationId) return;
            if (summary.status !== 'pending' || summary.generation_active === false) return;
            if (Date.now() >= deadline) {
                showToast('摘要仍在后台生成，可稍后查看状态', 3500);
                return;
            }
        }
    } catch (error) {
        if (!watcher.cancelled && isCurrentSessionRef(ref)) {
            showToast(`刷新摘要状态失败：${errorDetail(error)}`, 3500);
        }
    } finally {
        if (summaryWatchers.get(summaryId) === watcher) summaryWatchers.delete(summaryId);
    }
}

function watchPendingSummaries(ref, session) {
    for (const summary of (session && session.summaries) || []) {
        if (
            !summary
            || summary.status !== 'pending'
            || typeof summary.id !== 'string'
            || typeof summary.generation_id !== 'string'
            || summary.generation_active === false
            || summaryWatchers.has(summary.id)
        ) continue;
        void watchSummaryGeneration(ref, summary.id, summary.generation_id);
    }
}

async function regenerateSummary(summary) {
    if (!summary || !summary.id) return;
    if (!canPerformTurnAction('summary')) {
        showToast('当前对话正在生成，摘要暂不可重生成');
        return;
    }
    const action = summary.status === 'failed'
        || (summary.status === 'pending' && summary.generation_active === false)
        ? '重试生成'
        : '重新生成';
    if (!confirm(`${action}第 ${((state.session.summaries || []).findIndex(item => item.id === summary.id) + 1)} 段剧情记忆？这会调用一次本地模型。`)) return;
    const ref = captureSessionRef();
    state.selectedSummaryId = summary.id;
    try {
        const accepted = await summaryService.regenerate(ref, summary.id);
        if (accepted && accepted.generation_id && !summaryWatchers.has(summary.id)) {
            void watchSummaryGeneration(ref, summary.id, accepted.generation_id);
        }
    } catch (error) {
        showToast(`${action}失败：${errorDetail(error)}`, 3500);
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

function readPersistedTurnPointer() {
    try { return turnPersistence.read(); }
    catch (_error) { return null; }
}

function persistActiveTurn(turn = state.activeTurn) {
    if (!turn || !turn.turnId || !turn.ref) return;
    try { turnPersistence.write(turn); } catch (_error) {}
}

function clearPersistedTurn() {
    try { turnPersistence.clear(); } catch (_error) {}
}

function setTurnUiState(active, cancelling = false, syncPending = false) {
    state.isStreaming = active;
    const sendBtn = document.getElementById('send-btn');
    const cancelBtn = document.getElementById('send-cancel-btn');
    if (sendBtn) {
        sendBtn.disabled = active;
        sendBtn.classList.toggle('hidden', active);
        sendBtn.setAttribute('aria-disabled', String(active));
    }
    if (cancelBtn) {
        const waitingForSession = syncPending || Boolean(state.activeTurn && state.activeTurn.syncPending);
        cancelBtn.disabled = !active || cancelling || waitingForSession;
        cancelBtn.classList.toggle('hidden', !active);
        cancelBtn.setAttribute('aria-busy', String(cancelling || waitingForSession));
        cancelBtn.textContent = waitingForSession ? '正在同步存档…' : cancelling ? '正在取消…' : '⏹ 取消';
    }

    const selectors = [
        '#project-btn', '#tab-world', '#tab-saves', '#model-select', '#reset-btn',
        '#save-new-inline', '#save-rename-inline', '#save-delete-inline',
        '#save-import-inline', '#history-btn',
        '.msg-action-btn', '.msg-checkbox',
        '.history-restore', '.ce-save', '.ce-delete',
        '.worldbook-write-control',
        '.roleplay-write-control',
    ];
    document.querySelectorAll(selectors.join(',')).forEach(element => {
        if ('disabled' in element) element.disabled = active;
        element.setAttribute('aria-disabled', String(active));
    });
    if (state.roleplayPanel) state.roleplayPanel.setDisabled(active || state.navigationBusy);
    const lastAI = document.querySelector('#chat-stream .msg.assistant:last-of-type');
    if (lastAI && state.session) renderSummaryPanel(lastAI, state.session);
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

function sameSessionRef(left, right) {
    return Boolean(left && right
        && left.project === right.project
        && left.save === right.save
        && left.epoch === right.epoch);
}

function activeTurnSnapshot(turn = state.activeTurn) {
    if (!turn) return null;
    return Object.freeze({
        turnId: turn.turnId,
        ref: turn.ref,
        targetEl: turn.targetEl || null,
        content: turn.content || '',
        thinking: turn.thinking || '',
        terminal: Boolean(turn.terminal),
    });
}

function renderTurnSnapshot(snapshot) {
    if (!snapshot) return;
    if (snapshot.targetEl && snapshot.targetEl.isConnected) {
        snapshot.targetEl.textContent = snapshot.content || (snapshot.terminal ? '' : '（生成中…）');
    }
    const thinking = document.getElementById('thinking-content');
    const panel = document.getElementById('thinking-panel');
    if (thinking) thinking.textContent = snapshot.thinking;
    if (panel) panel.classList.toggle('hidden', !snapshot.thinking);
    const stream = document.getElementById('chat-stream');
    if (stream) stream.scrollTop = stream.scrollHeight;
}

function ensureActiveTurnFrame() {
    if (activeFrameRenderer) return activeFrameRenderer;
    activeFrameRenderer = createFrameRenderer({
        requestFrame: callback => requestAnimationFrame(callback),
        cancelFrame: frameId => cancelAnimationFrame(frameId),
        isCurrent: snapshot => {
            const active = state.activeTurn;
            return Boolean(active
                && active.turnId === snapshot.turnId
                && active.targetEl === snapshot.targetEl
                && sameSessionRef(active.ref, snapshot.ref)
                && isCurrentSessionRef(snapshot.ref));
        },
        render: renderTurnSnapshot,
    });
    return activeFrameRenderer;
}

function queueActiveTurnFrame(turn = state.activeTurn) {
    const snapshot = activeTurnSnapshot(turn);
    if (snapshot) ensureActiveTurnFrame().enqueue(snapshot);
}

function flushActiveTurnFrame() {
    return activeFrameRenderer ? activeFrameRenderer.flush() : false;
}

function cancelActiveTurnFrame(options = {}) {
    if (!activeFrameRenderer) return false;
    const committed = activeFrameRenderer.cancel(options);
    activeFrameRenderer = null;
    return committed;
}

function renderActiveTurnBuffer(turn = state.activeTurn) {
    renderTurnSnapshot(activeTurnSnapshot(turn));
}

function installActiveTurn(turn, ref, options = {}) {
    cancelActiveTurnFrame({ flush: true });
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
    if (base.promptDiagnostics) rememberWorldbookDiagnostics(ref, base.promptDiagnostics);
    ensureActiveTurnFrame();
    persistActiveTurn();
    setTurnUiState(true, false, base.terminal);
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
        queueActiveTurnFrame();
    } else if (event.type === 'parsed') {
        flushActiveTurnFrame();
        const parsedEvent = state.activeTurn.parsed;
        if (parsedEvent.legacySession) applySessionResult(parsedEvent.legacySession, active.ref);
        if (isCurrentSessionRef(active.ref)) renderParsedResponse(parsedEvent.parsed, parsedEvent);
        setTurnUiState(true, state.activeTurn.cancelling);
    } else if (event.type === 'terminal') {
        flushActiveTurnFrame();
        cancelActiveTurnFrame();
        setTurnUiState(true, false, true);
    }
    return true;
}

function updateActiveTurnFromMeta(meta) {
    const active = state.activeTurn;
    if (!active || active.turnId !== meta.turn_id) return false;
    const terminal = TERMINAL_STATUSES.includes(meta.status);
    const promptDiagnostics = meta.prompt_diagnostics
        && typeof meta.prompt_diagnostics === 'object'
        && !Array.isArray(meta.prompt_diagnostics)
        ? meta.prompt_diagnostics
        : active.promptDiagnostics;
    if (terminal) flushActiveTurnFrame();
    state.activeTurn = {
        ...active,
        status: meta.status,
        content: terminal && typeof meta.content === 'string' ? meta.content : active.content,
        thinking: terminal && typeof meta.thinking === 'string' ? meta.thinking : active.thinking,
        error: meta.error || null,
        promptDiagnostics,
        terminal,
        terminalCount: terminal ? Math.max(1, active.terminalCount) : active.terminalCount,
        syncPending: terminal ? true : active.syncPending,
    };
    if (promptDiagnostics) rememberWorldbookDiagnostics(active.ref, promptDiagnostics);
    persistActiveTurn();
    renderActiveTurnBuffer();
    if (terminal) {
        cancelActiveTurnFrame();
        setTurnUiState(true, false, true);
    }
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
    let active = state.activeTurn;
    if (!active || active.turnId !== turnId || !active.terminal) return false;
    if (!active.syncPending) {
        state.activeTurn = { ...active, syncPending: true };
        active = state.activeTurn;
    }
    persistActiveTurn(active);
    setTurnUiState(true, Boolean(active.cancelling), true);
    flushActiveTurnFrame();
    cancelActiveTurnFrame();
    try {
        const reloaded = await reloadCurrentSession(active.ref);
        if (!reloaded) throw new ApiError('终态存档刷新未提交', { code: 'terminal_reload_stale' });
    } catch (error) {
        if (state.activeTurn && state.activeTurn.turnId === turnId) {
            state.activeTurn = { ...state.activeTurn, syncPending: true };
            persistActiveTurn();
            setTurnUiState(true, false, true);
        }
        showToast(`turn 已终止，但刷新存档失败：${errorDetail(error)}；刷新页面可重试同步`, 5000);
        return false;
    }
    const current = state.activeTurn;
    if (!current || current.turnId !== turnId) return false;
    active = current;
    if (active.parsed && isCurrentSessionRef(active.ref)) {
        try { renderParsedResponse(active.parsed.parsed, active.parsed); }
        catch (error) { console.warn('结构化响应渲染失败，已保留刷新后的存档', error); }
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
    flushActiveTurnFrame();
    cancelActiveTurnFrame();
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
    return createTurnPayload({
        ref: requestRef,
        revision: currentRevision(requestRef),
        model: state.session.current_model || document.getElementById('model-select').value || null,
        params: state.modelParams,
        userInput,
    });
}

function prepareProvisionalTurn(requestRef) {
    cancelActiveTurnFrame({ flush: true });
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
        cancelActiveTurnFrame();
        setTurnUiState(false, false);
        showToast(`${failureLabel}失败：${errorDetail(error)}`, 4000);
    }
}

async function sendMessage(text = null) {
    const input = document.getElementById('user-input');
    const userText = text !== null ? String(text).trim() : input.value.trim();
    const requestRef = captureSessionRef();
    if (!userText || state.navigationBusy || !canPerformTurnAction('send', requestRef)) return;
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

// ===== 角色 / 世界书 / 用户档案 编辑器 =====

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
    if (!canPerformTurnAction('card_write', editorRef)) {
        showToast('当前存档正在生成，请先取消或等待完成');
        return;
    }
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
    if (!canPerformTurnAction('card_write', editorRef)) {
        showToast('加载角色卡期间已开始生成，请等待完成后重试');
        return;
    }

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
    let fieldIdCounter = 0;

    // ===== 列表渲染（仅 allowNew）=====
    function syncCardListSelection() {
        if (!listEl) return;
        const newButton = listEl.querySelector('#ce-new');
        if (newButton) {
            const selected = currentItem === null;
            newButton.classList.toggle('active', selected);
            newButton.setAttribute('aria-current', selected ? 'true' : 'false');
        }
        listEl.querySelectorAll('.card-row').forEach(row => {
            const selected = Boolean(
                currentItem && currentItem[config.idField] === row.dataset.id
            );
            row.classList.toggle('active', selected);
            row.setAttribute('aria-current', selected ? 'true' : 'false');
        });
    }

    function renderList() {
        if (!listEl) return;
        listEl.innerHTML = `
            <button type="button" class="modal-btn new-card-btn" id="ce-new">＋ 新建</button>
            ${items.map(it => `
                <button type="button" class="card-row ${currentItem && currentItem[config.idField] === it[config.idField] ? 'active' : ''}" data-id="${escapeHtml(it[config.idField])}" aria-label="编辑卡片 ${escapeHtml(it.name || it[config.idField] || it.id || '')}">
                    <span class="card-row-name">${escapeHtml(it.name || it[config.idField] || it.id || '')}</span>
                </button>`).join('')}`;
        syncCardListSelection();
        listEl.querySelector('#ce-new').addEventListener('click', () => {
            currentItem = null;
            syncCardListSelection();
            renderForm();
        });
        listEl.querySelectorAll('.card-row').forEach(row => {
            row.addEventListener('click', () => {
                currentItem = items.find(it => it[config.idField] === row.dataset.id) || null;
                syncCardListSelection();
                renderForm();
            });
        });
    }

    // ===== 字段行 =====
    function makeFieldRow(fd) {
        const row = document.createElement('div');
        row.className = 'fld-row';
        row.dataset.key = fd.key;
        row.dataset.type = fd.type;
        row.dataset.label = fd.label || fd.key || '字段';
        if (fd.array) row.dataset.array = '1';
        if (fd.required) row.dataset.required = '1';
        const integerField = fd.type === 'integer' || fd.integer === true || fd.key === 'chattiness';
        if (integerField) row.dataset.integer = '1';
        if (fd.min !== undefined) row.dataset.min = String(fd.min);
        if (fd.max !== undefined) row.dataset.max = String(fd.max);
        const fieldId = `ce-field-${++fieldIdCounter}`;

        const handle = document.createElement('span');
        handle.className = 'fld-handle';
        handle.title = '拖拽排序'; handle.textContent = '⠿'; handle.draggable = true;
        row.appendChild(handle);

        const body = document.createElement('div'); body.className = 'fld-body';

        if (fd.type === 'custom') {
            const keyWrap = document.createElement('div'); keyWrap.className = 'fld-cell grow';
            const keyLbl = document.createElement('label'); keyLbl.textContent = '字段名'; keyLbl.htmlFor = `${fieldId}-key`;
            const keyInp = document.createElement('input'); keyInp.type = 'text'; keyInp.className = 'ce-field-key';
            keyInp.id = `${fieldId}-key`;
            keyInp.placeholder = '例：所属势力'; keyInp.value = fd.key || '';
            keyWrap.appendChild(keyLbl); keyWrap.appendChild(keyInp); body.appendChild(keyWrap);
            const valWrap = document.createElement('div'); valWrap.className = 'fld-cell grow';
            const valLbl = document.createElement('label'); valLbl.textContent = '值'; valLbl.htmlFor = `${fieldId}-value`;
            const valInp = document.createElement('input'); valInp.type = 'text'; valInp.className = 'ce-field-val';
            valInp.id = `${fieldId}-value`;
            valInp.placeholder = '例：青龙商会'; valInp.value = fd.value || '';
            valWrap.appendChild(valLbl); valWrap.appendChild(valInp); body.appendChild(valWrap);
        } else if (fd.type === 'checkbox') {
            const lbl = document.createElement('label');
            lbl.htmlFor = fieldId;
            lbl.style.cssText = 'font-size:12px;color:var(--text);cursor:pointer;display:flex;align-items:center;gap:6px';
            const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'ce-field-val';
            cb.id = fieldId;
            cb.checked = getPathValue(currentItem || {}, fd.key, fd);
            lbl.appendChild(cb); lbl.appendChild(document.createTextNode(fd.checkboxLabel || fd.label));
            body.appendChild(lbl);
        } else if (fd.type === 'textarea') {
            const lbl = document.createElement('label'); lbl.textContent = fd.label; lbl.className = 'fld-label'; lbl.htmlFor = fieldId; body.appendChild(lbl);
            const ta = document.createElement('textarea'); ta.className = 'ce-field-val'; ta.rows = fd.rows || 4;
            ta.id = fieldId;
            if (fd.placeholder) ta.placeholder = fd.placeholder;
            ta.value = getPathValue(currentItem || {}, fd.key, fd); body.appendChild(ta);
        } else if (fd.type === 'number' || fd.type === 'integer') {
            const lbl = document.createElement('label'); lbl.textContent = fd.label; lbl.className = 'fld-label'; lbl.htmlFor = fieldId; body.appendChild(lbl);
            const inp = document.createElement('input'); inp.type = 'number'; inp.className = 'ce-field-val';
            inp.id = fieldId;
            if (fd.min !== undefined) inp.min = fd.min; if (fd.max !== undefined) inp.max = fd.max;
            if (integerField) inp.step = '1';
            inp.value = getPathValue(currentItem || {}, fd.key, fd); body.appendChild(inp);
        } else {
            const lbl = document.createElement('label'); lbl.textContent = fd.label; lbl.className = 'fld-label'; lbl.htmlFor = fieldId; body.appendChild(lbl);
            const inp = document.createElement('input'); inp.type = 'text'; inp.className = 'ce-field-val';
            inp.id = fieldId;
            if (fd.placeholder) inp.placeholder = fd.placeholder;
            inp.value = getPathValue(currentItem || {}, fd.key, fd); body.appendChild(inp);
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
            clearDragState(groupsEl);
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
            if (!canPerformTurnAction('card_write', editorRef)) {
                statusEl.textContent = '✗ 当前存档正在生成，请先取消或等待完成';
                return;
            }
            saveButton.disabled = true;
            statusEl.textContent = '保存中…';
            try {
                const collected = collectCardEditorData(groupsEl, { idField: config.idField });
                const data = mergeCardEditorData(currentItem || extraData || {}, collected.data);
                const idVal = collected.idValue;
                if (config.idField && !idVal) throw new Error(`请填 ${config.idLabel || 'ID'}`);
                const saveUrl = config.saveApi ? config.saveApi(idVal || 'user') : null;
                if (!saveUrl) throw new Error('缺少保存 API');
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
                const invalidField = e && e.field
                    ? groupsEl.querySelector(`.fld-row[data-key="${CSS.escape(e.field)}"] .ce-field-val`)
                    : null;
                if (invalidField) {
                    invalidField.setAttribute('aria-invalid', 'true');
                    invalidField.focus();
                } else if (saveButton.isConnected) {
                    saveButton.focus();
                }
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
                if (!canPerformTurnAction('card_write', editorRef)) {
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
            const groups = schema.groups.map(g => ({
                ...g,
                fields: g.fields.map(f => ({
                    ...f,
                    ...(f.key === 'chattiness' ? {
                        default: f.default === undefined ? 50 : f.default,
                        min: f.min === undefined ? 0 : f.min,
                        max: f.max === undefined ? 100 : f.max,
                        integer: true,
                    } : {}),
                    builtin: true,
                })),
            }));
            return groupsWithCustomFields(groups, c, schema.customGroup);
        },
        postSave: async () => { showToast('角色已保存，点「重置」让新角色进场景'); },
    });
}

async function openWorldbookEditor() {
    const editorRef = captureSessionRef();
    if (state.navigationBusy || !sessionBelongsToRef(state.session, editorRef)) {
        showToast('当前存档尚未加载完成');
        return;
    }
    if (!canPerformTurnAction('card_write', editorRef)) {
        showToast('当前存档正在生成，请先取消或等待完成');
        return;
    }
    let entries;
    let diagnostics;
    let schema;
    try {
        [schema, entries, diagnostics] = await Promise.all([
            worldbookService.schema(),
            worldbookService.list(editorRef.project),
            loadLatestWorldbookDiagnostics(editorRef),
        ]);
    } catch (error) {
        showToast(`加载世界书失败：${errorDetail(error)}`, 3500);
        return;
    }
    if (!isCurrentSessionRef(editorRef) || !sessionBelongsToRef(state.session, editorRef)) return;
    // 加载期间 turn 可能已启动；DOM 尚不存在时全局禁用器无法覆盖新控件，
    // 因此在构造编辑器前再次封闭竞态窗口。
    if (!canPerformTurnAction('card_write', editorRef)) {
        showToast('加载世界书期间已开始生成，请等待完成后重试');
        return;
    }

    const editor = createWorldbookEditor({
        documentRef: document,
        schema,
        entries,
        manualIds: state.session.manual_worldbook_ids || [],
        diagnostics,
        disabled: false,
        onPendingChange: pending => modalController.setPending(pending),
        confirmDelete: entryId => confirm(`确定删除世界书条目「${entryId}」吗？删除后将移入回收区，可以恢复。`),
        onSaveEntry: async (data, context) => {
            if (!isCurrentSessionRef(editorRef)) throw new Error('当前项目或存档已切换，请重新打开编辑器');
            if (!canPerformTurnAction('card_write', editorRef)) {
                throw new Error('当前存档正在生成，请先取消或等待完成');
            }
            if (!context.isNew && context.originalId !== data.id) {
                throw new Error('已有世界书 ID 不可直接修改');
            }
            await worldbookService.save(editorRef.project, data.id, data);
            if (!isCurrentSessionRef(editorRef)) throw new Error('保存完成，但当前存档已切换');
            return worldbookService.list(editorRef.project);
        },
        onDeleteEntry: async entryId => {
            if (!isCurrentSessionRef(editorRef)) throw new Error('当前项目或存档已切换，请重新打开编辑器');
            if (!canPerformTurnAction('card_write', editorRef)) {
                throw new Error('当前存档正在生成，请先取消或等待完成');
            }
            const result = await worldbookService.remove(editorRef.project, entryId);
            if (!isCurrentSessionRef(editorRef)) throw new Error('删除完成，但当前存档已切换');
            const returnedSession = sessionFromResult(result);
            if (returnedSession && sessionBelongsToRef(returnedSession, editorRef)) {
                commitSessionState(returnedSession, editorRef);
            }
            return worldbookService.list(editorRef.project);
        },
        onSaveManual: async entryIds => {
            if (!isCurrentSessionRef(editorRef)) throw new Error('当前项目或存档已切换，请重新打开编辑器');
            return worldbookService.saveManual(editorRef, entryIds);
        },
    });
    showModal({ title: '📖 世界书 — 触发与预算', body: editor.root });
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
            const groups = [
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
            return groupsWithCustomFields(groups, ud, { key: '_custom', label: '自定义字段' });
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
        const next = {
            temperature: Number(body.querySelector('#p-temp').value),
            top_p: Number(body.querySelector('#p-topp').value),
            top_k: Number(body.querySelector('#p-topk').value),
            num_predict: Number(body.querySelector('#p-nump').value),
            think: body.querySelector('#p-think').checked,
        };
        await saveSettings(next);
        state.modelParams = next;
        return true;
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
        const data = await promptService.load();
        const body = document.createElement('div');
        const drafts = {
            system: data.system || '',
            group_chat: data.group_chat || '',
            summary: data.summary || '',
        };
        const tabConfig = [
            ['system', '系统提示'],
            ['group_chat', '群聊模板'],
            ['summary', '摘要模板'],
        ];
        const tabsContainer = document.createElement('div');
        tabsContainer.className = 'modal-tabs';
        tabsContainer.setAttribute('role', 'tablist');
        tabsContainer.setAttribute('aria-label', '提示词类型');
        const editorRow = document.createElement('div');
        editorRow.className = 'form-row prompt-editor-row';
        const editorLabel = document.createElement('label');
        editorLabel.htmlFor = 'prompt-textarea';
        editorLabel.className = 'sr-only';
        editorLabel.textContent = '提示词内容';
        const textarea = document.createElement('textarea');
        textarea.id = 'prompt-textarea';
        textarea.rows = 18;
        textarea.value = drafts.system;
        editorRow.appendChild(editorLabel);
        editorRow.appendChild(textarea);
        const editorPanel = document.createElement('div');
        editorPanel.id = 'prompt-editor-panel';
        editorPanel.setAttribute('role', 'tabpanel');
        editorPanel.setAttribute('aria-labelledby', 'prompt-tab-system');
        editorPanel.appendChild(editorRow);

        const actions = document.createElement('div');
        actions.className = 'form-actions prompt-editor-actions';
        const saveButton = document.createElement('button');
        saveButton.type = 'button';
        saveButton.className = 'modal-btn';
        saveButton.id = 'prompt-save';
        saveButton.textContent = '保存';
        const resetButton = document.createElement('button');
        resetButton.type = 'button';
        resetButton.className = 'modal-btn';
        resetButton.id = 'prompt-reset';
        resetButton.textContent = '恢复默认';
        const statusEl = document.createElement('span');
        statusEl.id = 'prompt-status';
        statusEl.className = 'prompt-status';
        statusEl.setAttribute('role', 'status');
        statusEl.setAttribute('aria-live', 'polite');
        actions.appendChild(saveButton);
        actions.appendChild(resetButton);
        actions.appendChild(statusEl);
        body.appendChild(tabsContainer);
        body.appendChild(editorPanel);
        body.appendChild(actions);
        showModal({ title: '编辑提示词', body });

        let currentTab = 'system';
        let pending = false;
        const tabs = [];

        function setPromptPending(value) {
            pending = Boolean(value);
            modalController.setPending(pending);
            textarea.disabled = pending;
            saveButton.disabled = pending;
            resetButton.disabled = pending;
            tabs.forEach(tab => { tab.disabled = pending; });
        }

        function activateTab(name) {
            if (pending || name === currentTab) return;
            drafts[currentTab] = textarea.value;
            currentTab = name;
            textarea.value = drafts[currentTab];
            editorPanel.setAttribute('aria-labelledby', `prompt-tab-${currentTab}`);
            tabs.forEach(tab => {
                const active = tab.dataset.tab === currentTab;
                tab.classList.toggle('active', active);
                tab.setAttribute('aria-selected', String(active));
                tab.tabIndex = active ? 0 : -1;
            });
        }

        tabConfig.forEach(([name, label], index) => {
            const tab = document.createElement('button');
            tab.type = 'button';
            tab.id = `prompt-tab-${name}`;
            tab.className = `modal-tab${index === 0 ? ' active' : ''}`;
            tab.dataset.tab = name;
            tab.textContent = label;
            tab.setAttribute('role', 'tab');
            tab.setAttribute('aria-selected', String(index === 0));
            tab.setAttribute('aria-controls', 'prompt-editor-panel');
            tab.tabIndex = index === 0 ? 0 : -1;
            tab.addEventListener('click', () => activateTab(name));
            tab.addEventListener('keydown', event => {
                if (pending) return;
                const currentIndex = tabs.indexOf(tab);
                const targetIndex = promptTabTargetIndex(
                    event.key,
                    currentIndex,
                    tabConfig.length,
                );
                if (targetIndex === null) return;
                event.preventDefault();
                const target = tabs[targetIndex];
                if (!target) return;
                activateTab(target.dataset.tab);
                target.focus();
            });
            tabs.push(tab);
            tabsContainer.appendChild(tab);
        });

        saveButton.addEventListener('click', async () => {
            if (pending) return;
            drafts[currentTab] = textarea.value;
            statusEl.textContent = '保存中…';
            setPromptPending(true);
            try {
                await promptService.save(currentTab, drafts[currentTab]);
                statusEl.textContent = '✓ 已保存，下次对话生效';
                setTimeout(() => { statusEl.textContent = ''; }, 3000);
            } catch (error) {
                statusEl.textContent = '✗ ' + errorDetail(error);
            } finally {
                setPromptPending(false);
                textarea.focus();
            }
        });

        resetButton.addEventListener('click', async () => {
            if (pending) return;
            if (!confirm('恢复为默认提示词？当前编辑内容会丢失。')) return;
            statusEl.textContent = '恢复中…';
            setPromptPending(true);
            try {
                const fresh = await promptService.reset(currentTab);
                drafts[currentTab] = fresh[currentTab] || '';
                textarea.value = drafts[currentTab];
                statusEl.textContent = '✓ 已恢复默认'; setTimeout(() => { statusEl.textContent = ''; }, 3000);
            } catch (error) {
                statusEl.textContent = '✗ ' + errorDetail(error);
            } finally {
                setPromptPending(false);
                textarea.focus();
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
    projectListboxController = createListboxController({
        documentRef: document,
        trigger: projectBtn,
        panel: projectDropdown,
        listbox: document.getElementById('project-list'),
        optionSelector: '.dropdown-item[data-project]',
        isSelected: option => option.dataset.project === state.currentProject,
        canOpen: () => !state.navigationBusy && !isTurnActiveForRef(committedSessionRef),
        onBlocked: () => showToast('当前正在切换或生成，请稍候'),
        beforeOpen: hideAllDropdowns,
        position: positionDropdown,
        onSelect: option => { void switchProject(option.dataset.project); },
    });

    // 项目下拉：新建项目
    document.getElementById('project-new-inline').addEventListener('click', () => {
        projectListboxController.close({ restoreFocus: true });
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
    saveListboxController = createListboxController({
        documentRef: document,
        trigger: saveTab,
        panel: saveDropdown,
        listbox: document.getElementById('save-list'),
        optionSelector: '.dropdown-item[data-save]',
        isSelected: option => option.dataset.save === state.currentSave,
        canOpen: () => !state.navigationBusy && !isTurnActiveForRef(committedSessionRef),
        onBlocked: () => showToast('当前正在切换或生成，请稍候'),
        beforeOpen: hideAllDropdowns,
        position: positionDropdown,
        onSelect: option => { void switchSave(option.dataset.save); },
    });

    // 存档下拉内的 5 个操作按钮
    const runSaveDropdownAction = action => {
        saveListboxController.close({ restoreFocus: true });
        hideAllDropdowns();
        action();
    };
    document.getElementById('save-new-inline').addEventListener('click', () => runSaveDropdownAction(promptForNewSave));
    document.getElementById('save-rename-inline').addEventListener('click', () => runSaveDropdownAction(promptForRenameSave));
    document.getElementById('save-delete-inline').addEventListener('click', () => runSaveDropdownAction(deleteCurrentSave));
    document.getElementById('save-export-inline').addEventListener('click', () => runSaveDropdownAction(exportCurrentSave));
    document.getElementById('save-import-inline').addEventListener('click', () => runSaveDropdownAction(promptForImportSave));

    // 点击空白处关闭所有下拉
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.dropdown-panel') && !e.target.closest('#project-btn') && !e.target.closest('#tab-saves')) {
            hideAllDropdowns();
        }
    });

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

}

// ===== 新顶栏所需的弹窗辅助函数 =====

function promptForNewProject() {
    const input = document.createElement('input');
    input.type = 'text'; input.placeholder = '新项目名（如：修仙世界）';
    input.style.cssText = 'width:100%;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text)';
    showModal({ title: '新建项目（世界观）', body: input, footer: { confirmText: '创建', onConfirm: async () => {
        const name = input.value.trim();
        if (!name) return false;
        return Boolean(await createNewProject(name));
    }}});
    setTimeout(() => input.focus(), 100);
}

function promptForNewSave() {
    const input = document.createElement('input');
    input.type = 'text'; input.placeholder = '存档名（如：主线剧情 / 支线A）';
    input.style.cssText = 'width:100%;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text)';
    showModal({ title: '新建存档', body: input, footer: { confirmText: '创建', onConfirm: async () => {
        const name = input.value.trim() || '新存档';
        return Boolean(await createNewSave(name));
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
        if (!name) return false;
        return Boolean(await renameCurrentSave(name));
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
