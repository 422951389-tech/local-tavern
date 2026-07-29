import {
    createProductActionButton,
    createProductElement,
    createProductMetric,
    formatProductDate,
} from './product-ui.mjs';

function diagnosticsStatusLabel(value) {
    return ({ ready: '就绪', ok: '正常', degraded: '需检查', unavailable: '不可用', unknown: '未记录' })[value] || value || '未记录';
}

export function createDiagnosticsController(options) {
    const {
        documentRef,
        service,
        showModal,
        errorDetail,
        BlobRef = globalThis.Blob,
        URLRef = globalThis.URL,
        now = () => new Date(),
    } = options;
    const element = (tag, className = '', text = null) => (
        createProductElement(documentRef, tag, className, text)
    );
    const actionButton = (label, className = 'secondary-btn', iconName = null) => (
        createProductActionButton(documentRef, label, className, iconName)
    );
    const metric = (label, value, suffix = '') => (
        createProductMetric(documentRef, label, value, suffix)
    );

    function renderSnapshot(snapshot) {
        const root = element('div', 'diagnostics-snapshot');
        const summary = element('div', 'product-metrics');
        summary.append(
            metric('整体状态', diagnosticsStatusLabel(snapshot.health.status)),
            metric('运行模式', snapshot.application.runtime_mode || '未记录'),
            metric('传输方式', snapshot.application.transport || '未记录'),
            metric('运行中任务', snapshot.turns.active),
            metric('有效备份', snapshot.backups.valid),
            metric('已配置云端来源', snapshot.providers.length),
        );
        root.appendChild(summary);
        const checks = element('section', 'product-section');
        checks.appendChild(element('h3', '', '健康检查'));
        const checkList = element('div', 'diagnostic-checks');
        const names = { data: '数据目录', runtime: '桌面运行时', ollama: '本地 Ollama', providers: '模型来源', maintenance: '维护任务', inference: '当前推理' };
        for (const [key, label] of Object.entries(names)) {
            const check = snapshot.health.checks[key];
            const row = element('div', 'diagnostic-check');
            row.append(
                element('strong', '', label),
                element('span', `product-badge status-${check.status}`, diagnosticsStatusLabel(check.status)),
                element('small', '', [check.code, check.source, check.connectivity].filter(Boolean).join(' · ') || '无附加信息'),
            );
            checkList.appendChild(row);
        }
        checks.appendChild(checkList);
        root.appendChild(checks);
        const errors = element('section', 'product-section');
        errors.appendChild(element('h3', '', '近期错误代码'));
        if (!snapshot.recent_errors.length) errors.appendChild(element('p', 'product-empty', '没有记录到近期错误。'));
        else {
            const list = element('ul', 'product-plain-list');
            snapshot.recent_errors.forEach(item => list.appendChild(element(
                'li', '', `${item.timestamp || '时间未记录'} · ${item.component || '未知组件'} · ${item.code || '无代码'}`,
            )));
            errors.appendChild(list);
        }
        root.appendChild(errors);
        root.appendChild(element('p', 'product-privacy-note', '诊断内容经过字段白名单重建，不包含 API Key、提示词正文、云端地址或角色私密原文。'));
        return root;
    }

    async function showCenter() {
        const body = element('div', 'product-tool diagnostics-center');
        const toolbar = element('div', 'product-toolbar');
        const refresh = actionButton('刷新诊断', 'secondary-btn', 'regenerate');
        const download = actionButton('下载脱敏支持包', 'primary-btn', 'download');
        toolbar.append(refresh, download);
        const status = element('p', 'product-status', '正在读取诊断…');
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        const content = element('div');
        body.append(toolbar, status, content);
        const load = async () => {
            refresh.disabled = true;
            status.textContent = '正在读取诊断…';
            try {
                const snapshot = await service.snapshot();
                content.replaceChildren(renderSnapshot(snapshot));
                status.textContent = `诊断生成于 ${formatProductDate(snapshot.generated_at)}`;
            } catch (error) {
                status.textContent = `读取诊断失败：${errorDetail(error)}`;
            } finally {
                refresh.disabled = false;
            }
        };
        refresh.addEventListener('click', () => { void load(); });
        download.addEventListener('click', async () => {
            download.disabled = true;
            status.textContent = '正在生成脱敏支持包…';
            try {
                const snapshot = await service.supportBundle();
                const blob = new BlobRef([JSON.stringify(snapshot, null, 2)], { type: 'application/json' });
                const link = documentRef.createElement('a');
                link.href = URLRef.createObjectURL(blob);
                link.download = `local-tavern-support-${now().toISOString().slice(0, 10)}.json`;
                link.click();
                URLRef.revokeObjectURL(link.href);
                status.textContent = '脱敏支持包已下载';
            } catch (error) {
                status.textContent = `支持包生成失败：${errorDetail(error)}`;
            } finally {
                download.disabled = false;
            }
        });
        showModal({ title: '诊断中心', body });
        await load();
    }

    return Object.freeze({ renderSnapshot, showCenter });
}
