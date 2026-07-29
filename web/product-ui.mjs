import { createIcon } from './icons.mjs';

export function createProductElement(documentRef, tag, className = '', text = null) {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (text !== null) node.textContent = String(text);
    return node;
}

export function createProductActionButton(
    documentRef,
    label,
    className = 'secondary-btn',
    iconName = null,
) {
    const button = createProductElement(documentRef, 'button', className);
    button.type = 'button';
    if (iconName) button.appendChild(createIcon(documentRef, iconName, { size: 16 }));
    button.appendChild(createProductElement(documentRef, 'span', '', label));
    return button;
}

export function createProductMetric(documentRef, label, value, suffix = '') {
    const item = createProductElement(documentRef, 'div', 'product-metric');
    item.appendChild(createProductElement(documentRef, 'span', 'product-metric-label', label));
    item.appendChild(createProductElement(documentRef, 'strong', 'product-metric-value', `${value}${suffix}`));
    return item;
}

export function formatProductDate(value) {
    if (!value) return '时间未记录';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export function productLines(value) {
    return String(value || '').split(/\r?\n/).map(item => item.trim()).filter(Boolean);
}
