function normalizeSession(body) {
    const session = body && typeof body === 'object' ? (body.session || body) : null;
    return session && session.session_id && Number.isInteger(session.revision) ? session : null;
}

export function createSaveService(client, endpoints) {
    if (!client || typeof client.get !== 'function' || typeof client.post !== 'function') {
        throw new TypeError('存档服务需要 ApiClient');
    }
    if (!endpoints || !endpoints.sessions || !endpoints.session) {
        throw new TypeError('存档服务缺少 endpoints');
    }

    async function list(project) {
        const body = await client.get(`${endpoints.sessions}?project=${encodeURIComponent(project)}`, {
            schema: value => Array.isArray(value && value.sessions) || '存档列表响应无效',
        });
        return [...body.sessions];
    }

    async function get(project, save) {
        const body = await client.get(
            `${endpoints.session}?project=${encodeURIComponent(project)}&save=${encodeURIComponent(save)}`,
            { schema: value => Boolean(normalizeSession(value)) || '存档响应无效' },
        );
        return normalizeSession(body);
    }

    async function create(project, name) {
        const body = await client.post(endpoints.sessionCreate, { project, name }, {
            schema: value => Boolean(normalizeSession(value)) || '创建存档响应无效',
        });
        return normalizeSession(body);
    }

    function exportJson(project, save) {
        return client.get(
            `${endpoints.sessionExport}?project=${encodeURIComponent(project)}&save=${encodeURIComponent(save)}`,
            { schema: body => Boolean(body && typeof body.json_str === 'string') || '导出响应无效' },
        );
    }

    async function importJson(project, jsonText, name) {
        const body = await client.post(endpoints.sessionImport, {
            project,
            json_str: jsonText,
            name,
        }, { schema: value => Boolean(normalizeSession(value)) || '导入响应无效' });
        return normalizeSession(body);
    }

    return Object.freeze({ list, get, create, exportJson, importJson });
}
