// 本地酒馆 — 前端逻辑 v2（项目+存档双层架构）
// 流式对话、角色卡渲染、行动建议、会话管理、提示词编辑

const API = {
    models: '/api/models',
    characters: '/api/characters',
    user: '/api/user',
    worldbook: '/api/worldbook',
    session: '/api/session',
    chat: '/api/chat',
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
    modelParams: {
        temperature: 0.8,
        top_p: 0.9,
        top_k: 40,
        num_predict: 4096,
        think: true,
    },
};

let activeController = null;   // 当前流式请求的 AbortController，用于中途取消

// ===== 项目列表 =====
async function loadProjects() {
    try {
        const res = await fetch(API.projects);
        const data = await res.json();
        state.projectList = data.projects || [];
        if (state.projectList.length === 0) state.projectList = ['默认项目'];

        // 兼容旧 select（hidden 但仍存在）
        const oldSelect = document.getElementById('project-select');
        if (oldSelect) {
            oldSelect.innerHTML = '';
            state.projectList.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p; opt.textContent = p;
                oldSelect.appendChild(opt);
            });
            oldSelect.value = state.currentProject;
        }
        // 渲染新顶栏的项目下拉
        await renderProjectDropdown();
    } catch (e) {
        console.warn('加载项目失败', e);
    }
}

// ===== 项目下拉渲染（含元信息） =====
async function renderProjectDropdown() {
    const listEl = document.getElementById('project-list');
    if (!listEl) return;

    // 并发拉每个项目的 stats（角色/世界书/存档数）
    const stats = {};
    await Promise.all(state.projectList.map(async (p) => {
        try {
            const [c, w, s] = await Promise.all([
                fetch(`${API.characters}?project=${encodeURIComponent(p)}`).then(r => r.json()).catch(() => ({characters:[]})),
                fetch(`${API.worldbook}?project=${encodeURIComponent(p)}`).then(r => r.json()).catch(() => ({entries:[]})),
                fetch(`${API.sessions}?project=${encodeURIComponent(p)}`).then(r => r.json()).catch(() => ({sessions:[]})),
            ]);
            stats[p] = {
                chars: (c.characters || []).length,
                world: (w.entries || []).length,
                saves: (s.sessions || []).length,
            };
        } catch { stats[p] = { chars: 0, world: 0, saves: 0 }; }
    }));

    listEl.innerHTML = state.projectList.map(p => {
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
        await loadModels();
        await loadProjects();
        // 优先用 URL hash 指定的项目；其次用「默认项目」字面量；最后才 fallback 到 projectList[0]
        const hash = window.location.hash.replace('#', '');
        if (hash && state.projectList.includes(hash)) state.currentProject = hash;
        else if (state.projectList.includes('默认项目')) state.currentProject = '默认项目';
        else if (state.projectList.length > 0) state.currentProject = state.projectList[0];
        // 刷新项目下拉（让当前项目高亮 + 显示元信息）
        await renderProjectDropdown();
        // 同步旧 select（兜底）
        const oldProjSel = document.getElementById('project-select');
        if (oldProjSel) oldProjSel.value = state.currentProject;

        await loadUser();
        await loadCharacters();
        await loadSettings();
        await loadSaveList();
        await loadOrCreateCurrentSave();
        bindUI();
        showToast(`已进入「${state.currentProject}」`, 1500);
    } catch (e) {
        console.error('初始化失败', e);
        alert('初始化失败：' + e.message + '\n请确认 server.py 已启动');
    }
}

async function loadSettings() {
    try {
        const res = await fetch(API.settings);
        if (!res.ok) return;
        const s = await res.json();
        if (s.temperature !== undefined) state.modelParams.temperature = s.temperature;
        if (s.top_p !== undefined) state.modelParams.top_p = s.top_p;
        if (s.top_k !== undefined) state.modelParams.top_k = s.top_k;
        if (s.num_predict !== undefined) state.modelParams.num_predict = s.num_predict;
        if (s.think !== undefined) state.modelParams.think = s.think;
    } catch (e) { console.warn('读取设置失败', e); }
}

async function saveSettings() {
    try {
        await fetch(API.settings, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ data: state.modelParams }),
        });
    } catch (e) { console.warn('保存设置失败', e); }
}

async function loadModels() {
    const res = await fetch(API.models);
    const data = await res.json();
    const select = document.getElementById('model-select');
    select.innerHTML = '';
    data.models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        select.appendChild(opt);
    });
}

async function loadUser() {
    await fetch(`${API.user}?project=${encodeURIComponent(state.currentProject)}`);
}

async function loadCharacters() {
    const res = await fetch(`${API.characters}?project=${encodeURIComponent(state.currentProject)}`);
    const data = await res.json();
    state.characters = data.characters || [];
}

// ===== 存档管理 =====

async function loadSaveList() {
    const res = await fetch(`${API.sessions}?project=${encodeURIComponent(state.currentProject)}`);
    const data = await res.json();
    state.saveList = data.sessions || [];

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
    if (state.saveList.length === 0) {
        const res = await fetch(API.sessionCreate, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: state.currentProject, name: '默认存档' }),
        });
        const session = await res.json();
        state.currentSave = session.session_id;
        state.session = session;
        await loadSaveList();
    } else {
        state.currentSave = state.saveList[0].session_id;
        await loadCurrentSession();
    }
    document.getElementById('save-select').value = state.currentSave;
    renderSession(state.session);
    renderHistory(state.session.message_history || []);
}

async function loadCurrentSession() {
    const url = `${API.session}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    const res = await fetch(url);
    state.session = await res.json();
}

async function switchSave(newSaveId) {
    if (!newSaveId || newSaveId === state.currentSave) return;
    if (state.isStreaming && activeController) activeController.abort();
    state.currentSave = newSaveId;
    await loadCurrentSession();
    renderSession(state.session);
    renderHistory(state.session.message_history || []);
    if (state.session.current_model) {
        document.getElementById('model-select').value = state.session.current_model;
    }
    document.getElementById('suggestions').innerHTML = '';
    document.getElementById('thinking-panel').classList.add('hidden');
}

async function createNewSave(name) {
    const res = await fetch(API.sessionCreate, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: state.currentProject, name }),
    });
    const session = await res.json();
    await loadSaveList();
    state.currentSave = session.session_id;
    state.session = session;
    document.getElementById('save-select').value = state.currentSave;
    renderSession(session);
    renderHistory([]);
    document.getElementById('suggestions').innerHTML = '';
}

async function renameCurrentSave(newName) {
    if (!state.currentSave) return;
    const res = await fetch(API.sessionRename, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: state.currentProject, save: state.currentSave, new_name: newName }),
    });
    if (!res.ok) {
        const err = await res.json();
        alert('重命名失败：' + (err.detail || ''));
        return;
    }
    const updated = await res.json();
    state.session = updated;
    state.currentSave = updated.session_id;
    await loadSaveList();
    document.getElementById('save-select').value = state.currentSave;
}

async function deleteCurrentSave() {
    if (!state.currentSave) return;
    if (state.saveList.length <= 1) {
        alert('至少保留 1 个存档');
        return;
    }
    if (state.isStreaming && activeController) activeController.abort();
    const s = state.saveList.find(x => x.session_id === state.currentSave);
    const name = s ? s.name : state.currentSave;
    if (!confirm(`确认删除存档「${name}」？此操作不可恢复。`)) return;

    const res = await fetch(API.sessionDelete, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: state.currentProject, save: state.currentSave }),
    });
    if (!res.ok) { const err = await res.json(); alert('删除失败：' + (err.detail || '')); return; }
    await loadSaveList();
    await loadOrCreateCurrentSave();
}

async function exportCurrentSave() {
    if (!state.currentSave) return;
    const url = `${API.sessionExport}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    const res = await fetch(url);
    const data = await res.json();
    const blob = new Blob([data.json_str], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${state.session.name || state.currentSave}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
}

async function importSave(file) {
    const text = await file.text();
    const res = await fetch(API.sessionImport, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: state.currentProject, json_str: text, name: file.name.replace(/\.json$/i, '') }),
    });
    if (!res.ok) { const err = await res.json(); alert('导入失败：' + (err.detail || '')); return; }
    await loadSaveList();
    alert('导入成功');
}

async function switchProject(newProject) {
    if (!newProject || newProject === state.currentProject) return;
    // A9：切换前若还在生成中，等待流结束（防止跨项目竞态）
    if (state.isStreaming) {
        showToast('正在生成中，请稍候再切换项目');
        return;
    }
    state.currentProject = newProject;
    window.location.hash = '#' + newProject;
    document.getElementById('project-name').textContent = newProject;
    await loadUser();
    await loadCharacters();
    await loadSaveList();
    await loadOrCreateCurrentSave();
    // 视觉反馈：顶栏项目按钮高亮 + toast
    const btn = document.getElementById('project-btn');
    if (btn) {
        btn.classList.add('highlight');
        setTimeout(() => btn.classList.remove('highlight'), 700);
    }
    showToast(`已切换到「${newProject}」`);
    // 刷新元信息（角色/世界书/存档数变了）
    await renderProjectDropdown();
}

async function createNewProject(name) {
    const res = await fetch(API.projects, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
    });
    if (!res.ok) { const err = await res.json(); alert('创建失败：' + (err.detail || '')); return; }
    await loadProjects();
    await switchProject(name);
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
        const bar = renderAffinityBar(c.affinity || 0);
        return `<div class="panel-char">
            <div class="name">${escapeHtml(c.name || cid)}</div>
            <div class="affinity-bar">${bar} ${c.affinity || 0}%</div>
            ${c.mood ? `<div style="color:var(--text-dim);font-size:11px">心情: ${escapeHtml(c.mood)}</div>` : ''}
        </div>`;
    }).join('');
}

function renderAffinityBar(percent) {
    const filled = Math.round(percent / 10);
    return '█'.repeat(filled) + '░'.repeat(10 - filled);
}

function renderHistory(history) {
    const stream = document.getElementById('chat-stream');
    stream.innerHTML = '';
    history.forEach((msg, idx) => {
        if (msg.role === 'user') appendUserMessage(msg.content, idx, msg);
        else if (msg.role === 'assistant') appendAssistantMessage(msg.content, msg.thinking || '', idx, msg);
    });
    // 重渲染历史时，给最新一条 AI 消息补上「📋 剧情记忆」折叠面板（已有 summaries 才显示）
    const lastAI = stream.querySelector('.msg.assistant:last-of-type');
    if (lastAI) renderSummaryPanel(lastAI, state.session);
    scrollToBottom();
}

// ===== 消息追加 =====

function appendUserMessage(text, index = null, msgData = null) {
    const stream = document.getElementById('chat-stream');
    const div = document.createElement('div');
    div.className = 'msg user';
    div.dataset.index = index !== null ? index : '';
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

function appendAssistantMessage(content, thinking = '', index = null, msgData = null) {
    const stream = document.getElementById('chat-stream');
    const div = document.createElement('div');
    div.className = 'msg assistant';
    div.dataset.index = index !== null ? index : '';
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
    const idxRaw = msgEl.dataset.index;
    const idx = (idxRaw === '' || idxRaw == null) ? -1 : parseInt(idxRaw);
    if (!Number.isFinite(idx) || idx < 0) {
        console.warn('消息节点缺少有效 data-index，已跳过绑定:', msgEl);
        return;
    }
    const checkbox = msgEl.querySelector('.msg-checkbox');
    checkbox.addEventListener('change', async (e) => {
        await toggleMessageInPrompt(idx, e.target.checked);
        msgEl.classList.toggle('disabled-from-prompt', !e.target.checked);
    });
    if (!checkbox.checked) msgEl.classList.add('disabled-from-prompt');

    msgEl.querySelector('.msg-action-btn.delete').addEventListener('click', async () => {
        if (!confirm('确认删除这条消息？')) return;
        await deleteMessage(idx);
        await reloadCurrentSession();
    });
    msgEl.querySelector('.msg-action-btn.edit').addEventListener('click', () => enterEditMode(msgEl, idx));
    msgEl.querySelector('.msg-action-btn.regenerate').addEventListener('click', async () => {
        if (!confirm('重新生成？将回滚到这条消息之前重新调用 AI。')) return;
        await regenerateFrom(idx);
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
                const res = await togglePin(idx);
                if (!res || res.error) {
                    // 回滚
                    pinBtn.classList.toggle('active');
                    alert('钉选失败: ' + (res && res.error ? res.error : '网络错误'));
                    return;
                }
                // 以服务端真实状态为准
                const nowPinned = res.session && res.session.message_history && res.session.message_history[idx] && res.session.message_history[idx].pinned;
                pinBtn.classList.toggle('active', !!nowPinned);
                Object.assign(state.session, res.session || {});
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

async function togglePin(idx) {
    const url = `${API.session}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    try {
        const r = await fetch(url, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'toggle_pinned', index: idx }),
        });
        if (!r.ok) {
            const txt = await r.text().catch(() => '');
            return { error: `HTTP ${r.status} ${txt}` };
        }
        return await r.json();
    } catch (e) { console.error('toggle_pin 失败', e); return { error: String(e) }; }
}

function enterEditMode(msgEl, idx) {
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
            await editMessage(idx, newContent);
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

async function toggleMessageInPrompt(idx, inPrompt) {
    const url = `${API.session}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    await fetch(url, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'toggle_in_prompt', index: idx, in_prompt: inPrompt }),
    });
}

async function deleteMessage(idx) {
    const url = `${API.session}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    await fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'delete', index: idx }) });
}

async function editMessage(idx, newContent) {
    const url = `${API.session}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    await fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'edit', index: idx, content: newContent }) });
}

async function regenerateFrom(idx) {
    const url = `${API.session}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
    await fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'snapshot' }) });
    await fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'truncate', index: idx }) });
    await reloadCurrentSession();
    const history = state.session.message_history || [];
    const lastUserMsg = [...history].reverse().find(m => m.role === 'user');
    if (!lastUserMsg) { alert('没有找到要重生成的用户消息'); return; }
    await sendMessage(lastUserMsg.content, true);
}

async function reloadCurrentSession() {
    await loadCurrentSession();
    renderSession(state.session);
    renderHistory(state.session.message_history || []);
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

    parsed.characters.forEach(c => {
        const card = document.createElement('div');
        card.className = 'character-card';
        card.innerHTML = `
            <div class="char-header">
                <span class="char-name">🎭 ${escapeHtml(c.name)}</span>
                <span class="char-affinity">${renderAffinityBar(c.affinity)} ${c.affinity}%</span>
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
        editBtn.addEventListener('click', () => enterEditMode(panel, newest, index));
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
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function enterEditMode(panel, summary, index) {
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
        const r = await fetch('/api/session/summary', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project: state.currentProject,
                save: state.currentSave,
                index: index,
                text: text || '',
                time: time || '',
                facts: facts || [],
                relations: relations || [],
            }),
        });
        if (!r.ok) {
            const txt = await r.text().catch(() => '');
            alert('保存失败: HTTP ' + r.status + ' ' + txt);
            return;
        }
        const res = await r.json();
        if (res.session) Object.assign(state.session, res.session);
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
        const r = await fetch(API.summaryRegen, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: state.currentProject, save: state.currentSave }),
        });
        if (!r.ok) {
            const txt = await r.text().catch(() => '');
            const errMsg = `请求失败: HTTP ${r.status} ${txt}`;
            alert(errMsg);
            if (btn) btn.disabled = false;
            return;
        }
        const res = await r.json();
        if (res.error) { alert('重新生成失败: ' + res.error); if (btn) btn.disabled = false; return; }
        // 成功 → 刷新存档与对话，重新渲染面板（旧 panel 已脱离 DOM）
        if (res.session) Object.assign(state.session, res.session);
        await reloadCurrentSession();
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

async function sendMessage(text = null, isRegenerate = false) {
    const input = document.getElementById('user-input');
    const userText = text !== null ? text : input.value.trim();
    if (!userText || state.isStreaming) return;
    // 清空上一轮残留思考内容，避免跨轮累积串味
    const tc = document.getElementById('thinking-content');
    if (tc) tc.textContent = '';
    if (!isRegenerate) input.value = '';
    if (!isRegenerate) appendUserMessage(userText);
    const targetEl = appendAssistantMessage('（生成中…）');
    state.isStreaming = true;
    // 新建 AbortController，用于中途取消
    activeController = new AbortController();
    // 切流式态：隐藏发送、显示取消
    const sendBtn = document.getElementById('send-btn');
    const cancelBtn = document.getElementById('send-cancel-btn');
    sendBtn.classList.add('hidden');
    cancelBtn.classList.remove('hidden');

    let rawContent = '';

    try {
        const res = await fetch(API.chat, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            signal: activeController.signal,
            body: JSON.stringify({
                user_input: userText,
                project: state.currentProject,
                save: state.currentSave,
                temperature: state.modelParams.temperature,
                top_p: state.modelParams.top_p,
                top_k: state.modelParams.top_k,
                num_predict: state.modelParams.num_predict,
                think: state.modelParams.think,
            }),
        });

        if (!res.ok) {
            const err = await res.text();
            if (targetEl && targetEl.isConnected) targetEl.textContent = `❌ 错误: ${err}`;
            return;  // 复位由 finally 统一处理
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n\n');
            buffer = lines.pop();
            let stopStream = false;
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const payload = line.slice(6).trim();
                if (payload === '[DONE]') continue;
                try {
                    const data = JSON.parse(payload);
                    if (data.type === 'thinking') appendStreamThinking(data.content);
                    else if (data.type === 'content') {
                        rawContent += data.content;
                        if (targetEl && targetEl.isConnected && targetEl.textContent === '（生成中…）') targetEl.textContent = '';
                        if (targetEl && targetEl.isConnected) appendStreamChunk(targetEl, data.content);
                    } else if (data.type === 'error') {
                        if (targetEl && targetEl.isConnected) targetEl.textContent = `❌ ${data.content}`;
                        stopStream = true;  // 后端报错，停止读取整个流
                        break;
                    } else if (data.type === 'parsed') {
                        // 仅当 targetEl 仍挂在 DOM 时才应用，防止跨流串扰
                        if (targetEl && targetEl.isConnected) {
                            if (data.session) Object.assign(state.session, data.session);
                            renderParsedResponse(data.parsed);
                            loadSaveList();
                        }
                    }
                } catch (e) { console.warn('解析 SSE 数据失败', e, payload); }
            }
            if (stopStream) break;
        }
    } catch (e) {
        // AbortController 主动 abort 抛出的异常不算网络错误
        if (e.name !== 'AbortError' && e.name !== 'TypeError') {
            console.error('发送失败', e);
            if (targetEl && targetEl.isConnected) targetEl.textContent = `❌ 网络错误: ${e.message}`;
        } else if (targetEl && targetEl.isConnected) {
            targetEl.textContent = '⏹ 已取消';
        }
    } finally {
        state.isStreaming = false;
        activeController = null;
        // 复位发送/取消按钮态
        document.getElementById('send-btn').classList.remove('hidden');
        document.getElementById('send-cancel-btn').classList.add('hidden');
        // A2：chat 结束兜底拉一次完整 session，确保前端 state 与落盘一致
        try { await reloadCurrentSession(); } catch (e) {}
    }
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
//   prefix:      CSS class 前缀（cf/wf/uf）— 防止冲突
// ============================================================

async function createCardEditor(config) {
    // 1. 加载数据
    let items = [];
    let extraData = null;
    if (config.listApi) {
        try {
            const res = await fetch(config.listApi);
            const json = await res.json();
            items = json[config.listKey] || json.data || [];
        } catch (e) { console.warn('加载列表失败', e); }
    }
    if (config.extraApi) {
        try {
            const res = await fetch(config.extraApi);
            extraData = await res.json();
        } catch (e) { console.warn('加载附加数据失败', e); }
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

        formEl.querySelector('.ce-save').addEventListener('click', async () => {
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
            if (config.idField && !idVal) return alert(`请填 ${config.idLabel || 'ID'}`);
            if (!data.custom || Object.keys(data.custom).length === 0) delete data.custom;
            // 单条模式（用户档案）固定 id='user'
            if (!config.idField) data.id = 'user';

            const saveUrl = config.saveApi ? config.saveApi(idVal || 'user') : null;
            if (!saveUrl) { statusEl.textContent = '✗ 缺少保存 API'; return; }

            statusEl.textContent = '保存中…';
            try {
                const r = await fetch(saveUrl, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ data }),
                });
                if (!r.ok) { const err = await r.json(); throw new Error(err.detail || '保存失败'); }
                statusEl.textContent = '✓ 已保存';
                // 刷新列表
                if (config.allowNew) {
                    const fresh = await fetch(config.listApi).then(r => r.json()).catch(() => null);
                    if (fresh) {
                        items = fresh[config.listKey] || fresh.data || [];
                        currentItem = items.find(it => it[config.idField] === idVal) || data;
                        renderList(); renderForm();
                    }
                }
                if (config.postSave) await config.postSave(data);
            } catch (e) { statusEl.textContent = '✗ ' + e.message; }
        });

        const delBtn = formEl.querySelector('.ce-delete');
        if (delBtn) {
            delBtn.addEventListener('click', async () => {
                const deleteId = config.idField ? (currentItem && currentItem[config.idField]) : 'user';
                const displayName = config.idField ? (currentItem && (currentItem.name || currentItem[config.idField])) : '用户档案';
                if (!deleteId) return;
                if (!confirm(`确定要删除「${displayName}」吗？此操作不可恢复。`)) return;

                const deleteUrl = config.deleteApi(deleteId);
                statusEl.textContent = '删除中…';
                try {
                    const r = await fetch(deleteUrl, { method: 'DELETE' });
                    if (!r.ok) { const err = await r.json(); throw new Error(err.detail || '删除失败'); }
                    statusEl.textContent = '✓ 已删除';
                    if (config.allowNew) {
                        const fresh = await fetch(config.listApi).then(r => r.json()).catch(() => null);
                        if (fresh) items = fresh[config.listKey] || fresh.data || [];
                        currentItem = null;
                        renderList(); renderForm();
                    } else {
                        currentItem = null;
                        extraData = null;
                        renderForm();
                    }
                    // 本地立即清理 state 并刷新 UI，不依赖 reload
                    try {
                        if (config.idField && state.session && state.session.characters_state) {
                            delete state.session.characters_state[deleteId];
                        } else if (!config.idField && state.session) {
                            state.session.user_status = { name: '', identity: '', condition: '', abilities: [] };
                        }
                        renderSession(state.session);
                    } catch (e) { console.warn('删除后刷新 UI 失败', e); }
                    try { await reloadCurrentSession(); } catch (e) {}
                    if (config.postDelete) await config.postDelete(deleteId);
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
    // D2：角色卡字段由后端 schema 单一来源驱动
    const schema = await fetch(API.characterSchema).then(r => r.json()).catch(() => null);
    if (!schema || !schema.groups) {
        showToast('角色卡 schema 加载失败，请刷新重试');
        return;
    }
    await createCardEditor({
        title: '👥 角色卡 — 当前世界观的演员',
        listApi: `${API.characters}?project=${encodeURIComponent(state.currentProject)}`,
        listKey: 'characters',
        saveApi: (id) => `${API.characterSave(id)}?project=${encodeURIComponent(state.currentProject)}`,
        deleteApi: (id) => `${API.characterDelete(id)}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`,
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
    await createCardEditor({
        title: '📖 世界书 — 当前世界观的设定',
        listApi: `${API.worldbook}?project=${encodeURIComponent(state.currentProject)}`,
        listKey: 'entries',
        saveApi: (id) => `${API.worldbookSave(id)}?project=${encodeURIComponent(state.currentProject)}`,
        deleteApi: (id) => `${API.worldbookDelete(id)}?project=${encodeURIComponent(state.currentProject)}`,
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
    await createCardEditor({
        title: '👤 用户档案 — 当前世界的观众设定',
        listApi: null,
        allowNew: false,
        idField: null,
        saveApi: () => `${API.userSave}?project=${encodeURIComponent(state.currentProject)}`,
        deleteApi: (id) => `${API.userDelete}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`,
        prefix: 'user',
        extraApi: `${API.user}?project=${encodeURIComponent(state.currentProject)}`,
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
    const body = document.createElement('div');
    body.innerHTML = `<div class="param-intro">这里存着之前几次的存档快照（每次"重新生成"前会自动存一份）。点某个版本的「恢复」就回到那一次；点「预览」只读查看快照内容，不会覆盖当前存档。</div>
        <div id="history-list" class="history-list"><p style="color:var(--text-dim)">加载中…</p></div>`;
    showModal({ title: '🕐 历史存档', body });

    const listEl = body.querySelector('#history-list');
    try {
        const url = `${API.sessionHistory}?project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
        const res = await fetch(url);
        const data = await res.json();
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
            btn.addEventListener('click', () => showSnapshotPreview(btn.dataset.fn));
        });
        listEl.querySelectorAll('.history-restore').forEach(btn => {
            btn.addEventListener('click', async () => {
                const fn = btn.dataset.fn;
                if (!confirm('恢复这份快照？当前存档内容会被这份覆盖。')) return;
                const r = await fetch(API.sessionRestore, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ project: state.currentProject, save: state.currentSave, filename: fn }),
                });
                if (!r.ok) { const e = await r.json().catch(() => ({})); alert('恢复失败：' + (e.detail || '')); return; }
                await reloadCurrentSession(); hideModal(); alert('已恢复到该快照');
            });
        });
    } catch (e) { listEl.innerHTML = `<p class="empty">读取历史失败：${escapeHtml(e.message)}</p>`; }
}

async function showSnapshotPreview(filename) {
    const body = document.createElement('div');
    body.innerHTML = '<div class="snapshot-preview"><p style="color:var(--text-dim)">加载中…</p></div>';
    showModal({ title: '🔍 快照预览', body });

    const previewEl = body.querySelector('.snapshot-preview');
    try {
        const url = `${API.sessionSnapshot(filename)}&project=${encodeURIComponent(state.currentProject)}&save=${encodeURIComponent(state.currentSave)}`;
        const res = await fetch(url);
        if (!res.ok) { const e = await res.json().catch(() => ({})); previewEl.innerHTML = `<p class="empty">读取失败：${escapeHtml(e.detail || res.statusText)}</p>`; return; }
        const data = await res.json();
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

function showPromptsEditor() {
    fetch(API.prompts).then(r => r.json()).then(data => {
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
            const res = await fetch(API.promptSave(currentTab), {
                method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: textarea.value }),
            });
            if (res.ok) { statusEl.textContent = '✓ 已保存，下次对话生效'; setTimeout(() => { statusEl.textContent = ''; }, 3000); }
            else { const err = await res.json(); statusEl.textContent = '✗ ' + (err.detail || '保存失败'); }
        });

        body.querySelector('#prompt-reset').addEventListener('click', async () => {
            if (!confirm('恢复为默认提示词？当前编辑内容会丢失。')) return;
            const res = await fetch(API.promptReset(currentTab), { method: 'POST' });
            if (res.ok) {
                const fresh = await (await fetch(API.prompts)).json();
                data[currentTab] = fresh[currentTab]; textarea.value = fresh[currentTab] || '';
                const statusEl = body.querySelector('#prompt-status');
                statusEl.textContent = '✓ 已恢复默认'; setTimeout(() => { statusEl.textContent = ''; }, 3000);
            }
        });
    });
}

// ===== 事件绑定 =====

function bindUI() {
    // ===== 新顶栏 v3 =====

    // 项目按钮 → 弹出项目下拉
    const projectBtn = document.getElementById('project-btn');
    const projectDropdown = document.getElementById('project-dropdown');
    projectBtn.addEventListener('click', (e) => {
        e.stopPropagation();
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
        await fetch(API.switchModel, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: state.currentProject, save: state.currentSave, model }),
        });
    });

    // 重置
    document.getElementById('reset-btn').addEventListener('click', async () => {
    if (state.isStreaming && activeController) activeController.abort();
        if (!confirm('重置当前存档？将清空对话历史和角色状态，但保留存档本身。')) return;
        const res = await fetch(API.reset, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: state.currentProject, save: state.currentSave }),
        });
        const session = await res.json();
        state.session = session;
        document.getElementById('chat-stream').innerHTML = '';
        document.getElementById('suggestions').innerHTML = '';
        renderSession(session);
        renderHistory([]);
        if (session.current_model) document.getElementById('model-select').value = session.current_model;
    });

    // 工具按钮（顶栏右侧）
    document.getElementById('prompts-btn').addEventListener('click', showPromptsEditor);
    document.getElementById('model-params-btn').addEventListener('click', showModelParamsEditor);
    document.getElementById('history-btn').addEventListener('click', showHistoryEditor);

    // 发送
    document.getElementById('send-btn').addEventListener('click', () => sendMessage());
    // A1：取消生成——中断当前流式请求，后端 finally 保证已写入内容落盘
    const cancelGenBtn = document.getElementById('send-cancel-btn');
    cancelGenBtn.addEventListener('click', () => {
        if (activeController) activeController.abort();
    });
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
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = '.json';
    inp.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (file) { await importSave(file); hideModal(); }
    });
    showModal({ title: '导入存档', body: inp });
    inp.click();
}

// ===== 工具 =====
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function scrollToBottom() {
    const stream = document.getElementById('chat-stream');
    requestAnimationFrame(() => { stream.scrollTop = stream.scrollHeight; });
}

// 启动
init();
