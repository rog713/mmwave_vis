"""ESPHome presence sensors, read and written through their Home Assistant entities.

Built for the Apollo Automation R PRO-1, which pairs an HLK-LD2450 (tracks up to
three people as x/y positions and holds three rectangular zones) with an
HLK-LD2412 (distance only, strong on someone sitting still). Any ESPHome device
whose LD2450 entities carry the same names is picked up the same way.

Everything here is pure, so it can be tested without Home Assistant: which
entity is which, units, targets, zones and the settings catalogue
(static/settings_esphome.json, which the page builds its Config tab from).

The page works in whole centimetres with the sensor at the origin: x across,
y straight out from the sensor. The LD2450's entities are in millimetres, or in
whatever unit Home Assistant shows them in (inches on a US-customary install),
so every value is converted by its own unit_of_measurement both ways.
"""
import json
import math
import os
import re

CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'settings_esphome.json')

ZONE_CATEGORY = 'ld2450_zones'
ZONE_COUNT = 3
TARGET_COUNT = 3
GATE_COUNT = 14
ZONE_COORDS = ('x1', 'y1', 'x2', 'y2')
# What the LD2450's zone numbers accept (ESPHome: x ±4860 mm, y 0 to 7560 mm)
ZONE_X_LIMIT_CM = 486
ZONE_Y_LIMIT_CM = 756


# ---------------------------------------------------------------------------
# Settings catalogue
# ---------------------------------------------------------------------------

def load(path=CATALOG_PATH):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


CATALOG = load()
SETTINGS = {s['key']: s for s in CATALOG['settings']}


def setting(key):
    return SETTINGS.get(key) if isinstance(key, str) else None


def _as_int(value):
    """An integer from an int, a whole float or a string of digits; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r'-?\d+', value.strip()):
        return int(value.strip())
    return None


def validate(key, value):
    """(ok, error) for writing `value` to setting `key`."""
    s = setting(key)
    if s is None:
        return False, f"Unknown setting: {key}"
    if s.get('options'):
        if not isinstance(value, str) or not any(o['value'] == value for o in s['options']):
            return False, f"Invalid value '{value}' for {s['title']}. Allowed: {[o['value'] for o in s['options']]}"
        return True, None
    n = _as_int(value)
    if n is None:
        return False, f"{s['title']} takes a whole number, got: {value}"
    if n < s['min'] or n > s['max']:
        return False, f"{s['title']} takes {s['min']} to {s['max']}, got {n}"
    return True, None


def setting_from_state(key, state):
    """The page's value for a setting from its entity's state, or None if it has none."""
    s = setting(key)
    if s is None or not known(state):
        return None
    if s['control'] == 'toggle':
        return {'on': 'ON', 'off': 'OFF'}.get(str(state).lower())
    if s.get('options'):
        return str(state)
    n = number(state)
    return None if n is None else int(round(n))


def service_call(key, value):
    """(domain, service, service_data) that writes a validated value to a setting's entity."""
    s = SETTINGS[key]
    domain = s['entity'].split('.', 1)[0]
    if domain == 'switch':
        return 'switch', 'turn_on' if value == 'ON' else 'turn_off', {}
    if domain == 'select':
        return 'select', 'select_option', {'option': value}
    return 'number', 'set_value', {'value': _as_int(value)}


# ---------------------------------------------------------------------------
# Which entity is which
# ---------------------------------------------------------------------------
# ESPHome names an entity once, in the device's YAML ("LD2450 Target-1 X"), and
# Home Assistant derives three things from it: the registry's original_name, the
# object id inside the unique_id ("<mac>-sensor-ld2450_target-1_x") and the
# entity_id ("sensor.apollo_r_pro_1_ab12cd_ld2450_target_1_x"). People rename
# entities and devices, so all three are tried, most trustworthy first.

def slug(text):
    """'LD2450 Target-1 X' and 'ld2450_target-1_x' → 'ld2450_target_1_x'."""
    return re.sub(r'[^a-z0-9]+', '_', str(text or '').lower()).strip('_')


def _role_table():
    roles = {}   # (Home Assistant domain, slug of the ESPHome name) → role

    def add(domain, name, role):
        roles[(domain, slug(name))] = role

    for n in range(1, TARGET_COUNT + 1):
        for field in ('x', 'y', 'speed', 'angle', 'distance', 'resolution'):
            add('sensor', f'LD2450 Target-{n} {field}', f't{n}_{field}')
    for n in range(1, ZONE_COUNT + 1):
        for c in ZONE_COORDS:
            add('number', f'LD2450 Zone-{n} {c}', f'z{n}_{c}')
        add('sensor', f'LD2450 Zone-{n} All Target Count', f'z{n}_count')
        add('sensor', f'LD2450 Zone-{n} Still Target Count', f'z{n}_still_count')
        add('sensor', f'LD2450 Zone-{n} Moving Target Count', f'z{n}_moving_count')
    add('sensor', 'LD2450 Presence Target Count', 'target_count')
    for radar in ('LD2450', 'LD2412'):
        r = radar.lower()
        add('binary_sensor', f'{radar} Presence', f'{r}_presence')
        add('binary_sensor', f'{radar} Moving Target', f'{r}_moving')
        add('binary_sensor', f'{radar} Still Target', f'{r}_still')
        add('sensor', f'{radar} Firmware', f'{r}_firmware')
        add('button', f'{radar} Restart', f'{r}_restart')
        add('button', f'{radar} Factory Reset', f'{r}_factory_reset')
    add('sensor', 'LD2412 Moving Distance', 'ld2412_moving_distance')
    add('sensor', 'LD2412 Still Distance', 'ld2412_still_distance')
    add('sensor', 'LD2412 Detection Distance', 'ld2412_detection_distance')
    add('sensor', 'LD2412 Move Energy', 'ld2412_move_energy')
    add('sensor', 'LD2412 Still Energy', 'ld2412_still_energy')
    for g in range(GATE_COUNT):
        add('sensor', f'LD2412 g{g:02d} move energy', f'g{g}_move')
        add('sensor', f'LD2412 g{g:02d} still energy', f'g{g}_still')
    add('button', 'LD2412 Start Dynamic Background Correction', 'ld2412_calibrate')
    add('binary_sensor', 'LD2412 Dynamic Background Correction Status', 'ld2412_calibrating')
    add('sensor', 'LTR390 Light', 'illuminance')
    add('sensor', 'Apollo Firmware Version', 'firmware')
    add('sensor', 'ESPHome Version', 'esphome_version')
    add('sensor', 'IP Address', 'ip_address')
    add('update', 'Firmware Update', 'update')
    for s in CATALOG['settings']:
        domain, name = s['entity'].split('.', 1)
        add(domain, name, s['key'])
    return roles


ROLES = _role_table()
_SLUGS = {}   # domain → its slugs, longest first, for matching the end of an entity_id
for (_domain, _slug) in ROLES:
    _SLUGS.setdefault(_domain, []).append(_slug)
for _list in _SLUGS.values():
    _list.sort(key=len, reverse=True)

TARGET_ROLES = frozenset(f't{n}_{f}' for n in range(1, TARGET_COUNT + 1) for f in ('x', 'y', 'speed'))
ZONE_ROLES = frozenset(f'z{n}_{c}' for n in range(1, ZONE_COUNT + 1) for c in ZONE_COORDS)
ZONE_COUNT_ROLES = {f'z{n}_count': n for n in range(1, ZONE_COUNT + 1)}
PRESENCE_ROLES = ('ld2450_presence', 'ld2412_presence')
# Live readings the page shows beside the map; sent under "esphome" in device_config
LIVE_FLAGS = ('ld2450_presence', 'ld2450_moving', 'ld2450_still',
              'ld2412_presence', 'ld2412_moving', 'ld2412_still', 'ld2412_calibrating')
LIVE_DISTANCES = ('ld2412_moving_distance', 'ld2412_still_distance', 'ld2412_detection_distance')
LIVE_PERCENTS = ('ld2412_move_energy', 'ld2412_still_energy')
GATE_ROLES = frozenset(f'g{g}_{kind}' for g in range(GATE_COUNT) for kind in ('move', 'still'))
BUTTONS = ('ld2450_restart', 'ld2450_factory_reset', 'ld2412_restart', 'ld2412_factory_reset', 'ld2412_calibrate')

_UNIQUE_ID = re.compile(r'^[0-9a-f:]{12,17}(?:@\w+)?-[a-z_]+-(.+)$', re.IGNORECASE)


def role_of(entry):
    """The role of one entity registry entry, or None."""
    entity_id = entry.get('entity_id') or ''
    domain, _, object_id = entity_id.partition('.')
    if not object_id:
        return None
    for name in (entry.get('original_name'), (_UNIQUE_ID.match(entry.get('unique_id') or '') or [None, None])[1]):
        role = ROLES.get((domain, slug(name))) if name else None
        if role:
            return role
    # The entity_id ends with the entity's name after the device's: longest name first, so
    # "..._ld2450_zone_1_still_target_count" isn't taken for "..._still_target_count"
    for candidate in _SLUGS.get(domain, ()):
        if object_id == candidate or object_id.endswith('_' + candidate):
            return ROLES[(domain, candidate)]
    return None


def entity_roles(entries):
    """({role: entity_id}, {role: entity_id of a disabled entity}) for one device's
    registry entries. A disabled entity has no state; the page says which to enable."""
    roles, disabled = {}, {}
    for e in entries or []:
        role = role_of(e)
        if not role:
            continue
        target = disabled if e.get('disabled_by') else roles
        target.setdefault(role, e['entity_id'])
    for role in roles:
        disabled.pop(role, None)
    return roles, disabled


def is_tracker(roles, disabled=None):
    """Whether a device has an LD2450: it reports the first target's position."""
    have = set(roles) | set(disabled or ())
    return 't1_x' in have and 't1_y' in have


def mac_of(device):
    """A device registry entry's MAC address as 12 lowercase hex digits, or None."""
    for pair in device.get('connections') or []:
        if len(pair) >= 2 and pair[0] == 'mac':
            digits = re.sub(r'[^0-9a-f]', '', str(pair[1]).lower())
            if len(digits) == 12:
                return digits
    return None


def device_entry(device):
    """What the addon keeps about a device: its key (the MAC address, which survives
    renaming it and adding it to Home Assistant again) and what the sensor list shows."""
    mac = mac_of(device)
    key = mac or str(device.get('id') or '')
    if not key:
        return None
    return {
        'key': key,
        'topic': f"esphome/{key}",
        'friendly_name': device.get('name_by_user') or device.get('name') or key,
        'ieee': f"esphome:{key}",       # the id layouts, names and history are kept under
        'description': None,
        'ha_device_id': device.get('id'),
        'manufacturer': device.get('manufacturer'),
        'model': device.get('model'),
        'sw_version': device.get('sw_version'),
    }


# ---------------------------------------------------------------------------
# States and units
# ---------------------------------------------------------------------------

def known(state):
    return state not in (None, '', 'unknown', 'unavailable')


def number(state):
    """A finite float from an entity state, or None."""
    if not known(state) or isinstance(state, bool):
        return None
    try:
        v = float(state)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


_MM = {'mm': 1.0, 'cm': 10.0, 'm': 1000.0, 'km': 1e6, 'in': 25.4, 'ft': 304.8, 'yd': 914.4, 'mi': 1609344.0}
_MM_PER_S = {'mm/s': 1.0, 'cm/s': 10.0, 'm/s': 1000.0, 'in/s': 25.4, 'ft/s': 304.8,
             'km/h': 1e6 / 3600, 'mph': 1609344.0 / 3600, 'kn': 1852000.0 / 3600}


def length_cm(state, unit, default='mm'):
    """An entity's length in centimetres, or None."""
    v = number(state)
    scale = _MM.get(unit or default)
    return None if v is None or scale is None else v * scale / 10


def length_from_cm(cm, unit, default='mm'):
    """Centimetres in an entity's own unit, to write to it."""
    return cm * 10 / _MM.get(unit or default, 1.0)


def speed_cm_s(state, unit):
    v = number(state)
    scale = _MM_PER_S.get(unit or 'mm/s')
    return None if v is None or scale is None else v * scale / 10


# ---------------------------------------------------------------------------
# Targets and zones
# `read(role)` returns (state, unit_of_measurement) of a device's entity, or
# (None, None) when it has none.
# ---------------------------------------------------------------------------

def build_targets(read):
    """The people the LD2450 is tracking, as the page draws them: x and y in whole
    centimetres, `dop` the speed in cm/s (positive is moving away). No height.

    A target the radar isn't tracking is unknown, or 0, 0 on older firmware."""
    targets = []
    for n in range(1, TARGET_COUNT + 1):
        x = length_cm(*read(f't{n}_x'))
        y = length_cm(*read(f't{n}_y'))
        if x is None or y is None or (x == 0 and y == 0):
            continue
        speed = speed_cm_s(*read(f't{n}_speed'))
        targets.append({'id': n, 'x': int(round(x)), 'y': int(round(y)), 'z': 0,
                        'dop': int(round(speed)) if speed is not None else 0})
    return targets


def build_zone(read, n):
    """Zone n as {x_min, x_max, y_min, y_max} in whole centimetres, or None while any
    of its four numbers has no state. An unused zone is all zeros."""
    x1, y1, x2, y2 = (length_cm(*read(f'z{n}_{c}')) for c in ZONE_COORDS)
    if None in (x1, y1, x2, y2):
        return None
    return {'x_min': int(round(min(x1, x2))), 'x_max': int(round(max(x1, x2))),
            'y_min': int(round(min(y1, y2))), 'y_max': int(round(max(y1, y2)))}


def build_zones(read):
    """{'area1': zone, ...} for the zones that have a state (the page keeps the rest as they are)."""
    zones = {}
    for n in range(1, ZONE_COUNT + 1):
        zone = build_zone(read, n)
        if zone is not None:
            zones[f'area{n}'] = zone
    return zones


_ZONE_FIELDS = {'width_min': 'x1', 'width_max': 'x2', 'depth_min': 'y1', 'depth_max': 'y2'}
_IGNORED_ZONE_FIELDS = {'height_min', 'height_max'}   # the LD2450 has no height


def parse_zone_write(value):
    """Check a zone write from the page: {"area2": {"width_min": -100, "width_max": 100,
    "depth_min": 50, "depth_max": 250}}, all zeros to clear a zone.

    Returns ({zone number: {'x1', 'y1', 'x2', 'y2'} in cm}, None) or (None, error).
    The radar counts someone as inside when x1 < x < x2 and y1 < y < y2, so each pair
    is put in that order.
    """
    if not isinstance(value, dict) or not value:
        return None, "A zone write needs at least one zone"
    zones = {}
    for area, fields in value.items():
        m = re.fullmatch(r'area([1-9])', area) if isinstance(area, str) else None
        if not m or int(m.group(1)) > ZONE_COUNT:
            return None, f"The LD2450 has zones 1 to {ZONE_COUNT}, not {area}"
        if not isinstance(fields, dict):
            return None, f"Zone {m.group(1)} must be an object"
        unknown = set(fields) - set(_ZONE_FIELDS) - _IGNORED_ZONE_FIELDS
        if unknown:
            return None, f"Unknown zone fields: {sorted(unknown)}"
        coords = {}
        for field, coord in _ZONE_FIELDS.items():
            v = _as_int(fields.get(field))
            if v is None:
                return None, f"Zone {m.group(1)}: {field} must be a whole number of centimetres"
            limit = ZONE_X_LIMIT_CM if coord[0] == 'x' else ZONE_Y_LIMIT_CM
            low = -limit if coord[0] == 'x' else 0
            if v < low or v > limit:
                return None, f"Zone {m.group(1)}: {field}={v} is outside {low} to {limit} cm"
            coords[coord] = v
        if coords['x1'] > coords['x2']:
            coords['x1'], coords['x2'] = coords['x2'], coords['x1']
        if coords['y1'] > coords['y2']:
            coords['y1'], coords['y2'] = coords['y2'], coords['y1']
        zones[int(m.group(1))] = coords
    return zones, None


def native_zone_value(cm, attrs):
    """A zone coordinate in the number entity's own unit, kept inside its min and max."""
    attrs = attrs or {}
    v = length_from_cm(cm, attrs.get('unit_of_measurement'))
    lo, hi = number(attrs.get('min')), number(attrs.get('max'))
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    # Whole millimetres stay whole; other units keep what they need
    return int(round(v)) if abs(v - round(v)) < 1e-6 else round(v, 3)


def same_length(state, attrs, cm, tolerance_cm=0.1):
    """Whether a number entity already holds `cm` (within a millimetre)."""
    have = length_cm(state, (attrs or {}).get('unit_of_measurement'))
    return have is not None and abs(have - cm) <= tolerance_cm


# ---------------------------------------------------------------------------
# What the page shows
# ---------------------------------------------------------------------------

def on_off(state):
    """True/False from a binary sensor or switch state, None if unknown."""
    if not known(state):
        return None
    return {'on': True, 'off': False}.get(str(state).lower())


def build_payload(read, roles=None):
    """The device_config payload for a device: everything (roles=None) or what the
    changed `roles` affect. Values an entity has no state for are left out.

      occupancy                  either radar's presence sensor is on
      mmwave_area<n>_occupancy   someone is in zone n (its target count isn't 0)
      illuminance                lux
      ld2450_zones               {'area1': {x_min, x_max, y_min, y_max}, ...} in cm
      <setting key>              each setting of the catalogue
      esphome                    live readings: presence per radar, LD2412 distances
                                 (cm) and energies, gate energies, availability
    """
    want = (lambda role: True) if roles is None else (lambda role: role in roles)
    any_of = (lambda group: True) if roles is None else (lambda group: any(r in roles for r in group))
    payload, live = {}, {}

    if any_of(PRESENCE_ROLES):
        seen = [on_off(read(r)[0]) for r in PRESENCE_ROLES]
        if any(v is not None for v in seen):
            payload['occupancy'] = any(v is True for v in seen)
    for role, n in ZONE_COUNT_ROLES.items():
        if want(role):
            count = number(read(role)[0])
            if count is not None:
                payload[f'mmwave_area{n}_occupancy'] = count > 0
    if want('illuminance'):
        lux = number(read('illuminance')[0])
        if lux is not None:
            payload['illuminance'] = int(round(lux))
    if any_of(ZONE_ROLES):
        zones = build_zones(read)
        if zones:
            payload[ZONE_CATEGORY] = zones
    for key in SETTINGS:
        if want(key):
            value = setting_from_state(key, read(key)[0])
            if value is not None:
                payload[key] = value

    for role in LIVE_FLAGS:
        if want(role):
            value = on_off(read(role)[0])
            if value is not None:
                live[role] = value
    for role in LIVE_DISTANCES:
        if want(role):
            state, unit = read(role)
            if state is not None:   # None = no such entity; unknown = no target, sent as null
                cm = length_cm(state, unit, default='cm')
                live[role] = None if cm is None else int(round(cm))
    for role in LIVE_PERCENTS:
        if want(role):
            state, _ = read(role)
            if state is not None:
                v = number(state)
                live[role] = None if v is None else int(round(v))
    if any_of(GATE_ROLES):
        for kind in ('move', 'still'):
            row = [number(read(f'g{g}_{kind}')[0]) for g in range(GATE_COUNT)]
            if any(v is not None for v in row):
                live[f'gate_{kind}'] = [None if v is None else int(round(v)) for v in row]
    if any_of(TARGET_ROLES):
        # Every entity of a device goes unavailable together when it drops off the network
        state = read('t1_x')[0]
        if state is not None:
            live['available'] = state != 'unavailable'
    if live:
        payload['esphome'] = live
    return payload


def history_state(payload):
    """The timeline's fields (occupancy and each zone) out of a device_config payload."""
    state = {}
    if 'occupancy' in payload:
        state['occupancy'] = payload['occupancy']
    for n in range(1, ZONE_COUNT + 1):
        key = f'mmwave_area{n}_occupancy'
        if key in payload:
            state[f'area{n}'] = payload[key]
    return state


def unsupported_settings(roles):
    """Catalogue settings this device has no enabled entity for."""
    return [key for key in SETTINGS if key not in roles]


def build_info(entry, roles, disabled, read, read_attrs):
    """The device_info the page shows at the top of the Config tab (same shape as
    device_info.empty_info, plus what only an ESPHome sensor has)."""
    def text(role):
        state = read(role)[0]
        return str(state) if known(state) else None

    firmware = text('firmware')
    esphome_version = text('esphome_version')
    radars = [f"{name} {v}" for name, v in (('LD2450', text('ld2450_firmware')), ('LD2412', text('ld2412_firmware'))) if v]
    update = None
    if 'update' in roles:
        state = read('update')[0]
        attrs = read_attrs('update') or {}
        if known(state) and attrs.get('latest_version') is not None:
            update = {'entity_id': roles['update'],
                      'installed': str(attrs.get('installed_version')) if attrs.get('installed_version') is not None else None,
                      'latest': str(attrs['latest_version']),
                      'available': state == 'on', 'in_progress': bool(attrs.get('in_progress'))}
    entities = {f'area{n}': roles[role] for role, n in ZONE_COUNT_ROLES.items() if role in roles}
    for role in PRESENCE_ROLES:
        if role in roles:
            entities[role] = roles[role]
    if 'ld2450_presence' in roles:
        entities['occupancy'] = roles['ld2450_presence']
    return {
        'stack': 'esphome',
        'ieee': entry.get('ieee'),
        'firmware': {
            'ota': f"ESPHome {esphome_version}" if firmware and esphome_version else None,
            'build': firmware or entry.get('sw_version') or (f"ESPHome {esphome_version}" if esphome_version else None),
            'date': None,
        },
        'mmwave_version': {'text': ' · '.join(radars), 'raw': ''} if radars else None,
        'link': {'lqi': None, 'rssi': None},
        'last_seen': None,
        'quirk_class': None,
        'quirk': None,
        'unsupported_settings': unsupported_settings(roles),
        'update': update,
        'ha_device_id': entry.get('ha_device_id'),
        'entities': entities,
        'area_trigger': 'entity',
        'model': ' '.join(p for p in (entry.get('manufacturer'), entry.get('model')) if p) or None,
        'ip_address': text('ip_address'),
        # Entities that exist but are disabled in Home Assistant, by what they'd give the page
        'disabled_entities': dict(disabled),
        'missing_tracking': [r for r in ('t1_x', 't1_y') if r not in roles],
    }
