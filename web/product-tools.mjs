const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const FINGERPRINT_PATTERN = /^[0-9a-f]{64}$/;
const BACKUP_ACTIONS = new Set(['create', 'replace', 'remove']);
const MEMORY_ARRAY_LIMIT = 200;

function isRecord(value) {
    return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function text(value, maximum = 512) {
    if (typeof value !== 'string') return '';
    return value.replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, maximum);
}

function integer(value, { minimum = 0, maximum = Number.MAX_SAFE_INTEGER, fallback = 0 } = {}) {
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) return fallback;
    return value;
}

function finiteNumber(value, { minimum = 0, maximum = Number.MAX_SAFE_INTEGER, fallback = null } = {}) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < minimum || value > maximum) return fallback;
    return value;
}

function fingerprint(value, required = false) {
    const normalized = text(value, 64).toLowerCase();
    if (FINGERPRINT_PATTERN.test(normalized)) return normalized;
    if (required) throw new TypeError('备份指纹无效');
    return '';
}

function uuid(value, label = 'ID') {
    const normalized = text(value, 64).toLowerCase();
    if (!UUID_PATTERN.test(normalized)) throw new TypeError(`${label}无效`);
    return normalized;
}

function identity(value, label) {
    const normalized = text(value, 256);
    if (!normalized) throw new TypeError(`${label}不能为空`);
    return normalized;
}

function requireRef(ref) {
    if (!isRecord(ref)) throw new TypeError('缺少当前项目或存档');
    return Object.freeze({
        project: identity(ref.project, '项目'),
        save: identity(ref.save, '存档'),
    });
}

function revision(value) {
    if (!Number.isSafeInteger(value) || value < 0) throw new TypeError('存档 revision 无效');
    return value;
}

function stringArray(value, maximumItems = MEMORY_ARRAY_LIMIT, maximumLength = 1000) {
    if (!Array.isArray(value)) return [];
    return value.slice(0, maximumItems).map(item => text(item, maximumLength)).filter(Boolean);
}

export function normalizeBackupSummary(value) {
    if (!isRecord(value)) throw new TypeError('备份摘要无效');
    const backupId = uuid(value.backup_id, '备份 ID');
    const invalid = value.status === 'invalid';
    return Object.freeze({
        backupId,
        status: invalid ? 'invalid' : 'valid',
        code: text(value.code, 80),
        kind: text(value.kind, 40),
        reason: text(value.reason, 500),
        createdAt: text(value.created_at, 80),
        sourceFingerprint: fingerprint(value.source_fingerprint),
        fileCount: integer(value.file_count),
        totalSize: integer(value.total_size),
        application: text(value.application, 80),
        applicationVersion: text(value.application_version, 80),
        dataSchemaVersion: integer(value.data_schema_version),
        expired: value.expired === true,
    });
}

export function normalizeBackupListResponse(value) {
    if (!isRecord(value) || !Array.isArray(value.backups) || value.backups.length > 1000) {
        throw new TypeError('备份列表响应无效');
    }
    return Object.freeze(value.backups.map(normalizeBackupSummary));
}

function normalizeBackupChange(value) {
    if (!isRecord(value)) return null;
    const action = text(value.action, 16);
    const path = text(value.path, 1000);
    if (!BACKUP_ACTIONS.has(action) || !path) return null;
    return Object.freeze({ path, action });
}

export function normalizeBackupDryRun(value) {
    if (!isRecord(value)) throw new TypeError('备份预检响应无效');
    const changes = (Array.isArray(value.changes) ? value.changes : [])
        .slice(0, 5000).map(normalizeBackupChange).filter(Boolean);
    const conflicts = (Array.isArray(value.conflicts) ? value.conflicts : [])
        .slice(0, 5000).map(normalizeBackupChange).filter(Boolean);
    return Object.freeze({
        backupId: uuid(value.backup_id, '备份 ID'),
        currentFingerprint: fingerprint(value.current_fingerprint, true),
        backupFingerprint: fingerprint(value.backup_fingerprint, true),
        changes: Object.freeze(changes),
        conflicts: Object.freeze(conflicts),
        requiresConfirmation: value.requires_confirmation === true,
    });
}

export function normalizeBackupDrill(value) {
    if (!isRecord(value)) throw new TypeError('备份演练响应无效');
    return Object.freeze({
        drillId: uuid(value.drill_id, '演练 ID'),
        backupId: uuid(value.backup_id, '备份 ID'),
        status: text(value.status, 32),
        checkedFileCount: integer(value.checked_file_count),
        workspaceCleaned: value.workspace_cleaned === true,
        startedAt: text(value.started_at, 80),
        completedAt: text(value.completed_at, 80),
        error: text(value.error, 300),
    });
}

export function normalizeBackupRestore(value) {
    if (!isRecord(value) || value.restored !== true) throw new TypeError('备份恢复响应无效');
    return Object.freeze({
        restored: true,
        backupId: uuid(value.backup_id, '备份 ID'),
        preRestoreBackupId: uuid(value.pre_restore_backup_id, '恢复前备份 ID'),
        currentFingerprint: fingerprint(value.current_fingerprint, true),
    });
}

export function createBackupService(client) {
    if (!client || typeof client.get !== 'function' || typeof client.post !== 'function') {
        throw new TypeError('备份服务缺少 API client');
    }
    const endpoint = '/api/backups';
    return Object.freeze({
        async list() {
            return normalizeBackupListResponse(await client.get(endpoint));
        },
        async create(reason = '') {
            const payload = await client.post(endpoint, { reason: text(reason, 500) || null });
            if (!isRecord(payload) || !isRecord(payload.backup)) throw new TypeError('创建备份响应无效');
            return normalizeBackupSummary(payload.backup);
        },
        async dryRun(backupId) {
            const id = uuid(backupId, '备份 ID');
            return normalizeBackupDryRun(await client.post(`${endpoint}/${encodeURIComponent(id)}/dry-run`, {}));
        },
        async drill(backupId) {
            const id = uuid(backupId, '备份 ID');
            return normalizeBackupDrill(await client.post(`${endpoint}/${encodeURIComponent(id)}/drill`, {}));
        },
        async restore(backupId, currentFingerprint, confirmConflicts = false) {
            const id = uuid(backupId, '备份 ID');
            if (typeof confirmConflicts !== 'boolean') throw new TypeError('恢复冲突确认必须是布尔值');
            return normalizeBackupRestore(await client.post(`${endpoint}/${encodeURIComponent(id)}/restore`, {
                expected_current_fingerprint: fingerprint(currentFingerprint, true),
                confirm_conflicts: confirmConflicts,
            }));
        },
    });
}

function normalizeHealthCheck(value) {
    if (!isRecord(value)) return Object.freeze({ status: 'unknown', code: '', source: '', connectivity: '' });
    return Object.freeze({
        status: text(value.status, 32) || 'unknown',
        code: text(value.code, 80),
        source: text(value.source, 80),
        connectivity: text(value.connectivity, 80),
        cloud_configured: value.cloud_configured === true,
    });
}

function normalizeDiagnosticsHealth(value) {
    const source = isRecord(value) ? value : {};
    const rawChecks = isRecord(source.checks) ? source.checks : {};
    const checks = {};
    for (const key of ['data', 'runtime', 'ollama', 'providers', 'maintenance', 'inference']) {
        checks[key] = normalizeHealthCheck(rawChecks[key]);
    }
    return Object.freeze({
        status: text(source.status, 32) || 'unknown',
        service: text(source.service, 80),
        contract_version: integer(source.contract_version),
        readiness_scope: text(source.readiness_scope, 80),
        checks: Object.freeze(checks),
    });
}

function normalizeDiagnosticsProviders(value) {
    if (!Array.isArray(value)) return Object.freeze([]);
    return Object.freeze(value.slice(0, 100).flatMap(item => {
        if (!isRecord(item)) return [];
        const id = text(item.id, 80);
        if (!id) return [];
        return [Object.freeze({
            id,
            name: text(item.name, 120),
            kind: text(item.kind, 80),
            has_credential: item.has_credential === true,
            model_count: integer(item.model_count),
            context_limit: integer(item.context_limit),
        })];
    }));
}

export function normalizeDiagnosticsSnapshot(value) {
    if (!isRecord(value)) throw new TypeError('诊断响应无效');
    const application = isRecord(value.application) ? value.application : {};
    const runtime = isRecord(value.runtime) ? value.runtime : {};
    const release = isRecord(value.release) ? value.release : {};
    const releaseSource = isRecord(release.source) ? release.source : {};
    const turns = isRecord(value.turns) ? value.turns : {};
    const backups = isRecord(value.backups) ? value.backups : {};
    const latest = isRecord(backups.latest) ? backups.latest : null;
    const recentErrors = Array.isArray(value.recent_errors)
        ? value.recent_errors.slice(0, 20).flatMap(item => isRecord(item) ? [Object.freeze({
            timestamp: text(item.timestamp, 40),
            level: text(item.level, 20),
            component: text(item.component, 80),
            code: text(item.code, 80),
        })] : [])
        : [];
    return Object.freeze({
        schema_version: integer(value.schema_version),
        generated_at: text(value.generated_at, 80),
        application: Object.freeze({
            name: text(application.name, 80),
            runtime_mode: text(application.runtime_mode, 80),
            transport: text(application.transport, 80),
        }),
        runtime: Object.freeze({
            python: text(runtime.python, 40),
            system: text(runtime.system, 80),
            release: text(runtime.release, 120),
            machine: text(runtime.machine, 80),
        }),
        release: Object.freeze({
            status: text(release.status, 40),
            schema_version: integer(release.schema_version),
            application: text(release.application, 80),
            packaging: text(release.packaging, 80),
            file_count: integer(release.file_count),
            total_bytes: integer(release.total_bytes),
            source: Object.freeze({
                commit: text(releaseSource.commit, 80),
                dirty: releaseSource.dirty === true,
            }),
        }),
        turns: Object.freeze({
            status: text(turns.status, 32),
            total: integer(turns.total),
            active: integer(turns.active),
            terminal: integer(turns.terminal),
            invalid: integer(turns.invalid),
            event_log_bytes: integer(turns.event_log_bytes),
        }),
        recent_errors: Object.freeze(recentErrors),
        health: normalizeDiagnosticsHealth(value.health),
        providers: normalizeDiagnosticsProviders(value.providers),
        backups: Object.freeze({
            total: integer(backups.total),
            valid: integer(backups.valid),
            invalid: integer(backups.invalid),
            pending_restores: integer(backups.pending_restores),
            latest: latest ? Object.freeze({
                backup_id: UUID_PATTERN.test(text(latest.backup_id, 64)) ? text(latest.backup_id, 64).toLowerCase() : '',
                kind: text(latest.kind, 40),
                created_at: text(latest.created_at, 80),
                file_count: integer(latest.file_count),
                total_size: integer(latest.total_size),
                expired: latest.expired === true,
            }) : null,
        }),
    });
}

export function createDiagnosticsService(client) {
    if (!client || typeof client.get !== 'function') throw new TypeError('诊断服务缺少 API client');
    return Object.freeze({
        async snapshot() {
            return normalizeDiagnosticsSnapshot(await client.get('/api/diagnostics'));
        },
        async supportBundle() {
            return normalizeDiagnosticsSnapshot(await client.get('/api/diagnostics/support-bundle'));
        },
    });
}

export function normalizeMemoryNote(value) {
    if (!isRecord(value)) throw new TypeError('记忆便签无效');
    const characterId = value.character_id === null || value.character_id === undefined
        ? ''
        : text(value.character_id, 256);
    return Object.freeze({
        id: uuid(value.id, '记忆便签 ID'),
        kind: text(value.kind, 40),
        characterId,
        text: text(value.text, 50_000),
        time: text(value.time, 1000),
        facts: Object.freeze(stringArray(value.facts)),
        relations: Object.freeze(stringArray(value.relations)),
        status: text(value.status, 40),
        contentStatus: text(value.content_status, 40),
        sourceStatus: text(value.source_status, 40),
        createdAt: text(value.created_at, 80),
        editedAt: text(value.edited_at, 80),
    });
}

function normalizeMemoryList(value) {
    if (!isRecord(value) || !Array.isArray(value.notes) || value.notes.length > 100) {
        throw new TypeError('记忆便签列表响应无效');
    }
    return Object.freeze({
        revision: revision(value.revision),
        notes: Object.freeze(value.notes.map(normalizeMemoryNote)),
    });
}

function memoryDraft(value, { includeCharacter = true } = {}) {
    if (!isRecord(value)) throw new TypeError('记忆便签草稿无效');
    const result = {
        text: text(value.text, 50_000),
        time: text(value.time, 1000),
        facts: stringArray(value.facts),
        relations: stringArray(value.relations),
    };
    if (includeCharacter) result.character_id = text(value.characterId, 256) || null;
    return result;
}

function normalizeMemoryMutation(value) {
    if (!isRecord(value) || !isRecord(value.session) || !Number.isSafeInteger(value.session.revision)) {
        throw new TypeError('记忆便签写入响应无效');
    }
    const noteValue = value.note || value.deleted;
    return Object.freeze({ note: normalizeMemoryNote(noteValue), session: value.session });
}

export function createMemoryNotesService(client) {
    if (!client || typeof client.get !== 'function' || typeof client.post !== 'function'
        || typeof client.patch !== 'function' || typeof client.delete !== 'function') {
        throw new TypeError('记忆便签服务缺少 API client');
    }
    const endpoint = '/api/memory-notes';
    const base = (ref, expectedRevision) => {
        const normalized = requireRef(ref);
        return {
            project: normalized.project,
            save: normalized.save,
            expected_revision: revision(expectedRevision),
        };
    };
    return Object.freeze({
        async list(ref) {
            const normalized = requireRef(ref);
            return normalizeMemoryList(await client.get(
                `${endpoint}?project=${encodeURIComponent(normalized.project)}&save=${encodeURIComponent(normalized.save)}`,
            ));
        },
        async create(ref, expectedRevision, draft) {
            return normalizeMemoryMutation(await client.post(endpoint, {
                ...base(ref, expectedRevision),
                ...memoryDraft(draft),
            }));
        },
        async update(ref, expectedRevision, noteId, draft) {
            const id = uuid(noteId, '记忆便签 ID');
            return normalizeMemoryMutation(await client.patch(`${endpoint}/${encodeURIComponent(id)}`, {
                ...base(ref, expectedRevision),
                ...memoryDraft(draft),
            }));
        },
        async remove(ref, expectedRevision, noteId) {
            const id = uuid(noteId, '记忆便签 ID');
            return normalizeMemoryMutation(await client.delete(`${endpoint}/${encodeURIComponent(id)}`, {
                json: base(ref, expectedRevision),
            }));
        },
    });
}

function diagnosticSource(value) {
    if (!isRecord(value)) return null;
    const source = text(value.source, 80);
    const id = text(value.id, 256);
    if (!source || !id) return null;
    return Object.freeze({
        source,
        id,
        estimatedTokens: integer(value.estimated_tokens),
        kept: value.kept === true,
        reason: text(value.reason, 120),
    });
}

function contextDiagnostics(value) {
    if (!isRecord(value)) return null;
    const sources = (Array.isArray(value.sources) ? value.sources : [])
        .slice(0, 1000).map(diagnosticSource).filter(Boolean);
    const hasKnownValue = Number.isSafeInteger(value.context_limit)
        || Number.isSafeInteger(value.estimated_prompt_tokens)
        || sources.length > 0;
    if (!hasKnownValue) return null;
    return Object.freeze({
        schemaVersion: integer(value.schema_version),
        estimator: text(value.estimator, 120),
        contextLimit: integer(value.context_limit),
        contextLimitSource: text(value.context_limit_source, 120),
        reservedOutputTokens: integer(value.reserved_output_tokens),
        safetyMarginTokens: integer(value.safety_margin_tokens),
        inputBudgetTokens: integer(value.input_budget_tokens),
        estimatedPromptTokens: integer(value.estimated_prompt_tokens),
        remainingInputTokens: integer(value.remaining_input_tokens),
        sources: Object.freeze(sources),
        worldbookMatchCount: Array.isArray(value.worldbook_matches)
            ? Math.min(1000, value.worldbook_matches.length)
            : 0,
    });
}

function generationTelemetry(value) {
    if (!isRecord(value)) return null;
    const provider = text(value.provider, 80);
    const model = text(value.model, 256);
    const status = text(value.status, 40);
    if (!provider && !model && !status) return null;
    return Object.freeze({
        schemaVersion: integer(value.schema_version),
        provider,
        model,
        status,
        errorCode: text(value.error_code, 80),
        latencyMs: finiteNumber(value.latency_ms),
        inputTokensEstimated: integer(value.input_tokens_estimated),
        outputTokensEstimated: integer(value.output_tokens_estimated),
        outputBytes: integer(value.output_bytes),
        tokenSource: text(value.token_source, 80),
        finishedAt: text(value.finished_at, 80),
    });
}

export function normalizeMessageGenerationDetails(message) {
    const source = isRecord(message) ? message : {};
    const metadata = isRecord(source.metadata) ? source.metadata : {};
    const context = contextDiagnostics(source.context_diagnostics || metadata.context_diagnostics);
    const telemetry = generationTelemetry(source.generation_telemetry || metadata.generation_telemetry);
    return Object.freeze({ context, telemetry, empty: !context && !telemetry });
}

function replyVariant(value, { active = false } = {}) {
    if (!isRecord(value)) return null;
    const id = text(active ? value.reply_variant_id : value.id, 64).toLowerCase();
    if (!UUID_PATTERN.test(id)) return null;
    const timestamps = isRecord(value.timestamps) ? value.timestamps : {};
    const completedAt = text(timestamps.completed_at || value.completed_at, 80);
    const createdAt = text(timestamps.created_at || value.created_at, 80);
    return Object.freeze({
        id,
        active,
        content: text(value.content, 50_000),
        thinking: text(value.thinking, 50_000),
        createdAt,
        completedAt,
        sortKey: `${completedAt || createdAt || '9999'}\u0000${id}`,
    });
}

/**
 * 将当前回复与备选回复收敛为稳定、按完成时间排序的只读列表。
 * state_snapshot 等分支状态从不进入前端展示模型。
 */
export function normalizeReplyVariants(message) {
    if (!isRecord(message) || message.role !== 'assistant') {
        return Object.freeze({ variants: Object.freeze([]), activeIndex: -1, count: 0 });
    }
    const active = replyVariant(message, { active: true });
    const alternatives = Array.isArray(message.reply_alternatives)
        ? message.reply_alternatives.slice(0, 9).map(item => replyVariant(item)).filter(Boolean)
        : [];
    if (!active || alternatives.length === 0) {
        return Object.freeze({ variants: Object.freeze([]), activeIndex: -1, count: 0 });
    }
    const unique = new Map([[active.id, active]]);
    for (const alternative of alternatives) {
        if (!unique.has(alternative.id)) unique.set(alternative.id, alternative);
    }
    const variants = [...unique.values()].sort((left, right) => left.sortKey.localeCompare(right.sortKey));
    const activeIndex = variants.findIndex(item => item.id === active.id);
    return Object.freeze({
        variants: Object.freeze(variants),
        activeIndex,
        count: variants.length,
    });
}

export function createReplyAlternativeService(client) {
    if (!client || typeof client.patch !== 'function') {
        throw new TypeError('备选回复服务缺少 API client');
    }
    return Object.freeze({
        async select(ref, expectedRevision, messageId, alternativeId) {
            const normalized = requireRef(ref);
            const payload = await client.patch('/api/session/reply-alternative', {
                project: normalized.project,
                save: normalized.save,
                expected_revision: revision(expectedRevision),
                message_id: uuid(messageId, '消息 ID'),
                alternative_id: uuid(alternativeId, '备选回复 ID'),
            });
            if (!isRecord(payload) || !isRecord(payload.session)
                || !Number.isSafeInteger(payload.session.revision)) {
                throw new TypeError('切换备选回复响应无效');
            }
            return Object.freeze({
                messageId: uuid(payload.message_id, '消息 ID'),
                replyVariantId: uuid(payload.reply_variant_id, '备选回复 ID'),
                alternativeCount: integer(payload.alternative_count, { maximum: 9 }),
                stateApplied: payload.state_applied === true,
                session: payload.session,
            });
        },
    });
}

export function formatBytes(value) {
    const bytes = integer(value);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}
