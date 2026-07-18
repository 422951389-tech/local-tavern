function requireClient(client) {
    if (!client || typeof client.get !== 'function' || typeof client.post !== 'function') {
        throw new TypeError('项目服务需要 ApiClient');
    }
}

const PROJECT_STATS_ERRORS = new Set([
    'characters_unavailable',
    'worldbook_unavailable',
    'sessions_unavailable',
]);

function isNonNegativeInteger(value) {
    return Number.isSafeInteger(value) && value >= 0;
}

export function validateProjectStatsResponse(body) {
    if (!body || typeof body !== 'object' || Array.isArray(body) || !Array.isArray(body.stats)) {
        return '项目统计响应无效';
    }
    const projects = new Set();
    for (const row of body.stats) {
        if (!row || typeof row !== 'object' || Array.isArray(row)) return '项目统计条目无效';
        const keys = Object.keys(row).sort();
        const expected = ['characters', 'errors', 'project', 'saves', 'status', 'worldbook'];
        if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
            return '项目统计条目字段无效';
        }
        if (typeof row.project !== 'string' || !row.project || projects.has(row.project)) {
            return '项目统计 project 无效';
        }
        projects.add(row.project);
        if (![row.characters, row.worldbook, row.saves].every(isNonNegativeInteger)) {
            return '项目统计计数无效';
        }
        if (!['ready', 'partial'].includes(row.status) || !Array.isArray(row.errors)) {
            return '项目统计状态无效';
        }
        if (row.errors.some(error => typeof error !== 'string' || !PROJECT_STATS_ERRORS.has(error))) {
            return '项目统计错误类别无效';
        }
        if (new Set(row.errors).size !== row.errors.length) return '项目统计错误类别重复';
        for (const [error, field] of [
            ['characters_unavailable', 'characters'],
            ['worldbook_unavailable', 'worldbook'],
            ['sessions_unavailable', 'saves'],
        ]) {
            if (row.errors.includes(error) && row[field] !== 0) {
                return '项目统计失败分类必须使用零计数';
            }
        }
        if ((row.status === 'ready' && row.errors.length !== 0)
            || (row.status === 'partial' && row.errors.length === 0)) {
            return '项目统计状态与错误不一致';
        }
    }
    return true;
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
        const requested = [...new Set((projects || []).map(project => String(project)))];
        const body = await client.get(endpoints.projectStats || `${endpoints.projects}/stats`, {
            schema: validateProjectStatsResponse,
        });
        const stats = Object.fromEntries(body.stats.map(row => [row.project, Object.freeze({
            ...row,
            errors: Object.freeze([...row.errors]),
        })]));
        const missing = requested.filter(project => !Object.prototype.hasOwnProperty.call(stats, project));
        if (missing.length > 0) throw new TypeError('项目统计响应缺少已列出的项目');
        return stats;
    }

    return Object.freeze({ list, create, loadStats });
}
