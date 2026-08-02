import {
    createProductActionButton,
    createProductElement,
    createProductMetric,
    formatProductDate,
} from './product-ui.mjs';

function diagnosticsStatusLabel(value) {
    return ({ ready: '就绪', ok: '正常', degraded: '需检查', unavailable: '不可用', unknown: '未记录' })[value] || value || '未记录';
}

function rendererLabel(value) {
    return ({ software: '稳定软件渲染', hardware: '硬件加速', not_applicable: '浏览器开发模式' })[value] || '未记录';
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
            metric('界面渲染', rendererLabel(snapshot.desktop_rendering.mode)),
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
        const renderer = element('section', 'product-section diagnostics-renderer');
        renderer.appendChild(element('h3', '', 'EXE 界面渲染'));
        renderer.appendChild(element(
            'p', 'product-description',
            '稳定软件渲染用于规避部分显卡驱动、虚拟显示器与 Qt WebEngine 组合下的滚动闪烁；切换后需重启 EXE。',
        ));
        const rendererRow = element('div', 'diagnostics-renderer-row');
        const rendererSelect = element('select', 'product-select');
        rendererSelect.setAttribute('aria-label', '桌面渲染模式');
        for (const [value, label] of [['software', '稳定软件渲染（推荐）'], ['hardware', '硬件加速']]) {
            const option = element('option', '', label);
            option.value = value;
            option.selected = snapshot.desktop_rendering.mode === value;
            rendererSelect.appendChild(option);
        }
        const saveRenderer = actionButton('保存渲染模式', 'secondary-btn');
        saveRenderer.addEventListener('click', async () => {
            saveRenderer.disabled = true;
            try {
                await service.saveRenderer(rendererSelect.value);
                saveRenderer.textContent = '已保存，重启 EXE 生效';
            } catch (error) {
                saveRenderer.textContent = `保存失败：${errorDetail(error)}`;
            } finally {
                saveRenderer.disabled = false;
            }
        });
        rendererRow.append(rendererSelect, saveRenderer);
        renderer.appendChild(rendererRow);
        root.appendChild(renderer);

        const privacy = element('section', 'product-section fault-report-scope');
        privacy.appendChild(element('h3', '', '故障报告会包含什么'));
        const scopeGrid = element('div', 'fault-report-scope-grid');
        const included = element('div', 'fault-report-scope-card included');
        included.append(
            element('strong', '', '包含'),
            element('p', '', '应用版本、运行环境、渲染模式、健康检查状态、备份数量、模型来源类型和错误代码。'),
        );
        const excluded = element('div', 'fault-report-scope-card excluded');
        excluded.append(
            element('strong', '', '不包含'),
            element('p', '', '对话正文、角色设定、世界书正文、提示词、API Key、云端地址和本地文件路径。'),
        );
        scopeGrid.append(included, excluded);
        privacy.appendChild(scopeGrid);
        privacy.appendChild(element('p', 'product-privacy-note', '诊断内容经过字段白名单重建；你可以先预览完整 JSON，再决定是否导出。'));
        root.appendChild(privacy);
        return root;
    }

    async function showCenter() {
        const body = element('div', 'product-tool diagnostics-center');
        const toolbar = element('div', 'product-toolbar');
        const refresh = actionButton('刷新诊断', 'secondary-btn', 'regenerate');
        const download = actionButton('预览故障报告', 'primary-btn', 'eye');
        toolbar.append(refresh, download);
        const status = element('p', 'product-status', '正在读取诊断…');
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        const content = element('div');
        const reportPreview = element('section', 'fault-report-preview');
        reportPreview.hidden = true;
        body.append(toolbar, status, content, reportPreview);
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
            status.textContent = '正在生成故障报告预览…';
            try {
                const snapshot = await service.supportBundle();
                const serialized = JSON.stringify(snapshot, null, 2);
                reportPreview.replaceChildren();
                reportPreview.appendChild(element('h3', '', '故障报告预览'));
                reportPreview.appendChild(element('p', 'product-description', '确认内容后再导出；报告中没有对话、设定正文和凭据。'));
                reportPreview.appendChild(element('pre', 'fault-report-json', serialized));
                const exportButton = actionButton('导出故障报告（JSON）', 'primary-btn', 'download');
                exportButton.addEventListener('click', () => {
                    const blob = new BlobRef([serialized], { type: 'application/json' });
                    const link = documentRef.createElement('a');
                    link.href = URLRef.createObjectURL(blob);
                    link.download = `local-tavern-fault-report-${now().toISOString().slice(0, 10)}.json`;
                    link.click();
                    URLRef.revokeObjectURL(link.href);
                    status.textContent = '故障报告已导出';
                });
                reportPreview.appendChild(exportButton);
                reportPreview.hidden = false;
                status.textContent = '故障报告已生成，请检查预览内容';
            } catch (error) {
                status.textContent = `故障报告生成失败：${errorDetail(error)}`;
            } finally {
                download.disabled = false;
            }
        });
        showModal({ title: '诊断与故障报告', body });
        await load();
    }

    return Object.freeze({ renderSnapshot, showCenter });
}
