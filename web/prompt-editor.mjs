export function promptTabTargetIndex(key, currentIndex, length) {
    if (!Number.isInteger(currentIndex) || !Number.isInteger(length) || length <= 0) return null;
    if (key === 'ArrowRight') return (currentIndex + 1) % length;
    if (key === 'ArrowLeft') return (currentIndex - 1 + length) % length;
    if (key === 'Home') return 0;
    if (key === 'End') return length - 1;
    return null;
}

export function createPromptService(client, endpoints) {
    if (!client || typeof client.get !== 'function' || typeof client.put !== 'function') {
        throw new TypeError('Prompt 服务需要 ApiClient');
    }
    if (!endpoints || !endpoints.prompts) throw new TypeError('Prompt 服务缺少 endpoints');

    function validate(body) {
        return Boolean(
            body
            && typeof body.system === 'string'
            && typeof body.group_chat === 'string'
            && typeof body.summary === 'string'
        )
            || '提示词响应无效';
    }

    function load() {
        return client.get(endpoints.prompts, { schema: validate });
    }

    function save(name, content) {
        return client.put(endpoints.promptSave(name), { content });
    }

    async function reset(name) {
        await client.post(endpoints.promptReset(name));
        return load();
    }

    return Object.freeze({ load, save, reset });
}
