'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const { ApiClient } = require('../web/api-client.js');
const {
    TurnClient,
    createTurnState,
    reduceTurnEvent,
    canPerformAction,
} = require('../web/turn-client.js');

function publicTurn(overrides = {}) {
    const turnId = overrides.turn_id || '11111111-1111-4111-8111-111111111111';
    return {
        schema_version: 1,
        turn_id: turnId,
        project: 'project-a',
        save: 'save-a',
        status: 'pending',
        content: '',
        thinking: '',
        error: null,
        last_event_id: 0,
        events_url: `/api/chat/turns/${turnId}/events`,
        cancel_url: `/api/chat/turns/${turnId}/cancel`,
        ...overrides,
    };
}

test('turn reducer de-duplicates replay and records one terminal', () => {
    let state = createTurnState(publicTurn(), Object.freeze({ project: 'project-a', save: 'save-a', epoch: 1 }));
    let reduced = reduceTurnEvent(state, { id: 1, type: 'started' });
    state = reduced.state;
    reduced = reduceTurnEvent(state, { id: 2, type: 'content', content: '甲' });
    state = reduced.state;
    const duplicate = reduceTurnEvent(state, { id: 2, type: 'content', content: '甲' });
    assert.equal(duplicate.accepted, false);
    assert.equal(duplicate.duplicate, true);
    assert.equal(duplicate.state.content, '甲');

    reduced = reduceTurnEvent(state, { id: 3, type: 'content', content: '乙' });
    state = reduced.state;
    reduced = reduceTurnEvent(state, { id: 4, type: 'terminal', status: 'completed', error: null });
    state = reduced.state;
    assert.equal(state.content, '甲乙');
    assert.equal(state.terminal, true);
    assert.equal(state.terminalCount, 1);

    const lateTerminal = reduceTurnEvent(state, { id: 5, type: 'terminal', status: 'failed', error: {} });
    assert.equal(lateTerminal.accepted, false);
    assert.equal(lateTerminal.state.terminalCount, 1);
});

test('turn reducer reports event gaps and action policy blocks conflicting writes', () => {
    const initial = createTurnState(publicTurn(), { project: 'project-a', save: 'save-a', epoch: 1 });
    const jumped = reduceTurnEvent(initial, { id: 3, type: 'content', content: 'late' });
    assert.equal(jumped.gap, true);
    for (const action of ['send', 'model', 'reset', 'rename', 'delete', 'restore', 'message_edit', 'pin', 'include', 'regenerate', 'project_switch', 'save_switch']) {
        assert.equal(canPerformAction(action, 'streaming'), false, action);
    }
    assert.equal(canPerformAction('cancel', 'streaming'), true);
    assert.equal(canPerformAction('reset', 'completed'), true);
});

test('TurnClient resumes with after cursor and validates SSE ids', async () => {
    const encoder = new TextEncoder();
    let requestedUrl = '';
    const api = new ApiClient({
        fetchImpl: async (url) => {
            requestedUrl = url;
            const body = new ReadableStream({
                start(controller) {
                    controller.enqueue(encoder.encode(
                        'id: 3\nevent: content\ndata: {"id":3,"type":"content","content":"乙"}\n\n'
                        + 'id: 4\nevent: terminal\ndata: {"id":4,"type":"terminal","status":"completed","error":null}\n\n',
                    ));
                    controller.close();
                },
            });
            return new Response(body, { status: 200 });
        },
    });
    const client = new TurnClient(api);
    const events = [];
    for await (const event of client.events('turn-a', { after: 2 })) events.push(event);
    assert.match(requestedUrl, /events\?after=2$/);
    assert.deepEqual(events.map(event => event.id), [3, 4]);
});

test('TurnClient coalesces double cancel and respects completed winning the race', async () => {
    let calls = 0;
    let resolveRequest;
    const pending = new Promise(resolve => { resolveRequest = resolve; });
    const api = new ApiClient({
        fetchImpl: async () => {
            calls += 1;
            await pending;
            return new Response(JSON.stringify(publicTurn({ status: 'completed' })), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            });
        },
    });
    const client = new TurnClient(api);
    const first = client.cancel('turn-a');
    const second = client.cancel('turn-a');
    assert.strictEqual(first, second);
    resolveRequest();
    const [left, right] = await Promise.all([first, second]);
    assert.equal(calls, 1);
    assert.equal(left.status, 'completed');
    assert.equal(right.status, 'completed');
});
