// The selected switch's name in the switch list, firmware, mmWave module version,
// Zigbee signal and firmware updates (top of the Config tab, and a line in the
// status popover).

import { state, on, emit, ESPHOME, DEV } from './state.js';
import { $, h, setText, toast, copyText, shortDateTime } from './ui.js';
import { cleanName } from './names.js';

const WEAK_LQI = 80;
const MAX_NAME = 60;
const nameInput = $('#switchName');

// Which ZHA quirk runs the switch (device_info.quirk_kind)
const QUIRKS = {
    visualizer: "The Visualizer's",
    builtin: "Home Assistant's built-in",
    custom: 'Another custom quirk',
    none: 'None',
};

export function deviceInfoFromServer(info) {
    if (!info || info.topic !== state.device) return;
    state.deviceInfo = info;
    render();
    emit('device-info');
}

export function clearDeviceInfo() {
    state.deviceInfo = null;
    render();
}

function firmwareText(info) {
    const fw = info.firmware || {};
    if (fw.build && fw.ota) return `${fw.build} (${fw.ota})`;
    return fw.build || fw.ota || 'Not reported';
}

function signalText(info) {
    const { lqi, rssi } = info.link || {};
    if (lqi == null && rssi == null) return null;
    const parts = [];
    if (lqi != null) parts.push(`LQI ${lqi}`);
    if (rssi != null) parts.push(`RSSI ${rssi} dBm`);
    return parts.join(' · ');
}

// `wide` puts the value on its own line under the label (long entity IDs)
function row(label, value, extra = null, wide = false) {
    const cls = wide ? 'wide' : null;
    return [h('dt', { class: cls }, label), h('dd', { class: cls }, value, extra)];
}

function render() {
    const root = $('#switchInfo');
    const info = state.deviceInfo && state.deviceInfo.topic === state.device ? state.deviceInfo : null;
    setText($('#popFirmware'), info ? firmwareText(info) : '–');
    if (!state.device) { root.replaceChildren(h('p', { class: 'hint' }, `Choose a ${DEV}.`)); return; }
    if (!info) { root.replaceChildren(h('p', { class: 'hint' }, `Reading the ${DEV}'s details…`)); return; }

    const items = [];
    if (info.model) items.push(...row('Model', info.model));
    items.push(...row('Firmware', firmwareText(info)));
    if (info.mmwave_version) {
        items.push(...row(ESPHOME ? 'Radar firmware' : 'mmWave module', h('span', { title: info.mmwave_version.raw }, info.mmwave_version.text)));
    }
    if (info.ip_address) items.push(...row('IP address', info.ip_address));
    const signal = signalText(info);
    if (signal) {
        const weak = info.link.lqi != null && info.link.lqi < WEAK_LQI;
        items.push(...row('Signal', h('span', { class: weak ? 'warn-text' : null }, signal)));
    }
    if (info.last_seen) items.push(...row('Last seen', shortDateTime(info.last_seen * 1000)));
    if (state.stack === 'zha' && QUIRKS[info.quirk]) {
        items.push(...row('ZHA quirk', h('span', { class: info.quirk === 'visualizer' ? null : 'warn-text', title: info.quirk_class || '' },
            QUIRKS[info.quirk])));
    }
    const up = info.update;
    if (up && up.latest) {
        items.push(...row('Update', up.in_progress ? 'Installing…'
            : up.available ? h('strong', { class: 'warn-text' }, `${up.latest} available`) : 'Up to date'));
    }
    // An ESPHome sensor has a presence entity per radar
    const entities = info.entities || {};
    const presence = ESPHOME
        ? [['LD2450 presence entity', entities.ld2450_presence], ['LD2412 presence entity', entities.ld2412_presence]]
        : [['Occupancy entity', entities.occupancy]];
    for (const [label, id] of presence) {
        if (!id) continue;
        items.push(...row(label, h('code', null, id),
            h('button', { class: 'btn link', type: 'button', onclick: async () => {
                if (await copyText(id)) toast(`Copied ${id}.`, 'success', 2500);
            } }, 'Copy'), true));
    }

    const notes = [];
    if (info.link && info.link.lqi != null && info.link.lqi < WEAK_LQI) {
        notes.push(h('p', { class: 'hint' }, 'The Zigbee signal is weak, which can make tracking choppy. A mains-powered Zigbee device between the switch and your coordinator helps.'));
    }
    const unsupported = (info.unsupported_settings || []).length;
    if (state.stack === 'zha' && unsupported) {
        notes.push(h('p', { class: 'hint' }, `This switch's quirk doesn't have ${unsupported === 1 ? '1 setting' : `${unsupported} settings`}, so ${unsupported === 1 ? "it's" : "they're"} greyed out below.`));
    }
    if (ESPHOME && info.found === false) {
        notes.push(h('p', { class: 'hint warn-text' }, 'Home Assistant doesn\'t list this sensor at the moment. Check the ESPHome integration, then press Sync.'));
    }
    if (ESPHOME && unsupported) {
        notes.push(h('p', { class: 'hint' }, `${unsupported === 1 ? '1 setting is' : `${unsupported} settings are`} greyed out below because ${unsupported === 1 ? 'its entity is' : 'their entities are'} disabled in Home Assistant. Enable them on the device's page there, wait half a minute, then press Refresh.`));
    }
    if (!info.ha_available) {
        notes.push(h('p', { class: 'hint' }, ESPHOME ? 'The addon has no Home Assistant token, so it can\'t reach the sensor.'
            : 'Running without Home Assistant, so update and entity details aren\'t available.'));
    } else if (info.ha_error) {
        notes.push(h('p', { class: 'hint' }, `Couldn't read Home Assistant's device list: ${info.ha_error}`));
    }

    const links = [];
    if (info.ha_device_id) {
        links.push(h('a', { class: 'btn small', href: `/config/devices/device/${encodeURIComponent(info.ha_device_id)}`, target: '_top' }, 'Open in Home Assistant'));
    }
    links.push(h('button', { class: 'btn small ghost', type: 'button', onclick: refresh }, 'Refresh'));

    root.replaceChildren(h('dl', { class: 'facts' }, ...items), ...notes, h('div', { class: 'button-row' }, ...links));
}

function refresh() {
    if (!state.device) return;
    state.socket.emit('refresh_device_info', state.device);
    toast(`Reading the ${DEV}'s details again…`, 'info', 2500);
}

// --- Name in the switch list (display only; the addon keeps it for every browser) ---

const listEntry = () => state.devices.find(d => d.topic === state.device) || null;

function renderName() {
    const d = listEntry();
    nameInput.disabled = !d;
    if (document.activeElement !== nameInput) nameInput.value = (d && d.name) || '';
    nameInput.placeholder = d ? d.default_label : '';
    let hint = '';
    if (d) {
        hint = `Shown in the ${DEV} list. Leave it empty to use ` + (state.stack === 'zha' || ESPHOME ? 'its name in Home Assistant.'
            : d.description ? 'its description in Zigbee2MQTT.' : 'its name in Zigbee2MQTT.');
    }
    setText($('#switchNameHint'), hint);
}

function saveName() {
    const d = listEntry();
    if (!d) return;
    const name = cleanName(nameInput.value, MAX_NAME);
    nameInput.value = name;
    if (name === (d.name || '')) return;
    state.socket.emit('save_switch_name', { topic: d.topic, name: name || null }, ack => {
        if (ack && ack.error) toast(`The name wasn't saved: ${ack.error}`, 'error', 6000);
        else if (ack && ack.warning) toast(`Name saved for now. ${ack.warning}`, 'error', 8000);
    });
}

export function initSwitchInfo() {
    render();
    nameInput.addEventListener('change', saveName);
    nameInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') nameInput.blur();
        else if (e.key === 'Escape') {
            nameInput.value = (listEntry() || {}).name || '';
            nameInput.blur();
            e.preventDefault();   // used up: layout mode and a replay leave this Escape be
        }
    });
    on('device', renderName);
    on('devices', renderName);
    renderName();
}
