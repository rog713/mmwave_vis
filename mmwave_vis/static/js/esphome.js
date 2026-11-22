// What only an ESPHome presence sensor has (built for the Apollo R PRO-1): the LD2450's
// zone type, which of the two radars sees someone, the LD2412's distances on the map,
// its gates, and buttons to restart or calibrate a radar. On an Inovelli switch none
// of this is on the page and initEsphome() does nothing.

import { state, on, emit, ESPHOME, AREAS, AREA_CATEGORY } from './state.js';
import { $, $$, h, setText, toast, setNotice, clearNotice, confirmAction } from './ui.js';
import { renderZoneList, displayZones } from './zones.js';

const GATES = 14;
const MODE_KEY = 'ld2450_zone_type';
const MODE_CONFIRM_MS = 8000;

let radar = null;
let live = {};             // latest readings from the addon (device_config.esphome)
let ld2412 = {};           // the LD2412 settings the gate chart draws (thresholds, gates in use)
let modePending = null;    // zone type sent to the sensor and not reported back yet
let modeTimer = 0;
let gateRows = [];

const info = () => (state.deviceInfo && state.deviceInfo.topic === state.device ? state.deviceInfo : null);
const lacks = key => !!info() && (info().unsupported_settings || []).includes(key);
const disabled = role => !!info() && !!(info().disabled_entities || {})[role];
const metres = cm => `${(cm / 100).toFixed(2)} m`;

// --- Zone type -------------------------------------------------------------------------
// One setting for all three zones, held by the radar itself.

const MODE_HINTS = {
    Disabled: 'The radar tracks the whole room, and each zone counts the people inside it.',
    Detection: 'The radar only tracks people inside a zone. Everywhere else is ignored.',
    Filter: 'The radar ignores everything inside a zone: use it for a fan, a curtain or a doorway.',
};

function renderMode() {
    const select = $('#zoneMode');
    const mode = modePending || state.zoneMode;
    const missing = lacks(MODE_KEY);
    select.disabled = !state.device || missing;
    select.value = mode || '';
    let hint = '';
    if (missing) {
        hint = 'The "LD2450 Zone Type" entity is disabled in Home Assistant, so the zone type can\'t be shown or changed here.';
    } else if (modePending) {
        hint = 'Sending to the sensor…';
    } else if (mode) {
        hint = MODE_HINTS[mode] || '';
        const zones = displayZones()[AREA_CATEGORY] || {};
        if (mode === 'Detection' && !AREAS.some(a => zones[a])) hint += ' There are no zones yet, so nobody shows up until you draw one.';
    }
    setText($('#zoneModeHint'), hint);
    // The legend and the zones on the map follow the type
    $('#map').dataset.zoneMode = state.zoneMode || '';
}

function modeChanged() {
    const select = $('#zoneMode');
    const value = select.value;
    if (!state.device || !value || value === state.zoneMode) { renderMode(); return; }
    modePending = value;
    state.socket.emit('update_parameters', { params: { [MODE_KEY]: value } });
    clearTimeout(modeTimer);
    const device = state.device;
    modeTimer = setTimeout(() => {
        if (state.device !== device || !modePending) return;
        modePending = null;
        renderMode();
        toast('The sensor didn\'t confirm the zone type. Press Sync to see what it has.', 'error', 6000);
    }, MODE_CONFIRM_MS);
    renderMode();
}

function modeReported(value) {
    const changed = value !== state.zoneMode;
    state.zoneMode = value;
    if (modePending === value) {
        modePending = null;
        clearTimeout(modeTimer);
        toast('Zone type saved.', 'success', 2500);
    }
    renderMode();
    if (changed) {
        if (!state.edit) renderZoneList();
        emit('zones');
    }
}

// --- Which radar sees someone ------------------------------------------------------------

function chip(id, on, name) {
    const node = $(id);
    node.classList.toggle('on', on === true);
    node.classList.toggle('unknown', on === undefined);
    node.title = `${name}: ${on === undefined ? 'no reading yet' : on ? 'someone detected' : 'clear'}`;
}

function ld2412Text() {
    if (live.ld2412_calibrating) return 'Calibrating its background…';
    if (live.ld2412_presence === undefined) return '–';
    if (!live.ld2412_presence) return 'Clear';
    const parts = ['Someone detected'];
    if (live.ld2412_moving !== false && live.ld2412_moving_distance > 0) parts.push(`moving at ${metres(live.ld2412_moving_distance)}`);
    if (live.ld2412_still !== false && live.ld2412_still_distance > 0) parts.push(`still at ${metres(live.ld2412_still_distance)}`);
    return parts.join(' · ');
}

function ld2450Text() {
    if (live.ld2450_presence === undefined) return '–';
    if (!live.ld2450_presence) return 'Clear';
    const n = state.targetCount;
    return n ? `Tracking ${n} ${n === 1 ? 'person' : 'people'}` : 'Someone detected';
}

function renderRadars() {
    chip('#chipLd2450', live.ld2450_presence, 'LD2450 tracking radar');
    chip('#chipLd2412', live.ld2412_presence, 'LD2412 presence radar');
    setText($('#factLd2450'), ld2450Text());
    setText($('#factLd2412'), ld2412Text());

    const ranges = [];
    if (live.ld2412_presence !== false) {
        if (live.ld2412_moving !== false && live.ld2412_moving_distance > 0) {
            ranges.push({ r: live.ld2412_moving_distance, cls: 'moving', label: `LD2412 moving ${metres(live.ld2412_moving_distance)}` });
        }
        if (live.ld2412_still !== false && live.ld2412_still_distance > 0) {
            ranges.push({ r: live.ld2412_still_distance, cls: 'still', label: `LD2412 still ${metres(live.ld2412_still_distance)}` });
        }
    }
    radar.setRanges(ranges);

    // Apollo's firmware ships the distance sensors disabled
    const off = ['ld2412_moving_distance', 'ld2412_still_distance'].filter(disabled);
    setText($('#radarHint'), off.length
        ? 'To see how far away the LD2412 detects someone, here and as arcs on the map, enable "LD2412 Moving Distance" and "LD2412 Still Distance" on the device\'s page in Home Assistant, then press Sync.'
        : '');
}

// --- LD2412 gates ----------------------------------------------------------------------------
// The LD2412 splits its range into 14 gates and reports motion or stillness in a gate
// when the energy it measures there passes that gate's threshold. With engineering mode
// on it reports every gate's energy, drawn here against the thresholds.

const pad = g => String(g).padStart(2, '0');

function bar(kind) {
    const fill = h('i', { class: 'fill' });
    const mark = h('i', { class: 'mark' });
    const value = h('b', { class: 'num' }, '–');
    return { el: h('span', { class: `gate-bar ${kind}` }, fill, mark, value), fill, mark, value };
}

function buildGates() {
    gateRows = Array.from({ length: GATES }, (_, g) => {
        const label = h('span', { class: 'gate-label' });
        const move = bar('move'), still = bar('still');
        return { g, label, move, still, el: h('div', { class: 'gate-row' }, label, move.el, still.el) };
    });
    $('#gateChart').replaceChildren(
        h('div', { class: 'gate-row gate-head' }, h('span', null, 'Gate'), h('span', null, 'Motion'), h('span', null, 'Stillness')),
        ...gateRows.map(r => r.el));
}

function renderBar(b, energy, threshold, what) {
    const has = Number.isFinite(energy);
    b.fill.style.width = `${has ? Math.min(100, Math.max(0, energy)) : 0}%`;
    b.mark.hidden = !Number.isFinite(threshold);
    if (Number.isFinite(threshold)) b.mark.style.left = `${Math.min(100, Math.max(0, threshold))}%`;
    setText(b.value, has ? String(energy) : '–');
    b.el.classList.toggle('hot', has && Number.isFinite(threshold) && energy > threshold);
    b.el.title = `${what}: energy ${has ? energy : 'not reported'}${Number.isFinite(threshold) ? `, threshold ${threshold}` : ''}`;
}

function renderGates() {
    const res = parseFloat(ld2412.ld2412_distance_resolution) || 0.75;
    const first = Number(ld2412.ld2412_min_distance_gate), last = Number(ld2412.ld2412_max_distance_gate);
    const moves = live.gate_move || [], stills = live.gate_still || [];
    for (const r of gateRows) {
        const { g } = r;
        setText(r.label, `${g} · ${+(g * res).toFixed(2)}–${+((g + 1) * res).toFixed(2)} m`);
        // Gates outside the nearest and farthest gate in use are ignored by the radar
        r.el.classList.toggle('unused', (Number.isFinite(first) && g < first) || (Number.isFinite(last) && g > last));
        renderBar(r.move, moves[g], Number(ld2412[`ld2412_g${pad(g)}_move_threshold`] ?? NaN), `Gate ${g} motion`);
        renderBar(r.still, stills[g], Number(ld2412[`ld2412_g${pad(g)}_still_threshold`] ?? NaN), `Gate ${g} stillness`);
    }
    const reporting = moves.some(Number.isFinite) || stills.some(Number.isFinite);
    let note = 'A bar turns solid when its energy passes the threshold (the line). Change thresholds in the Config tab.';
    if (lacks('ld2412_engineering_mode') && !reporting) note = 'The LD2412\'s entities aren\'t available in Home Assistant.';
    else if (ld2412.ld2412_engineering_mode === 'OFF') note = 'Turn on engineering mode to see the energy in each gate. The lines are the thresholds.';
    else if (!reporting) note = 'Waiting for the radar\'s gate energies…';
    setText($('#gateNote'), note);
}

// --- Buttons on the sensor ----------------------------------------------------------------------

const BUTTONS = {
    ld2450_restart: { done: 'Restarting the LD2450…' },
    ld2412_restart: { done: 'Restarting the LD2412…' },
    ld2412_calibrate: {
        ask: 'Start the LD2412\'s background calibration? It measures the empty room, so leave the room and keep it still until the LD2412 line above stops saying it\'s calibrating.',
        label: 'Start', done: 'Calibration started. Keep the room empty until it finishes.',
    },
    ld2450_factory_reset: {
        ask: 'Reset the LD2450 to its factory settings? Its zones and zone type are cleared.',
        label: 'Reset', danger: true, done: 'Resetting the LD2450…',
    },
    ld2412_factory_reset: {
        ask: 'Reset the LD2412 to its factory settings? Every gate threshold goes back to its default.',
        label: 'Reset', danger: true, done: 'Resetting the LD2412…',
    },
};

async function press(button) {
    const b = BUTTONS[button];
    if (!b) return;
    if (!state.device) { toast('Choose a sensor first.', 'error'); return; }
    const device = state.device;
    if (b.ask && !await confirmAction(b.ask, { confirm: b.label, danger: !!b.danger })) return;
    if (state.device !== device) return;
    state.socket.emit('press_button', button);
    toast(b.done, 'info', 5000);
}

// --- Notices ---------------------------------------------------------------------------------------

function renderNotices() {
    if (state.device && live.available === false) {
        setNotice('esphomeOffline', {
            tone: 'danger', title: 'The sensor is offline in Home Assistant.',
            text: 'Its entities are unavailable. Check its power and network connection.',
        });
    } else {
        clearNotice('esphomeOffline');
    }
    const i = info();
    if (i && (i.missing_tracking || []).length) {
        setNotice('esphomeEntities', {
            tone: 'warning', title: 'The LD2450\'s position entities are disabled in Home Assistant,',
            text: 'so nobody can be drawn on the map. Enable "LD2450 Target-1 X" and "LD2450 Target-1 Y" (and targets 2 and 3) on the device\'s page, wait half a minute, then press Sync.',
        });
    } else {
        clearNotice('esphomeEntities');
    }
}

// --- Wiring --------------------------------------------------------------------------------------------

function fromSensor(c) {
    if (MODE_KEY in c) modeReported(c[MODE_KEY]);
    let gates = false;
    for (const [key, value] of Object.entries(c)) {
        if (key.startsWith('ld2412_') && ld2412[key] !== value) {
            ld2412[key] = value;
            gates = true;
        }
    }
    if (c.esphome) {
        Object.assign(live, c.esphome);
        if ('gate_move' in c.esphome || 'gate_still' in c.esphome) gates = true;
        renderRadars();
        if ('available' in c.esphome) renderNotices();
    }
    if (gates) renderGates();
}

function deviceChanged() {
    live = {};
    ld2412 = {};
    modePending = null;
    clearTimeout(modeTimer);
    state.zoneMode = null;
    renderMode();
    renderRadars();
    renderGates();
    renderNotices();
}

export function initEsphome(radarInstance) {
    if (!ESPHOME) return;
    radar = radarInstance;
    buildGates();
    $('#zoneMode').addEventListener('change', modeChanged);
    $$('[data-button]').forEach(btn => btn.addEventListener('click', () => press(btn.dataset.button)));
    state.socket.on('command_error', data => {
        const keys = (data && (Array.isArray(data.params) ? data.params : [data.param])) || [];
        if (modePending && keys.includes(MODE_KEY)) {
            modePending = null;
            clearTimeout(modeTimer);
            renderMode();
        }
    });
    on('device-config', fromSensor);
    on('device', deviceChanged);
    on('device-info', () => { renderMode(); renderRadars(); renderGates(); renderNotices(); });
    on('zones', renderMode);            // "no zones yet" under the Detection type
    on('targets', renderRadars);        // how many people the LD2450 is tracking
    deviceChanged();
}
