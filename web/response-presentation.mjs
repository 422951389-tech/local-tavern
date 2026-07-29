const SCENE_FIELDS = Object.freeze([
    'location',
    'time_weather',
    'main_quest',
    'current_scene',
    'next_goal',
    'user_line',
]);

const CHARACTER_TEXT_FIELDS = Object.freeze([
    'name',
    'mood',
    'inner_thought',
    'outfit',
    'posture',
    'dialogue',
    'expected_effect',
]);
const SCENE_CHANGE_LABELS = Object.freeze({
    location: '地点',
    time: '时间',
    weather: '天气',
});

const TASK_LABEL = String.raw`🎯\uFE0F?\s*(?:\[?\s*主线\s*\]?\s*)?(?:任务(?:名称)?|主线任务)\s*[:：]\s*`;
const CURRENT_SCENE_LABEL = String.raw`📌\uFE0F?\s*当前场景\s*[:：]\s*`;
const NEXT_GOAL_LABEL = String.raw`➡\uFE0F?\s*下一目标\s*[:：]\s*`;
const USER_LABEL = String.raw`👤\uFE0F?\s*(?:用户|玩家)(?:\s*[:：]\s*|\s+)`;
const INNER_LABEL = String.raw`💭\uFE0F?\s*(?:内心(?:想法|活动)?|想法)\s*[:：]\s*`;
const OUTFIT_LABEL = String.raw`👗\uFE0F?\s*穿着\s*[:：]\s*`;
const POSTURE_LABEL = String.raw`🧍\uFE0F?(?:\u200D[♀♂]\uFE0F?)?\s*(?:当前)?姿势\s*[:：]\s*`;
const DIALOGUE_LABEL = String.raw`💬\uFE0F?\s*对白\s*[:：]\s*`;
const NARRATION_LABEL = String.raw`(?:📖\uFE0F?\s*|#{1,6}[ \t]+)场景旁白(?:\s*[:：]\s*)?`;
const SUGGESTIONS_LABEL = String.raw`💡\uFE0F?\s*行动建议(?:\s*[:：]\s*)?`;
const TIME_LABEL = String.raw`⏱\uFE0F?\s*(?:(?:时间(?:\s*\/\s*天气)?|时间天气|天气)\s*[:：]\s*)?`;

const KNOWN_LABELS = Object.freeze([
    TASK_LABEL,
    CURRENT_SCENE_LABEL,
    NEXT_GOAL_LABEL,
    USER_LABEL,
    INNER_LABEL,
    OUTFIT_LABEL,
    POSTURE_LABEL,
    DIALOGUE_LABEL,
    NARRATION_LABEL,
    SUGGESTIONS_LABEL,
]);

function recordValue(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function textValue(value) {
    return typeof value === 'string' ? value.trim() : '';
}

function clampAffinity(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 0;
    return Math.max(0, Math.min(100, Math.round(numeric)));
}

function matchesFor(text, source) {
    return [...text.matchAll(new RegExp(source, 'gmu'))];
}

function roleHeaderStarts(text) {
    const positions = [];
    for (const match of text.matchAll(/🎭\uFE0F?/gu)) {
        const start = match.index;
        const tail = text.slice(start + match[0].length, start + match[0].length + 160);
        const affinityOffset = tail.search(/💝\uFE0F?/u);
        if (affinityOffset < 1) continue;
        const beforeAffinity = tail.slice(0, affinityOffset);
        if (/🎭\uFE0F?/u.test(beforeAffinity)) continue;
        if (new RegExp(`${NARRATION_LABEL}|${SUGGESTIONS_LABEL}`, 'mu').test(beforeAffinity)) continue;
        positions.push(start);
    }
    return positions;
}

function protocolPositions(text, { includeLocation = true } = {}) {
    const positions = new Set(roleHeaderStarts(text));
    for (const source of KNOWN_LABELS) {
        for (const match of matchesFor(text, source)) positions.add(match.index);
    }
    if (includeLocation) {
        const location = /📍\uFE0F?/u.exec(text);
        const firstKnown = positions.size > 0 ? Math.min(...positions) : Number.POSITIVE_INFINITY;
        if (location && location.index < firstKnown) positions.add(location.index);
    }
    return [...positions].sort((left, right) => left - right);
}

function normalizeProtocolBoundaries(value) {
    const text = textValue(value);
    const positions = protocolPositions(text);
    if (positions.length === 0) return text;
    const chunks = [];
    let cursor = 0;
    for (const position of positions) {
        chunks.push(text.slice(cursor, position));
        const lineStart = text.lastIndexOf('\n', position - 1) + 1;
        if (text.slice(lineStart, position).trim()) chunks.push('\n');
        cursor = position;
    }
    chunks.push(text.slice(cursor));
    return chunks.join('');
}

function nextBoundary(text, from, extraPositions = []) {
    const positions = [...protocolPositions(text, { includeLocation: false }), ...extraPositions]
        .filter(position => position > from)
        .sort((left, right) => left - right);
    return positions.length > 0 ? positions[0] : text.length;
}

function firstLabelMatch(text, source, from = 0) {
    const regex = new RegExp(source, 'gmu');
    regex.lastIndex = from;
    return regex.exec(text);
}

function extractField(text, source) {
    const match = firstLabelMatch(text, source);
    if (!match) return '';
    const start = match.index + match[0].length;
    return text.slice(start, nextBoundary(text, start)).trim();
}

function stripEdgeSeparator(value) {
    return textValue(value).replace(/[ \t]*[|｜][ \t]*$/u, '').trim();
}

function splitSuggestions(value) {
    const text = textValue(value);
    if (!text) return [];
    const markers = [...text.matchAll(/(?:^|\s)(?:[-*•][ \t]+|\d{1,2}[.、)][ \t]*)/gmu)];
    if (markers.length > 0) {
        const items = [];
        markers.forEach((match, index) => {
            const start = match.index + match[0].length;
            const end = index + 1 < markers.length ? markers[index + 1].index : text.length;
            const item = text.slice(start, end).trim();
            if (item) items.push(item);
        });
        if (items.length > 0) return items.slice(0, 5);
    }
    return text.split(/\r?\n/u)
        .map(line => line.trim().replace(/^[-*•]\s*/u, '').replace(/^\d+[.、)]\s*/u, ''))
        .filter(Boolean)
        .slice(0, 5);
}

function parseSceneMeta(text) {
    const locationMatch = /📍\uFE0F?\s*(?:(?:场景|地点)\s*[:：]\s*)?/u.exec(text);
    if (!locationMatch) return null;
    const roleStarts = roleHeaderStarts(text).filter(position => position > locationMatch.index);
    const terminals = [
        ...matchesFor(text, NARRATION_LABEL).map(match => match.index),
        ...matchesFor(text, SUGGESTIONS_LABEL).map(match => match.index),
    ].filter(position => position > locationMatch.index);
    const sceneEnd = [...roleStarts, ...terminals].sort((left, right) => left - right)[0] || text.length;
    const block = text.slice(locationMatch.index, sceneEnd);
    const localLocation = /📍\uFE0F?\s*(?:(?:场景|地点)\s*[:：]\s*)?/u.exec(block);
    const timeMatch = firstLabelMatch(block, TIME_LABEL);
    const locationStart = localLocation.index + localLocation[0].length;
    const locationEnd = timeMatch
        ? timeMatch.index
        : nextBoundary(block, locationStart);
    let timeWeather = '';
    if (timeMatch) {
        const timeStart = timeMatch.index + timeMatch[0].length;
        timeWeather = stripEdgeSeparator(block.slice(timeStart, nextBoundary(block, timeStart)));
    }
    return {
        location: stripEdgeSeparator(block.slice(locationStart, locationEnd)),
        time_weather: timeWeather,
        main_quest: extractField(block, TASK_LABEL),
        current_scene: extractField(block, CURRENT_SCENE_LABEL),
        next_goal: extractField(block, NEXT_GOAL_LABEL),
        user_line: extractField(block, USER_LABEL),
    };
}

function stripDialogueQuotes(value) {
    const text = textValue(value);
    for (const [opening, closing] of [['"', '"'], ['“', '”'], ["'", "'"], ['‘', '’']]) {
        if (text.startsWith(opening) && text.endsWith(closing)) {
            return text.slice(opening.length, -closing.length).trim();
        }
    }
    return text;
}

function parseCharacterBlock(block) {
    const firstLineEnd = block.search(/\r?\n/u);
    const headerLine = block.slice(0, firstLineEnd < 0 ? block.length : firstLineEnd);
    let header = headerLine.replace(/^\s*🎭\uFE0F?\s*/u, '');
    header = header.replace(/^(?:(?:出场)?角色(?:名)?)(?:\s*[:：]\s*|\s+)/u, '');
    const heart = header.search(/💝\uFE0F?/u);
    if (heart >= 0) header = header.slice(0, heart);
    const name = header.trim().replace(/[|｜:：\-— ]+$/u, '').slice(0, 50);
    const affinityMatch = /💝\uFE0F?\s*(?:好感度\s*[:：]?\s*)?[^\d+\-\r\n]{0,80}([+\-]?\d+)\s*%?/u.exec(block);
    if (!name || !affinityMatch) return null;

    let dialogue = extractField(block, DIALOGUE_LABEL);
    let expectedEffect = '';
    const effect = /[ \t]*[（(][ \t]*预期影响[ \t]*[:：][ \t]*([\s\S]*?)[ \t]*[）)][ \t]*$/u.exec(dialogue);
    if (effect) {
        expectedEffect = effect[1].trim();
        dialogue = dialogue.slice(0, effect.index).trim();
    }
    return {
        name,
        affinity: clampAffinity(affinityMatch[1]),
        inner_thought: extractField(block, INNER_LABEL),
        outfit: extractField(block, OUTFIT_LABEL),
        posture: extractField(block, POSTURE_LABEL),
        dialogue: stripDialogueQuotes(dialogue),
        expected_effect: expectedEffect,
    };
}

function parseCharacters(text) {
    const starts = roleHeaderStarts(text);
    const terminals = [
        ...matchesFor(text, NARRATION_LABEL).map(match => match.index),
        ...matchesFor(text, SUGGESTIONS_LABEL).map(match => match.index),
    ];
    return starts.map((start, index) => {
        const candidates = [starts[index + 1], ...terminals.filter(position => position > start)]
            .filter(Number.isInteger)
            .sort((left, right) => left - right);
        return parseCharacterBlock(text.slice(start, candidates[0] || text.length));
    }).filter(Boolean);
}

function parseNarration(text) {
    const narration = firstLabelMatch(text, NARRATION_LABEL);
    if (!narration) return '';
    const suggestions = firstLabelMatch(text, SUGGESTIONS_LABEL, narration.index + narration[0].length);
    if (!suggestions) return '';
    return text.slice(narration.index + narration[0].length, suggestions.index).trim();
}

export function normalizeResponsePresentation(value) {
    const source = recordValue(value);
    if (source.schema_version !== undefined && source.schema_version !== 1) return null;
    const rawScene = source.scene_meta;
    const scene = rawScene && typeof rawScene === 'object' && !Array.isArray(rawScene)
        ? Object.fromEntries(SCENE_FIELDS.map(field => [field, textValue(rawScene[field])]))
        : null;
    const characters = Array.isArray(source.characters)
        ? source.characters.map(raw => {
            const character = recordValue(raw);
            const normalized = {
                ...Object.fromEntries(CHARACTER_TEXT_FIELDS.map(field => [field, textValue(character[field])])),
                affinity: clampAffinity(character.affinity),
            };
            const previousAffinity = Number(character.previous_affinity);
            if (character.previous_affinity !== null
                && character.previous_affinity !== undefined
                && Number.isFinite(previousAffinity)) {
                normalized.previous_affinity = clampAffinity(previousAffinity);
            }
            return normalized;
        }).filter(character => character.name)
        : [];
    const seenSceneChanges = new Set();
    const sceneChanges = (Array.isArray(source.scene_changes) ? source.scene_changes : [])
        .map(raw => {
            const change = recordValue(raw);
            const key = typeof change.key === 'string' ? change.key : '';
            const valueText = textValue(change.value);
            if (!Object.hasOwn(SCENE_CHANGE_LABELS, key)
                || !valueText
                || seenSceneChanges.has(key)) return null;
            seenSceneChanges.add(key);
            return Object.freeze({ key, label: SCENE_CHANGE_LABELS[key], value: valueText });
        })
        .filter(Boolean)
        .slice(0, 3);
    return Object.freeze({
        schema_version: 1,
        scene_meta: scene ? Object.freeze(scene) : null,
        characters: Object.freeze(characters.map(character => Object.freeze(character))),
        narration: textValue(source.narration),
        suggestions: Object.freeze((Array.isArray(source.suggestions) ? source.suggestions : [])
            .map(textValue).filter(Boolean).slice(0, 5)),
        warnings: Object.freeze((Array.isArray(source.warnings) ? source.warnings : [])
            .map(textValue).filter(Boolean).slice(0, 50)),
        scene_changes: Object.freeze(sceneChanges),
    });
}

export function hasPresentableResponse(value) {
    const presentation = normalizeResponsePresentation(value);
    if (!presentation) return false;
    const scene = recordValue(presentation.scene_meta);
    return Boolean(
        presentation.characters.length > 0
        || presentation.narration
        || scene.main_quest
        || scene.current_scene
        || scene.next_goal
    );
}

export function parseLegacyAssistantResponse(raw) {
    let text = textValue(raw);
    if (!text) return normalizeResponsePresentation({ schema_version: 1 });
    if (text.startsWith('```')) {
        text = text.replace(/^```\w*\r?\n?/u, '').replace(/\r?\n?```\s*$/u, '');
    }
    text = normalizeProtocolBoundaries(text);
    const suggestionsMatch = firstLabelMatch(text, SUGGESTIONS_LABEL);
    return normalizeResponsePresentation({
        schema_version: 1,
        scene_meta: parseSceneMeta(text),
        characters: parseCharacters(text),
        narration: parseNarration(text),
        suggestions: suggestionsMatch
            ? splitSuggestions(text.slice(suggestionsMatch.index + suggestionsMatch[0].length))
            : [],
        warnings: [],
    });
}

export function presentationForMessage(message) {
    const source = recordValue(message);
    const stored = normalizeResponsePresentation(source.presentation);
    if (stored && hasPresentableResponse(stored)) return stored;
    const legacy = parseLegacyAssistantResponse(source.content);
    return hasPresentableResponse(legacy) ? legacy : null;
}

export function captureResponsePresentationBaseline(session, parsedEvent, turnId = null) {
    const currentSession = recordValue(session);
    const delta = recordValue(parsedEvent && parsedEvent.session_delta);
    const previousStates = recordValue(currentSession.characters_state);
    const nextStates = recordValue(delta.characters_state);
    const canCompare = Number.isSafeInteger(parsedEvent && parsedEvent.revision)
        && Number.isSafeInteger(currentSession.revision)
        && currentSession.revision < parsedEvent.revision;
    if (!canCompare) {
        const history = Array.isArray(currentSession.message_history)
            ? currentSession.message_history
            : [];
        const matchingMessage = [...history].reverse().find(message => (
            message
            && message.role === 'assistant'
            && (!turnId || message.turn_id === turnId)
        ));
        const persisted = matchingMessage ? presentationForMessage(matchingMessage) : null;
        if (persisted) {
            const persistedCharacters = {};
            persisted.characters.forEach((character, index) => {
                persistedCharacters[`persisted-${index}`] = Object.freeze({
                    id: '',
                    name: character.name,
                    currentAffinity: character.affinity,
                    previousAffinity: character.previous_affinity ?? null,
                    mood: character.mood || '',
                });
            });
            return Object.freeze({
                characters: Object.freeze(persistedCharacters),
                sceneChanges: persisted.scene_changes,
            });
        }
    }

    const characters = {};
    for (const [id, rawState] of Object.entries(nextStates)) {
        const next = recordValue(rawState);
        const previous = recordValue(previousStates[id]);
        characters[id] = Object.freeze({
            id,
            name: textValue(next.name || previous.name || id) || id,
            currentAffinity: clampAffinity(next.affinity),
            previousAffinity: canCompare && Number.isFinite(Number(previous.affinity))
                ? clampAffinity(previous.affinity)
                : null,
            mood: textValue(next.mood),
        });
    }

    const previousScene = recordValue(currentSession.scene_meta);
    const nextScene = recordValue(delta.scene_meta);
    const sceneChanges = [];
    if (canCompare) {
        for (const [key, label] of Object.entries(SCENE_CHANGE_LABELS)) {
            const before = textValue(previousScene[key]);
            const after = textValue(nextScene[key]);
            if (after && before !== after) {
                sceneChanges.push(Object.freeze({ key, label, value: after }));
            }
        }
    }
    return Object.freeze({
        characters: Object.freeze(characters),
        sceneChanges: Object.freeze(sceneChanges),
    });
}
