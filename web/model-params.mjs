export const DEFAULT_MODEL_PARAMS = Object.freeze({
    temperature: 0.8,
    top_p: 0.9,
    top_k: 40,
    num_predict: 4096,
    think: true,
});

const PARAM_SPECS = Object.freeze([
    Object.freeze({
        key: 'temperature', id: 'p-temp', label: '活跃度（温度）',
        min: 0, max: 2, step: 0.1,
        hint: '越小越老实，越大越奔放。常用 0.6~1.0',
        format: value => Number(value).toFixed(1),
    }),
    Object.freeze({
        key: 'top_p', id: 'p-topp', label: '收口（top_p）',
        min: 0.1, max: 1, step: 0.05,
        hint: '越小 AI 越只挑最有把握的词，越稳。',
        format: value => Number(value).toFixed(2),
    }),
    Object.freeze({
        key: 'top_k', id: 'p-topk', label: '候选（top_k）',
        min: 0, max: 200, step: 1, integer: true,
        hint: '0 = 不限。越小越保守。',
        format: value => String(Number(value)),
    }),
    Object.freeze({
        key: 'num_predict', id: 'p-nump', label: '最多字数',
        min: 256, max: 16384, step: 256, integer: true,
        hint: 'AI 一轮最多写多少字。太短会被截断。',
        format: value => String(Number(value)),
    }),
]);

function normalizedNumber(value, spec, fallback) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return fallback;
    if (spec.integer && !Number.isSafeInteger(value)) return fallback;
    return Math.max(spec.min, Math.min(spec.max, value));
}

export function normalizeModelParams(value = {}) {
    const source = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    const normalized = {};
    for (const spec of PARAM_SPECS) {
        normalized[spec.key] = normalizedNumber(
            source[spec.key],
            spec,
            DEFAULT_MODEL_PARAMS[spec.key],
        );
    }
    normalized.think = typeof source.think === 'boolean'
        ? source.think
        : DEFAULT_MODEL_PARAMS.think;
    return Object.freeze(normalized);
}

function element(documentRef, tag, className = '', text = '') {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (text !== '') node.textContent = text;
    return node;
}

export function createModelParamsEditor(documentRef, value = {}) {
    if (!documentRef || typeof documentRef.createElement !== 'function') {
        throw new TypeError('模型参数编辑器缺少 document');
    }
    const params = normalizeModelParams(value);
    const root = element(documentRef, 'div', 'model-params-editor');
    const intro = element(
        documentRef,
        'div',
        'param-intro',
        '这里的滑块控制 AI 这轮“说话的风格”。调完点「保存」，下次发消息生效。',
    );
    const emphasis = element(
        documentRef,
        'b',
        '',
        '往左调小 = 更稳、更老实跟你设定走；往右调大 = 更天马行空、更“放”。',
    );
    intro.appendChild(emphasis);
    root.appendChild(intro);

    const controls = {};
    for (const spec of PARAM_SPECS) {
        const row = element(documentRef, 'div', 'form-slider');
        const top = element(documentRef, 'div', 'slider-top');
        const label = element(documentRef, 'label', 'slider-label', spec.label);
        label.htmlFor = spec.id;
        const output = element(documentRef, 'output', 'slider-value');
        output.id = `val-${spec.id}`;
        output.setAttribute('for', spec.id);
        output.textContent = spec.format(params[spec.key]);
        top.appendChild(label);
        top.appendChild(output);

        const input = element(documentRef, 'input');
        input.type = 'range';
        input.id = spec.id;
        input.min = String(spec.min);
        input.max = String(spec.max);
        input.step = String(spec.step);
        input.value = String(params[spec.key]);
        const hint = element(documentRef, 'div', 'slider-hint', spec.hint);
        hint.id = `${spec.id}-hint`;
        input.setAttribute('aria-describedby', hint.id);
        input.addEventListener('input', () => { output.textContent = spec.format(input.value); });
        row.appendChild(top);
        row.appendChild(input);
        row.appendChild(hint);
        root.appendChild(row);
        controls[spec.key] = input;
    }

    const thinkRow = element(documentRef, 'div', 'form-slider');
    const thinkTop = element(documentRef, 'div', 'slider-top');
    const thinkLabel = element(documentRef, 'label', 'slider-label', '开“内心思考”');
    thinkLabel.htmlFor = 'p-think';
    const switchRoot = element(documentRef, 'span', 'param-switch');
    const think = element(documentRef, 'input');
    think.type = 'checkbox';
    think.id = 'p-think';
    think.checked = params.think;
    const switchVisual = element(documentRef, 'span', 'switch-slider');
    switchVisual.setAttribute('aria-hidden', 'true');
    switchRoot.appendChild(think);
    switchRoot.appendChild(switchVisual);
    thinkTop.appendChild(thinkLabel);
    thinkTop.appendChild(switchRoot);
    const thinkHint = element(
        documentRef,
        'div',
        'slider-hint',
        '开启后 AI 会先思考再写；关闭后直接生成。',
    );
    thinkHint.id = 'p-think-hint';
    think.setAttribute('aria-describedby', thinkHint.id);
    thinkRow.appendChild(thinkTop);
    thinkRow.appendChild(thinkHint);
    root.appendChild(thinkRow);
    controls.think = think;

    return Object.freeze({
        root,
        controls: Object.freeze(controls),
        read() {
            return normalizeModelParams({
                temperature: Number(controls.temperature.value),
                top_p: Number(controls.top_p.value),
                top_k: Number(controls.top_k.value),
                num_predict: Number(controls.num_predict.value),
                think: controls.think.checked,
            });
        },
        setDisabled(disabled) {
            for (const control of Object.values(controls)) control.disabled = Boolean(disabled);
        },
    });
}
