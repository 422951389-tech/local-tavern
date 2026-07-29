import { formatBytes } from './product-tools.mjs';
import {
    createProductActionButton,
    createProductElement,
    createProductMetric,
    formatProductDate,
} from './product-ui.mjs';

export function backupChangeSummary(dryRun) {
    const counts = { create: 0, replace: 0, remove: 0 };
    dryRun.changes.forEach(change => { counts[change.action] += 1; });
    return `新增 ${counts.create} · 替换 ${counts.replace} · 移除 ${counts.remove}`;
}

export function createBackupController(options) {
    const {
        documentRef,
        service,
        showModal,
        showToast,
        errorDetail,
        reloadWorkspace = () => globalThis.location.reload(),
        schedule = callback => globalThis.setTimeout(callback, 0),
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

    function showRestoreConfirmation(backup, dryRun) {
        const body = element('div', 'product-tool backup-restore-confirmation');
        body.appendChild(element(
            'p', 'product-warning',
            `恢复会把当前数据切换到此备份。预检结果：${backupChangeSummary(dryRun)}。恢复前会自动创建保护备份。`,
        ));
        const changes = element('div', 'backup-change-list');
        if (!dryRun.changes.length) {
            changes.appendChild(element('p', 'product-empty', '预检未发现文件变化。'));
        } else {
            for (const change of dryRun.changes) {
                const row = element('div', `backup-change action-${change.action}`);
                row.append(
                    element('span', 'backup-change-action', ({ create: '新增', replace: '替换', remove: '移除' })[change.action]),
                    element('code', 'backup-change-path', change.path),
                );
                changes.appendChild(row);
            }
        }
        body.appendChild(changes);
        const phrase = `恢复 ${backup.backupId.slice(0, 8)}`;
        const label = element('label', 'product-field');
        label.appendChild(element('span', 'product-field-label', `输入“${phrase}”确认`));
        const input = element('input');
        input.type = 'text';
        input.autocomplete = 'off';
        label.appendChild(input);
        body.appendChild(label);
        let conflictsAccepted = !dryRun.requiresConfirmation;
        if (dryRun.requiresConfirmation) {
            const conflict = element('label', 'product-check');
            const checkbox = element('input');
            checkbox.type = 'checkbox';
            checkbox.addEventListener('change', () => { conflictsAccepted = checkbox.checked; });
            conflict.append(checkbox, element('span', '', `我已查看 ${dryRun.conflicts.length} 项覆盖或移除冲突`));
            body.appendChild(conflict);
        }
        showModal({
            title: '确认恢复备份',
            body,
            footer: {
                confirmText: '执行恢复',
                pendingText: '恢复中…',
                onConfirm: async () => {
                    input.removeAttribute('aria-invalid');
                    if (input.value.trim() !== phrase || !conflictsAccepted) {
                        input.setAttribute('aria-invalid', 'true');
                        throw new Error('请完成确认短语与冲突确认后再恢复');
                    }
                    const result = await service.restore(
                        backup.backupId,
                        dryRun.currentFingerprint,
                        dryRun.requiresConfirmation,
                    );
                    const resultBody = element('div', 'product-tool');
                    resultBody.append(
                        element('p', 'product-success', '备份恢复完成，恢复前保护备份已经创建。'),
                        metric('已恢复备份', result.backupId),
                        metric('恢复前保护备份', result.preRestoreBackupId),
                    );
                    const reload = actionButton('重新载入工作台', 'primary-btn', 'regenerate');
                    reload.addEventListener('click', reloadWorkspace);
                    resultBody.appendChild(reload);
                    schedule(() => showModal({ title: '恢复完成', body: resultBody }));
                    return true;
                },
            },
        });
    }

    async function showCenter() {
        const body = element('div', 'product-tool backup-center');
        const toolbar = element('div', 'product-toolbar');
        const reason = element('input', 'product-compact-input');
        reason.type = 'text';
        reason.maxLength = 500;
        reason.placeholder = '备份说明（可选）';
        reason.setAttribute('aria-label', '备份说明');
        const create = actionButton('立即备份', 'primary-btn', 'archive');
        const refresh = actionButton('刷新', 'secondary-btn', 'regenerate');
        toolbar.append(reason, create, refresh);
        const status = element('p', 'product-status', '正在读取备份…');
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        const list = element('div', 'backup-list');
        body.append(toolbar, status, list);

        const load = async () => {
            list.replaceChildren();
            status.textContent = '正在读取备份…';
            try {
                const backups = await service.list();
                status.textContent = `共 ${backups.length} 个备份`;
                if (!backups.length) list.appendChild(element('div', 'product-empty', '还没有备份。可先创建一个手动备份。'));
                for (const backup of backups) {
                    const card = element('article', 'backup-card');
                    const heading = element('div', 'backup-card-heading');
                    heading.append(
                        element('strong', '', backup.reason || backup.kind || '手动备份'),
                        element('span', `product-badge status-${backup.status}`, backup.status === 'valid' ? '可用' : '无效'),
                    );
                    const meta = element(
                        'p', 'product-help',
                        `${formatProductDate(backup.createdAt)} · ${backup.fileCount} 个文件 · ${formatBytes(backup.totalSize)} · ${backup.backupId.slice(0, 8)}`,
                    );
                    const actions = element('div', 'product-row-actions');
                    const dry = actionButton('预检');
                    const drill = actionButton('恢复演练');
                    const restore = actionButton('恢复', 'danger-outline-btn');
                    const result = element('p', 'product-card-status');
                    result.setAttribute('role', 'status');
                    result.setAttribute('aria-live', 'polite');
                    const run = async (button, work) => {
                        button.disabled = true;
                        try { await work(); }
                        catch (error) { result.textContent = `操作失败：${errorDetail(error)}`; }
                        finally { button.disabled = false; }
                    };
                    dry.addEventListener('click', () => run(dry, async () => {
                        result.textContent = '正在预检…';
                        const report = await service.dryRun(backup.backupId);
                        result.textContent = `预检完成：${backupChangeSummary(report)}${report.conflicts.length ? ` · ${report.conflicts.length} 项冲突` : ''}`;
                    }));
                    drill.addEventListener('click', () => run(drill, async () => {
                        result.textContent = '正在执行隔离恢复演练…';
                        const report = await service.drill(backup.backupId);
                        result.textContent = report.status === 'passed'
                            ? `演练通过：检查 ${report.checkedFileCount} 个文件，临时工作区已清理`
                            : `演练结果：${report.status}${report.error ? ` · ${report.error}` : ''}`;
                    }));
                    restore.addEventListener('click', () => run(restore, async () => {
                        result.textContent = '正在执行恢复前预检…';
                        const report = await service.dryRun(backup.backupId);
                        showRestoreConfirmation(backup, report);
                    }));
                    if (backup.status !== 'valid' || backup.expired) {
                        dry.disabled = true;
                        drill.disabled = true;
                        restore.disabled = true;
                        result.textContent = backup.expired ? '此备份已过期，不能恢复' : '此备份校验失败，不能使用';
                    }
                    actions.append(dry, drill, restore);
                    card.append(heading, meta, actions, result);
                    list.appendChild(card);
                }
            } catch (error) {
                status.textContent = `读取备份失败：${errorDetail(error)}`;
            }
        };
        create.addEventListener('click', async () => {
            create.disabled = true;
            status.textContent = '正在创建备份…';
            try {
                const created = await service.create(reason.value);
                reason.value = '';
                showToast(`备份 ${created.backupId.slice(0, 8)} 已创建`);
                await load();
            } catch (error) {
                status.textContent = `创建备份失败：${errorDetail(error)}`;
            } finally {
                create.disabled = false;
            }
        });
        refresh.addEventListener('click', () => { void load(); });
        showModal({ title: '备份中心', body });
        await load();
    }

    return Object.freeze({ showCenter, showRestoreConfirmation });
}
