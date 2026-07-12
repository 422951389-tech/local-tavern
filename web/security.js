(function attachTavernSecurity(root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.TavernSecurity = Object.freeze(api);
})(typeof globalThis !== 'undefined' ? globalThis : this, function createTavernSecurity() {
    'use strict';

    const MAX_IMPORT_BYTES = 8 * 1024 * 1024;
    const HTML_ESCAPES = Object.freeze({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    });

    function escapeHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value).replace(/[&<>"']/g, char => HTML_ESCAPES[char]);
    }

    function normalizeAffinity(value) {
        const number = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(number)) return 0;
        return Math.max(0, Math.min(100, Math.round(number)));
    }

    function validateImportFile(file) {
        if (!file || typeof file.name !== 'string') return '请选择 JSON 文件';
        if (!/\.json$/i.test(file.name)) return '只允许导入 .json 文件';
        if (!Number.isFinite(file.size) || file.size < 0) return '无法读取文件大小';
        if (file.size > MAX_IMPORT_BYTES) return '导入文件不能超过 8 MiB';
        return '';
    }

    return { MAX_IMPORT_BYTES, escapeHtml, normalizeAffinity, validateImportFile };
});
