export function createFrameRenderer(options = {}) {
    const requestFrame = options.requestFrame || globalThis.requestAnimationFrame;
    const cancelFrame = options.cancelFrame || globalThis.cancelAnimationFrame;
    if (typeof requestFrame !== 'function' || typeof cancelFrame !== 'function') {
        throw new TypeError('帧渲染器需要 requestAnimationFrame/cancelAnimationFrame');
    }
    if (typeof options.render !== 'function') throw new TypeError('帧渲染器缺少 render');
    const isCurrent = typeof options.isCurrent === 'function' ? options.isCurrent : () => true;

    let pending = null;
    let frameId = null;
    let generation = 0;

    function commit(snapshot) {
        if (snapshot === null || !isCurrent(snapshot)) return false;
        options.render(snapshot);
        return true;
    }

    function enqueue(snapshot) {
        pending = snapshot;
        if (frameId !== null) return;
        const scheduledGeneration = generation;
        frameId = requestFrame(() => {
            frameId = null;
            if (scheduledGeneration !== generation) return;
            const latest = pending;
            pending = null;
            commit(latest);
        });
    }

    function flush() {
        if (frameId !== null) cancelFrame(frameId);
        frameId = null;
        generation += 1;
        const latest = pending;
        pending = null;
        return commit(latest);
    }

    function cancel({ flush: shouldFlush = false } = {}) {
        const committed = shouldFlush ? flush() : false;
        if (frameId !== null) cancelFrame(frameId);
        frameId = null;
        pending = null;
        generation += 1;
        return committed;
    }

    return Object.freeze({
        enqueue,
        flush,
        cancel,
        hasPending: () => pending !== null,
    });
}
