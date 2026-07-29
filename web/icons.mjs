const SVG_NS = 'http://www.w3.org/2000/svg';

const ICON_PATHS = Object.freeze({
    archive: ['M21 8v13H3V8', 'M1 3h22v5H1z', 'M10 12h4'],
    arrowRight: ['M5 12h14', 'm13 6 6 6-6 6'],
    book: ['M4 19.5A2.5 2.5 0 0 1 6.5 17H20', 'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z'],
    brain: ['M9.5 4.5A3 3 0 0 0 5 7v1a3 3 0 0 0-1 5.83V15a3 3 0 0 0 4.5 2.6', 'M14.5 4.5A3 3 0 0 1 19 7v1a3 3 0 0 1 1 5.83V15a3 3 0 0 1-4.5 2.6', 'M9.5 4.5V20', 'M14.5 4.5V20', 'M9.5 9H7', 'M17 9h-2.5', 'M9.5 15H7', 'M17 15h-2.5'],
    check: ['m5 12 4 4L19 6'],
    chevronDown: ['m6 9 6 6 6-6'],
    chevronLeft: ['m15 18-6-6 6-6'],
    cloud: ['M17.5 19H9a7 7 0 1 1 6.7-9h1.8a4.5 4.5 0 0 1 0 9z'],
    download: ['M12 3v12', 'm7 10 5 5 5-5', 'M5 21h14'],
    edit: ['M12 20h9', 'M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z'],
    eye: ['M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z', 'M12 12h.01'],
    folder: ['M3 6h6l2 2h10v11H3z'],
    history: ['M3 12a9 9 0 1 0 3-6.7L3 8', 'M3 3v5h5', 'M12 7v5l3 2'],
    key: ['M21 2 13.6 9.4', 'M15 6l3 3', 'M10 13a5 5 0 1 1-7.1 7.1A5 5 0 0 1 10 13Z'],
    mapPin: ['M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0Z', 'M12 10h.01'],
    menu: ['M4 6h16', 'M4 12h16', 'M4 18h16'],
    network: ['M12 5v5', 'M6 19v-2a3 3 0 0 1 3-3h6a3 3 0 0 1 3 3v2', 'M6 5h.01', 'M18 5h.01', 'M12 19h.01'],
    panel: ['M3 4h18v16H3z', 'M15 4v16'],
    pin: ['M12 17v5', 'M5 3h14', 'm7 3-1 7-3 3h18l-3-3-1-7'],
    plus: ['M12 5v14', 'M5 12h14'],
    provider: ['M12 2a4 4 0 0 1 4 4v2h1a3 3 0 0 1 3 3v7H4v-7a3 3 0 0 1 3-3h1V6a4 4 0 0 1 4-4Z', 'M8 12h8', 'M9 16h.01', 'M12 16h.01', 'M15 16h.01'],
    regenerate: ['M20 7v5h-5', 'M4 17v-5h5', 'M6.1 9a7 7 0 0 1 11.7-2L20 12', 'M4 12l2.2 5a7 7 0 0 0 11.7-2'],
    search: ['m21 21-4.3-4.3', 'M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z'],
    send: ['m22 2-7 20-4-9-9-4Z', 'M22 2 11 13'],
    settings: ['M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z', 'M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.1 3.6-.2-.1a1.7 1.7 0 0 0-1.9-.2l-1 .6a1.7 1.7 0 0 0-.9 1.6v.2H9.5v-.2a1.7 1.7 0 0 0-.9-1.6l-1-.6a1.7 1.7 0 0 0-1.9.2l-.2.1L3.4 17l.1-.1a1.7 1.7 0 0 0 .3-1.9l-.5-1a1.7 1.7 0 0 0-1.5-.9h-.2V8.9h.2A1.7 1.7 0 0 0 3.3 8l.5-1a1.7 1.7 0 0 0-.3-1.9L3.4 5l2.1-3.6.2.1a1.7 1.7 0 0 0 1.9.2l1-.6a1.7 1.7 0 0 0 .9-1.6v-.2h4.2v.2a1.7 1.7 0 0 0 .9 1.6l1 .6a1.7 1.7 0 0 0 1.9-.2l.2-.1L19.8 5l-.1.1a1.7 1.7 0 0 0-.3 1.9l.5 1a1.7 1.7 0 0 0 1.5.9h.2v4.2h-.2a1.7 1.7 0 0 0-1.5.9Z'],
    sliders: ['M4 21v-7', 'M4 10V3', 'M12 21v-9', 'M12 8V3', 'M20 21v-5', 'M20 12V3', 'M1 14h6', 'M9 8h6', 'M17 16h6'],
    sparkles: ['m12 3-1.2 3.3L7.5 7.5l3.3 1.2L12 12l1.2-3.3 3.3-1.2-3.3-1.2Z', 'm5 13-.8 2.2L2 16l2.2.8L5 19l.8-2.2L8 16l-2.2-.8Z', 'm19 14-.7 1.8-1.8.7 1.8.7L19 19l.7-1.8 1.8-.7-1.8-.7Z'],
    stop: ['M6 6h12v12H6z'],
    target: ['M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z', 'M12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12Z', 'M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z'],
    trash: ['M3 6h18', 'M8 6V4h8v2', 'm19 6-1 15H6L5 6', 'M10 11v6', 'M14 11v6'],
    upload: ['M12 21V9', 'm7 14 5-5 5 5', 'M5 3h14'],
    user: ['M20 21a8 8 0 0 0-16 0', 'M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z'],
    users: ['M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2', 'M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z', 'M22 21v-2a4 4 0 0 0-3-3.9', 'M16 3.1a4 4 0 0 1 0 7.8'],
    x: ['M18 6 6 18', 'm6 6 12 12'],
});

function svgElement(documentRef, tag) {
    return typeof documentRef.createElementNS === 'function'
        ? documentRef.createElementNS(SVG_NS, tag)
        : documentRef.createElement(tag);
}

export function createIcon(documentRef, name, options = {}) {
    if (!documentRef || typeof documentRef.createElement !== 'function') {
        throw new TypeError('图标渲染缺少 document');
    }
    const paths = ICON_PATHS[name];
    if (!paths) throw new TypeError(`未知图标：${name}`);
    const svg = svgElement(documentRef, 'svg');
    svg.setAttribute('class', options.className || 'ui-icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.8');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('focusable', 'false');
    const size = String(options.size || 20);
    svg.setAttribute('width', size);
    svg.setAttribute('height', size);
    if (options.label) {
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', String(options.label));
    } else {
        svg.setAttribute('aria-hidden', 'true');
    }
    for (const definition of paths) {
        const path = svgElement(documentRef, 'path');
        path.setAttribute('d', definition);
        svg.appendChild(path);
    }
    return svg;
}

export function hydrateIcons(documentRef = globalThis.document) {
    if (!documentRef || typeof documentRef.querySelectorAll !== 'function') return 0;
    let mounted = 0;
    for (const host of documentRef.querySelectorAll('[data-icon]')) {
        const name = host.dataset ? host.dataset.icon : host.getAttribute('data-icon');
        if (!ICON_PATHS[name]) continue;
        host.replaceChildren(createIcon(documentRef, name));
        mounted += 1;
    }
    return mounted;
}

export const iconNames = Object.freeze(Object.keys(ICON_PATHS));
