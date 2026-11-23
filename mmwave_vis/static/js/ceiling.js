// A sensor that looks down from a ceiling.
//
// The LD2450 measures one angle (left to right) and a distance. Looking down, its x is
// still a distance across the floor, but what it calls y is the distance from the sensor
// in every other direction: down as well as to either side. Someone right under the
// sensor reads y = `drop`, the gap between the sensor and a standing person, and someone
// `s` cm to the side of the radar's left-right line reads y = √(s² + drop²), whichever
// side they're on.
//
// With `layout.ceiling` set, the map is drawn as the floor from above: these turn the
// radar's y into that distance to the side and back. Zones, targets and everything the
// sensor is sent stay in the radar's own numbers.

import { state } from './state.js';

const ON_LINE_CM = 3;   // this close to the left-right line counts as on it

// The radar's y → cm to the side of its left-right line
export function floorFromRaw(y, drop) {
    return drop > 0 ? Math.sqrt(Math.max(0, y * y - drop * drop)) : y;
}

// ... and back. On the line means "from the sensor itself": a zone that starts there also
// takes anyone who reads closer than `drop` (someone tall, an arm in the air).
export function rawFromFloor(s, drop) {
    s = Math.abs(s);
    if (!(drop > 0)) return s;
    return s < ON_LINE_CM ? 0 : Math.sqrt(s * s + drop * drop);
}

// For the page's numbers (zone list, zone editor, table, pointer): whole centimetres, and
// the radar's own y when the sensor isn't on a ceiling
export const ceilingOn = () => !!state.layout.ceiling;
const drop = () => (state.layout.ceiling || {}).drop || 0;
export const shownY = y => ceilingOn() ? Math.round(floorFromRaw(Number(y), drop())) : y;
export const radarY = s => ceilingOn() ? Math.round(rawFromFloor(Number(s), drop())) : s;
