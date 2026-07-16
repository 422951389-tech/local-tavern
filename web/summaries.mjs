export function summaryPatchFromForm(root) {
    const value = field => {
        const element = root.querySelector(`[data-field="${field}"]`);
        return element ? element.value.trim() : '';
    };
    return {
        text: value('text'),
        time: value('time'),
        facts: value('facts').split('\n').map(item => item.trim()).filter(Boolean),
        relations: value('relations').split('\n').map(item => item.trim()).filter(Boolean),
    };
}

export function createSummaryService(writeSession, endpoints) {
    if (typeof writeSession !== 'function') throw new TypeError('摘要服务需要 sessionWrite');
    if (!endpoints || !endpoints.summary || !endpoints.summaryRegen) throw new TypeError('摘要服务缺少 endpoints');

    function update(ref, summaryIndex, summary) {
        return writeSession(endpoints.summary, 'PATCH', {
            project: ref.project,
            save: ref.save,
            index: summaryIndex,
            ...summary,
        }, '编辑摘要');
    }

    function regenerate(ref, summaryIndex = null) {
        const payload = { project: ref.project, save: ref.save };
        if (summaryIndex !== null) payload.summary_index = summaryIndex;
        return writeSession(endpoints.summaryRegen, 'POST', payload, '重生成摘要');
    }

    return Object.freeze({ update, regenerate });
}
