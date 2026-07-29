'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const BACKUP_ID = '11111111-1111-4111-8111-111111111111';
const DRILL_ID = '22222222-2222-4222-8222-222222222222';
const PRE_RESTORE_ID = '33333333-3333-4333-8333-333333333333';
const NOTE_ID = '44444444-4444-4444-8444-444444444444';
const MESSAGE_ID = '55555555-5555-4555-8555-555555555555';
const ACTIVE_VARIANT_ID = '66666666-6666-4666-8666-666666666666';
const OLD_VARIANT_ID = '77777777-7777-4777-8777-777777777777';
const FINGERPRINT = 'a'.repeat(64);

async function productTools() {
    return import('../web/product-tools.mjs');
}

function diagnosticFixture(overrides = {}) {
    return {
        schema_version: 1,
        generated_at: '2026-07-29T12:00:00+00:00',
        application: { name: 'Local Tavern', runtime_mode: 'desktop', transport: 'in_process_asgi' },
        runtime: { python: '3.12.4', system: 'Windows', release: '11', machine: 'AMD64' },
        release: {
            status: 'available', schema_version: 1, application: 'Local Tavern', packaging: 'folder',
            file_count: 12, total_bytes: 4096, source: { commit: 'abc123', dirty: false },
        },
        turns: { status: 'ok', total: 2, active: 1, terminal: 1, invalid: 0, event_log_bytes: 2048 },
        recent_errors: [{ timestamp: '2026-07-29', level: 'ERROR', component: 'routes.chat', code: 'provider_failed' }],
        health: {
            status: 'ready', service: 'local-tavern', contract_version: 2, readiness_scope: 'configuration',
            checks: {
                data: { status: 'ok' }, runtime: { status: 'ok' }, ollama: { status: 'ok' },
                providers: { status: 'ok', cloud_configured: true, connectivity: 'not_probed' },
                maintenance: { status: 'ok' },
                inference: { status: 'ok', source: 'ollama', connectivity: 'verified' },
            },
        },
        providers: [{ id: 'ollama', name: 'Ollama', kind: 'ollama', has_credential: false, model_count: 2, context_limit: 32768 }],
        backups: {
            total: 1, valid: 1, invalid: 0, pending_restores: 0,
            latest: { backup_id: BACKUP_ID, kind: 'manual', created_at: '2026-07-29', file_count: 5, total_size: 900, expired: false },
        },
        ...overrides,
    };
}

test('诊断快照只保留白名单，支持包不会泄露未知敏感字段', async () => {
    const { normalizeDiagnosticsSnapshot } = await productTools();
    const normalized = normalizeDiagnosticsSnapshot(diagnosticFixture({
        api_key: 'sk-secret',
        application: { name: 'Local Tavern', runtime_mode: 'desktop', transport: 'in_process_asgi', prompt_body: 'secret prompt' },
        providers: [{
            id: 'cloud', name: 'Cloud', kind: 'openai_compatible', has_credential: true,
            model_count: 1, context_limit: 8192, base_url: 'https://secret.example', credential_value: 'secret',
        }],
    }));
    const serialized = JSON.stringify(normalized);
    assert.equal(normalized.health.checks.inference.source, 'ollama');
    assert.equal(normalized.providers[0].has_credential, true);
    for (const forbidden of ['sk-secret', 'prompt_body', 'secret prompt', 'base_url', 'credential_value', 'secret.example']) {
        assert.equal(serialized.includes(forbidden), false, forbidden);
    }
});

test('备份服务固定执行 list/create/dry-run/drill/restore 契约并强制最新指纹', async () => {
    const { createBackupService } = await productTools();
    const calls = [];
    const summary = {
        backup_id: BACKUP_ID, kind: 'manual', reason: '用户操作', created_at: '2026-07-29',
        source_fingerprint: FINGERPRINT, file_count: 3, total_size: 1024,
        application: 'Local Tavern', application_version: '1', data_schema_version: 1, expired: false,
    };
    const client = {
        async get(path) { calls.push(['get', path]); return { backups: [summary] }; },
        async post(path, body) {
            calls.push(['post', path, body]);
            if (path === '/api/backups') return { backup: summary };
            if (path.endsWith('/dry-run')) return {
                backup_id: BACKUP_ID, current_fingerprint: FINGERPRINT, backup_fingerprint: FINGERPRINT,
                changes: [{ path: 'projects/a/session.json', action: 'replace', current_sha256: 'secret' }],
                conflicts: [{ path: 'projects/a/session.json', action: 'replace' }], requires_confirmation: true,
            };
            if (path.endsWith('/drill')) return {
                drill_id: DRILL_ID, backup_id: BACKUP_ID, status: 'passed', checked_file_count: 3,
                workspace_cleaned: true, started_at: 'start', completed_at: 'done', error: null,
            };
            return {
                restored: true, backup_id: BACKUP_ID, pre_restore_backup_id: PRE_RESTORE_ID,
                current_fingerprint: FINGERPRINT,
            };
        },
    };
    const service = createBackupService(client);
    assert.equal((await service.list())[0].backupId, BACKUP_ID);
    await service.create(' 用户操作 ');
    const dryRun = await service.dryRun(BACKUP_ID);
    assert.deepEqual(dryRun.conflicts, [{ path: 'projects/a/session.json', action: 'replace' }]);
    assert.equal((await service.drill(BACKUP_ID)).workspaceCleaned, true);
    assert.equal((await service.restore(BACKUP_ID, FINGERPRINT, true)).preRestoreBackupId, PRE_RESTORE_ID);
    assert.deepEqual(calls.at(-1), ['post', `/api/backups/${BACKUP_ID}/restore`, {
        expected_current_fingerprint: FINGERPRINT,
        confirm_conflicts: true,
    }]);
    await assert.rejects(() => service.restore(BACKUP_ID, 'stale', true), /指纹无效/);
});

test('长期记忆 CRUD 绑定项目、存档和 expected_revision，角色关联不丢失', async () => {
    const { createMemoryNotesService } = await productTools();
    const calls = [];
    const storedNote = {
        id: NOTE_ID, kind: 'memory_note', character_id: 'alpha', text: '她不喜欢下雨。', time: '春日',
        facts: ['雨天影响心情'], relations: ['与用户约定带伞'], status: 'completed',
        content_status: 'valid', source_status: 'unlinked', created_at: 'start', edited_at: 'done',
    };
    const session = { session_id: '存档 A', revision: 8 };
    const client = {
        async get(path) { calls.push(['get', path]); return { revision: 7, notes: [storedNote] }; },
        async post(path, body) { calls.push(['post', path, body]); return { note: storedNote, session }; },
        async patch(path, body) { calls.push(['patch', path, body]); return { note: storedNote, session }; },
        async delete(path, options) { calls.push(['delete', path, options]); return { deleted: storedNote, session }; },
    };
    const service = createMemoryNotesService(client);
    const ref = { project: '项目 A', save: '存档 A' };
    assert.equal((await service.list(ref)).notes[0].characterId, 'alpha');
    const draft = {
        characterId: 'alpha', text: '她不喜欢下雨。', time: '春日',
        facts: ['雨天影响心情'], relations: ['与用户约定带伞'],
    };
    await service.create(ref, 7, draft);
    await service.update(ref, 8, NOTE_ID, draft);
    await service.remove(ref, 9, NOTE_ID);
    assert.deepEqual(calls[1], ['post', '/api/memory-notes', {
        project: '项目 A', save: '存档 A', expected_revision: 7,
        text: draft.text, time: draft.time, facts: draft.facts, relations: draft.relations,
        character_id: 'alpha',
    }]);
    assert.deepEqual(calls.at(-1), ['delete', `/api/memory-notes/${NOTE_ID}`, {
        json: { project: '项目 A', save: '存档 A', expected_revision: 9 },
    }]);
});

test('消息生成详情展示预算和遥测，旧消息返回明确空态且不携带未知字段', async () => {
    const { normalizeMessageGenerationDetails } = await productTools();
    const details = normalizeMessageGenerationDetails({
        context_diagnostics: {
            schema_version: 2, estimator: 'local', context_limit: 32768, context_limit_source: 'model',
            reserved_output_tokens: 2048, safety_margin_tokens: 512, input_budget_tokens: 30208,
            estimated_prompt_tokens: 1200, remaining_input_tokens: 29008,
            sources: [
                { source: 'summary', id: NOTE_ID, estimated_tokens: 45, kept: true, reason: 'within_budget', raw_text: 'secret' },
            ],
            worldbook_matches: [{ id: 'entry', content: 'secret worldbook' }],
            prompt_body: 'secret prompt',
        },
        generation_telemetry: {
            schema_version: 1, provider: 'ollama', model: 'qwen', status: 'completed', error_code: null,
            latency_ms: 1234, input_tokens_estimated: 1200, output_tokens_estimated: 88,
            output_bytes: 512, token_source: 'local_estimator', finished_at: 'done', api_key: 'secret',
        },
    });
    assert.equal(details.context.remainingInputTokens, 29008);
    assert.equal(details.context.sources[0].kept, true);
    assert.equal(details.context.worldbookMatchCount, 1);
    assert.equal(details.telemetry.latencyMs, 1234);
    assert.equal(details.empty, false);
    assert.equal(JSON.stringify(details).includes('secret'), false);
    assert.deepEqual(normalizeMessageGenerationDetails({ content: '旧消息' }), {
        context: null, telemetry: null, empty: true,
    });
});

test('备选回复按稳定时间排序、过滤状态快照并发送精确 CAS 切换契约', async () => {
    const { createReplyAlternativeService, normalizeReplyVariants } = await productTools();
    const message = {
        id: MESSAGE_ID,
        role: 'assistant',
        reply_variant_id: ACTIVE_VARIANT_ID,
        content: '新回复',
        timestamps: { completed_at: '2026-07-29T12:00:02Z' },
        reply_alternatives: [{
            id: OLD_VARIANT_ID,
            content: '旧回复',
            timestamps: { completed_at: '2026-07-29T12:00:01Z' },
            state_snapshot: { prompt: '绝不应进入展示模型' },
        }],
    };
    const normalized = normalizeReplyVariants(message);
    assert.equal(normalized.count, 2);
    assert.equal(normalized.activeIndex, 1);
    assert.deepEqual(normalized.variants.map(item => item.id), [OLD_VARIANT_ID, ACTIVE_VARIANT_ID]);
    assert.equal(JSON.stringify(normalized).includes('state_snapshot'), false);
    assert.equal(JSON.stringify(normalized).includes('绝不应进入'), false);

    const calls = [];
    const session = { session_id: '存档 A', revision: 11 };
    const service = createReplyAlternativeService({
        async patch(path, body) {
            calls.push([path, body]);
            return {
                message_id: MESSAGE_ID,
                reply_variant_id: OLD_VARIANT_ID,
                alternative_count: 1,
                state_applied: false,
                session,
            };
        },
    });
    const result = await service.select(
        { project: '项目 A', save: '存档 A' }, 10, MESSAGE_ID, OLD_VARIANT_ID,
    );
    assert.equal(result.stateApplied, false);
    assert.equal(result.session, session);
    assert.deepEqual(calls[0], ['/api/session/reply-alternative', {
        project: '项目 A', save: '存档 A', expected_revision: 10,
        message_id: MESSAGE_ID, alternative_id: OLD_VARIANT_ID,
    }]);
});
