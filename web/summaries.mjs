export function summaryPatchFromForm(root) {
    const value = field => {
        const element = root.querySelector(`[data-field="${field}"]`);
        return element ? element.value.trim() : '';
    };
    const patch = {
        text: value('text'),
        time: value('time'),
        facts: value('facts').split('\n').map(item => item.trim()).filter(Boolean),
        relations: value('relations').split('\n').map(item => item.trim()).filter(Boolean),
    };
    if (!patch.text) throw new TypeError('前情提要不能为空');
    if (patch.text.length > 2000) throw new RangeError('前情提要不能超过 2000 个字符');
    if (patch.time.length > 300) throw new RangeError('时间线不能超过 300 个字符');
    for (const [field, label] of [['facts', '关键事件'], ['relations', '角色关系']]) {
        if (patch[field].length > 5) throw new RangeError(`${label}最多 5 条`);
        if (patch[field].some(item => item.length > 200)) {
            throw new RangeError(`${label}每条不能超过 200 个字符`);
        }
    }
    return patch;
}

function requireSummaryId(summaryId) {
    if (typeof summaryId !== 'string' || !summaryId.trim()) {
        throw new TypeError('摘要操作需要稳定 summary_id');
    }
    return summaryId.trim();
}

export function createSummaryService(writeSession, endpoints) {
    if (typeof writeSession !== 'function') throw new TypeError('摘要服务需要 sessionWrite');
    if (!endpoints || !endpoints.summary || !endpoints.summaryRegen) throw new TypeError('摘要服务缺少 endpoints');

    function update(ref, summaryId, summary) {
        return writeSession(endpoints.summary, 'PATCH', {
            project: ref.project,
            save: ref.save,
            summary_id: requireSummaryId(summaryId),
            ...summary,
        }, '编辑摘要');
    }

    function regenerate(ref, summaryId) {
        return writeSession(endpoints.summaryRegen, 'POST', {
            project: ref.project,
            save: ref.save,
            summary_id: requireSummaryId(summaryId),
        }, '重生成摘要');
    }

    return Object.freeze({ update, regenerate });
}
