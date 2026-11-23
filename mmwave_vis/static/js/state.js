// State shared by the page's modules, plus a minimal event bus so the map,
// zone list and editor can react to each other without importing each other.

// Which kind of device the addon is set up for: 'z2m' or 'zha' (Inovelli switches), or
// 'esphome' (Apollo R PRO-1: an LD2450 that tracks positions and an LD2412 beside it).
// The addon puts it on <body>, and it can't change without a reload.
export const STACK = (document.body.className.match(/stack-(\w+)/) || [])[1] || null;
export const ESPHOME = STACK === 'esphome';
// What messages call the device
export const DEV = ESPHOME ? 'sensor' : 'switch';
export const DEVS = ESPHOME ? 'sensors' : 'switches';

// `noun`: what one zone of the type is called ("Add area", "No detection areas").
// The LD2450 has one kind of zone, and its zone type applies to all three at once: with
// Filter they're drawn like interference areas (ignored), otherwise like detection areas.
export const ZONE_TYPES = ESPHOME ? [
    { category: 'ld2450_zones', group: 'detection', title: 'Zones', name: n => `Zone ${n}`, noun: 'zone', none: 'No zones',
        get cls() { return state.zoneMode === 'Filter' ? 'intf' : 'det'; } },
] : [
    { category: 'mmwave_detection_areas',    group: 'detection',    title: 'Detection',    name: n => `Area ${n}`,         cls: 'det',  noun: 'area', none: 'No detection areas' },
    { category: 'mmwave_interference_areas', group: 'interference', title: 'Interference', name: n => `Interference ${n}`, cls: 'intf', noun: 'area', none: 'No interference areas' },
    { category: 'mmwave_stay_areas',         group: 'stay',         title: 'Stay',         name: n => `Stay ${n}`,         cls: 'stay', noun: 'area', none: 'No stay areas' },
];
export const ZONE_TYPE = Object.fromEntries(ZONE_TYPES.map(t => [t.category, t]));
// The zones that have an occupancy sensor each (the chips above the map)
export const AREA_CATEGORY = ZONE_TYPES[0].category;
export const AREAS = ESPHOME ? ['area1', 'area2', 'area3'] : ['area1', 'area2', 'area3', 'area4'];
// The LD2450 sees a flat floor plan: its zones have no height
export const HAS_HEIGHT = !ESPHOME;
export const ZONE_KEYS = HAS_HEIGHT ? ['x_min', 'x_max', 'y_min', 'y_max', 'z_min', 'z_max'] : ['x_min', 'x_max', 'y_min', 'y_max'];
// What a zone edge can be set to (cm), or null for no limit worth enforcing here
export const ZONE_LIMITS = ESPHOME ? { x: [-486, 486], y: [0, 756] } : null;

export function emptyZones() {
    return Object.fromEntries(ZONE_TYPES.map(t => [t.category, Object.fromEntries(AREAS.map(a => [a, null]))]));
}

// When each zone type last arrived from the switch (0 = not since the device was selected).
// An empty [] zone list is the backend's "nothing reported yet" placeholder and doesn't count.
export function freshReportTimes() {
    return Object.fromEntries(ZONE_TYPES.map(t => [t.category, 0]));
}

// The slot's own name ("Area 2"), whatever the user called it
export function slotName(category, area) {
    return ZONE_TYPE[category].name(area.slice(4));
}

// The name the user gave a zone, or ''
export function customName(category, area) {
    return (state.zoneNames && state.zoneNames[`${category}:${area}`]) || '';
}

// For messages and lists: "Couch (Area 2)", or "Area 2" when it has no name
export function zoneName(category, area) {
    const custom = customName(category, area);
    const slot = slotName(category, area);
    return custom ? `${custom} (${slot})` : slot;
}

export const state = {
    socket: null,
    stack: null,              // 'z2m' | 'zha' | 'esphome' | null
    device: '',               // selected device topic
    deviceName: '',           // what the switch list calls it
    devices: [],              // the switch list from the addon (device_list)
    zones: emptyZones(),
    zoneReportAt: freshReportTimes(),
    zoneNames: {},            // "category:area" → name (display only, saved by the addon)
    areaOccupied: [false, false, false, false],
    occupied: null,           // the switch's overall occupancy: true / false / null (not reported yet)
    occupiedAt: 0,            // when it last changed
    targetCount: 0,           // targets in the latest report
    settings: {},             // the switch's latest reported settings (mmWaveHoldTime, ...)
    deviceInfo: null,         // firmware, link quality and HA entities (device_info event)
    radarReports: true,       // false when a ZHA quirk other than the Visualizer's runs the switch: no targets, areas or zones
    quirk: null,              // on ZHA: 'visualizer' | 'builtin' | 'custom' | 'none' | null (unknown)
    pending: {},              // "category:area" → saved zone (or null for a delete) the switch hasn't confirmed yet
    edit: null,               // { category, area, draft, isNew } while a zone is being edited
    arranging: false,         // room layout mode
    replay: null,             // a recorded clip being played back instead of live data
    restoring: false,         // a backup restore or multi-zone undo is writing slots
    layout: { x: 0, y: 0, rot: 0, room: null, mirror: false, ceiling: null },
    stayInvert: false,
    zoneMode: null,           // ESPHome: the LD2450's zone type, 'Disabled' | 'Detection' | 'Filter'
    lastCommandId: null,      // 1 = auto-detect interference, 3 = clear interference
    lastCommandAt: 0,
};

const listeners = new Map();

export function on(event, fn) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
}

export function emit(event, data) {
    const fns = listeners.get(event);
    if (fns) for (const fn of fns) fn(data);
}
