function requireClient(client) {
    if (!client || typeof client.get !== 'function' || typeof client.post !== 'function') {
        throw new TypeError('项目服务需要 ApiClient');
    }
}

export function createProjectService(client, endpoints) {
    requireClient(client);
    if (!endpoints || !endpoints.projects) throw new TypeError('项目服务缺少 endpoints');

    async function list() {
        const body = await client.get(endpoints.projects, {
            schema: value => Array.isArray(value && value.projects) || '项目列表响应无效',
        });
        return [...body.projects];
    }

    async function create(name) {
        const value = String(name || '').trim();
        if (!value) throw new TypeError('项目名不能为空');
        return client.post(endpoints.projects, { name: value }, {
            schema: body => Boolean(body && typeof body.name === 'string') || '创建项目响应无效',
        });
    }

    async function loadStats(projects) {
        const entries = await Promise.all((projects || []).map(async project => {
            try {
                const query = encodeURIComponent(project);
                const [characters, worldbook, sessions] = await Promise.all([
                    client.get(`${endpoints.characters}?project=${query}`, {
                        schema: body => Array.isArray(body && body.characters) || '角色列表响应无效',
                    }),
                    client.get(`${endpoints.worldbook}?project=${query}`, {
                        schema: body => Array.isArray(body && body.entries) || '世界书列表响应无效',
                    }),
                    client.get(`${endpoints.sessions}?project=${query}`, {
                        schema: body => Array.isArray(body && body.sessions) || '存档列表响应无效',
                    }),
                ]);
                return [project, {
                    chars: characters.characters.length,
                    world: worldbook.entries.length,
                    saves: sessions.sessions.length,
                }];
            } catch (error) {
                return [project, { chars: '—', world: '—', saves: '—', error }];
            }
        }));
        return Object.fromEntries(entries);
    }

    return Object.freeze({ list, create, loadStats });
}
