"""A stand-in for Home Assistant's WebSocket API with an Apollo R PRO-1 on it.

Enough of the API for the ESPHome driver: authentication, the device and entity
registries, subscribe_entities (the compressed state stream) and call_service
for numbers, selects, switches and buttons.

It follows the real things as their sources have them, not a physical sensor:
  * the entities are the ones in Apollo's firmware (R_PRO-1 26.3.2.1), with the
    same ones disabled by default;
  * registry entries, unique_ids, units and state strings are what Home
    Assistant's ESPHome integration produces (values are floats, so 1500 mm is
    "1500.0"; on a US-customary install sensors are shown in inches and
    numbers are not);
  * a zone number behaves as ESPHome's LD2450 component does: it publishes the
    value it was sent, sends the radar its whole zone table, then reads the
    table back and publishes every corner again.

The tests start it on a free port. Run it by hand to try the page without a
sensor (people then walk around in front of it):

    python tests/fake_ha.py            # listens on 127.0.0.1:8123
    ZIGBEE_STACK=esphome HA_URL=http://127.0.0.1:8123 HA_TOKEN=anything \\
        DATA_DIR=/tmp/mmwave python mmwave_vis/app.py
"""
import asyncio
import copy
import json
import math
import re
import threading
import time

from websockets.asyncio.server import serve

MAC = "AA:BB:CC:A1:B2:C3"
DEVICE_ID = "abc123device"
PREFIX = "apollo_r_pro_1_a1b2c3"
GATES = 14
ZONE_COORDS = ("X1", "Y1", "X2", "Y2")
ZONE_LIMITS_MM = {"X1": (-4860, 4860), "Y1": (0, 7560), "X2": (-4860, 4860), "Y2": (0, 7560)}
ZONE_TYPES = ["Disabled", "Detection", "Filter"]
MM_PER = {"mm": 1.0, "in": 25.4}
READBACK_S = 0.15     # roughly what the firmware needs to send the zone table and read it back


def object_id(name):
    """ESPHome's object id for an entity name: lower case, spaces to underscores."""
    return re.sub(r'[^a-z0-9_-]', '_', name.lower())


def entity_id(domain, name):
    """Home Assistant's entity_id: the device's name, then the entity's, slugified."""
    return f"{domain}.{PREFIX}_{re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')}"


def unique_id(platform, name, form="name"):
    """The unique_id Home Assistant's ESPHome integration gives an entity: the entity's
    name after the sub-device number, or (form="object_id") what older versions built
    and installs that haven't been migrated still have."""
    if form == "object_id":
        return f"{MAC}-{platform}-{object_id(name)}"
    return f"{MAC}/0/{platform}/{name}"


def num(value):
    """A number as Home Assistant states it. ESPHome's values are floats: 1500 is "1500.0"."""
    return str(float(value))


def converted(native, scale):
    """A number entity's value in a unit its user chose, as Home Assistant shows it:
    rounded to as many decimals as the native value is written with."""
    text = str(float(native))
    return round(native / scale, len(text) - text.index(".") - 1)


def r_pro_1(length_unit="mm", zone_unit="mm"):
    """[(domain, ESPHome platform, name, state, attributes, disabled)] for an R PRO-1.

    `length_unit`: "in" on a US-customary install, where Home Assistant shows distance
    sensors in inches and speeds in inches per second. Numbers aren't converted, so the
    zone corners stay in millimetres unless someone picks another unit for them in the
    entity's settings (`zone_unit`).
    """
    rows = []

    def add(domain, name, state, attrs=None, disabled=False, platform=None):
        rows.append((domain, platform or domain, name, state, dict(attrs or {}), disabled))

    us = length_unit == "in"
    length = {"unit_of_measurement": length_unit, "device_class": "distance", "state_class": "measurement"}
    speed = {"unit_of_measurement": "in/s" if us else "mm/s", "device_class": "speed", "state_class": "measurement"}
    percent = {"unit_of_measurement": "%", "state_class": "measurement"}
    zone_scale = MM_PER[zone_unit]
    for n in (1, 2, 3):
        add("sensor", f"LD2450 Target-{n} X", "unknown", length)
        add("sensor", f"LD2450 Target-{n} Y", "unknown", length)
        add("sensor", f"LD2450 Target-{n} Speed", "unknown", speed)
        add("sensor", f"LD2450 Target-{n} Angle", "unknown", {"unit_of_measurement": "°", "state_class": "measurement"})
        add("sensor", f"LD2450 Target-{n} Distance", "unknown", length)
        add("sensor", f"LD2450 Target-{n} Resolution", "unknown", length, disabled=True)
        add("sensor", f"LD2450 Target-{n} Direction", "NA", platform="text_sensor")
        for c in ZONE_COORDS:
            lo, hi = ZONE_LIMITS_MM[c]
            if zone_unit != "mm":      # Home Assistant rounds a converted range outwards
                lo, hi = math.floor(lo / zone_scale * 10) / 10, math.ceil(hi / zone_scale * 10) / 10
            add("number", f"LD2450 Zone-{n} {c}", num(0),
                {"unit_of_measurement": zone_unit, "device_class": "distance",
                 "min": float(lo), "max": float(hi), "step": 1.0, "mode": "auto"})
        for kind in ("All", "Still", "Moving"):
            add("sensor", f"LD2450 Zone-{n} {kind} Target Count", num(0), {"state_class": "measurement"})
    add("sensor", "LD2450 Presence Target Count", num(0), {"state_class": "measurement"})
    add("sensor", "LD2450 Still Target Count", num(0), {"state_class": "measurement"})
    add("sensor", "LD2450 Moving Target Count", num(0), {"state_class": "measurement"})
    for radar in ("LD2450", "LD2412"):
        add("binary_sensor", f"{radar} Presence", "off", {"device_class": "occupancy"})
        add("binary_sensor", f"{radar} Moving Target", "off", {"device_class": "motion"})
        add("binary_sensor", f"{radar} Still Target", "off", {"device_class": "occupancy"})
        add("sensor", f"{radar} Firmware", "V1.02.3", disabled=True, platform="text_sensor")
        add("sensor", f"{radar} BT MAC", "AA:BB:CC:00:00:01", disabled=True, platform="text_sensor")
        add("switch", f"{radar} Bluetooth", "on")
        add("button", f"{radar} Restart", "unknown")
        add("button", f"{radar} Factory Reset", "unknown")
        add("number", f"{radar} Timeout", num(5),
            {"unit_of_measurement": "s", "min": 0.0, "max": 3600.0 if radar == "LD2450" else 900.0, "step": 1.0, "mode": "auto"})
    add("select", "LD2450 Zone Type", "Disabled", {"options": list(ZONE_TYPES)})
    add("select", "LD2450 Baud rate", "256000", disabled=True)
    add("switch", "LD2450 Multi Target Tracking", "on", disabled=True)
    add("switch", "LD2412 Engineering Mode", "off")
    add("number", "LD2412 min distance gate", num(1), {"min": 1.0, "max": 12.0, "step": 1.0, "mode": "auto"})
    add("number", "LD2412 max distance gate", num(12), {"min": 2.0, "max": 13.0, "step": 1.0, "mode": "auto"})
    add("number", "LD2412 Light Threshold", num(128), {"min": 0.0, "max": 255.0, "step": 1.0, "mode": "auto"}, disabled=True)
    add("select", "LD2412 Distance resolution", "0.75m", {"options": ["0.2m", "0.5m", "0.75m"]}, disabled=True)
    add("select", "LD2412 Light Function", "off", {"options": ["off", "below", "above"]}, disabled=True)
    add("select", "LD2412 Hardware output pin level", "low", {"options": ["low", "high"]}, disabled=True)
    add("select", "LD2412 baud rate", "115200", disabled=True)
    add("button", "LD2412 Query params", "unknown")
    add("button", "LD2412 Start Dynamic Background Correction", "unknown")
    add("binary_sensor", "LD2412 Dynamic Background Correction Status", "off")
    add("binary_sensor", "LD2412 Out Pin", "off", disabled=True)
    distance = {"unit_of_measurement": "in" if us else "cm", "device_class": "distance", "state_class": "measurement"}
    for name in ("Moving Distance", "Still Distance", "Detection Distance"):
        add("sensor", f"LD2412 {name}", num(0), distance, disabled=True)
    add("sensor", "LD2412 Move Energy", num(0), percent, disabled=True)
    add("sensor", "LD2412 Still Energy", num(0), percent, disabled=True)
    add("sensor", "LD2412 Light", "unknown", {"state_class": "measurement"}, disabled=True)
    for g in range(GATES):
        for kind in ("move", "still"):
            add("number", f"LD2412 g{g:02d} {kind} threshold", num(40 if kind == "move" else 25),
                {"unit_of_measurement": "%", "min": 0.0, "max": 100.0, "step": 1.0, "mode": "auto"})
            add("sensor", f"LD2412 g{g:02d} {kind} energy", "unknown", percent)
    add("sensor", "LTR390 Light", "182.4", {"unit_of_measurement": "lx", "device_class": "illuminance", "state_class": "measurement"})
    add("sensor", "LTR390 UV Index", num(0), {"state_class": "measurement"})
    add("sensor", "CO2", num(612), {"unit_of_measurement": "ppm", "device_class": "carbon_dioxide", "state_class": "measurement"})
    add("sensor", "SCD40 Temperature", "21.4", {"unit_of_measurement": "°C", "device_class": "temperature"}, disabled=True)
    add("sensor", "SCD40 Humidity", "44.0", {"unit_of_measurement": "%", "device_class": "humidity"}, disabled=True)
    add("number", "SCD40 Temperature Offset", "18.86", {"unit_of_measurement": "°C", "min": -70.0, "max": 70.0, "step": 0.1, "mode": "box"}, disabled=True)
    add("number", "SCD40 Humidity Offset", num(0), {"unit_of_measurement": "%", "min": -70.0, "max": 70.0, "step": 0.1, "mode": "box"}, disabled=True)
    add("switch", "SCD40 Automatic Self Calibration", "on", disabled=True)
    add("button", "Calibrate SCD40 To 420ppm", "unknown")
    add("sensor", "ESP Temperature", "48.2", {"unit_of_measurement": "°C", "device_class": "temperature"}, disabled=True)
    add("sensor", "Uptime", num(4211), {"unit_of_measurement": "s", "device_class": "duration"})
    add("sensor", "Apollo Firmware Version", "26.3.2.1", platform="text_sensor")
    add("sensor", "ESPHome Version", "2026.3.1", platform="text_sensor")
    add("sensor", "IP Address", "192.168.1.57", platform="text_sensor")
    add("binary_sensor", "Online", "on", {"device_class": "connectivity"})
    add("switch", "Reduce DB Reporting", "off")
    add("select", "Firmware Type", "Ethernet", {"options": ["WiFi", "Ethernet"]})
    add("light", "RGB Light", "off")
    add("update", "Firmware Update", "off", {"installed_version": "26.3.2.1", "latest_version": "26.3.2.1", "in_progress": False})
    add("button", "Firmware Update", "unknown")
    add("button", "ESP Reboot", "unknown")
    return rows


class FakeHA:
    """`with FakeHA() as ha:` then `ha.url`; `ha.set_state()`, `ha.calls`, `ha.states`.

    `unique_ids`: "name" as Home Assistant builds them now, "object_id" as it used to.
    `readback_s`: how long the sensor takes to send the radar its zone table and read it back.
    """

    def __init__(self, port=0, token="test-token", length_unit="mm", zone_unit="mm", unique_ids="name",
                 simulate=False, readback_s=0.01):
        self.port = port
        self.token = token
        self.simulate = simulate
        self.length_unit = length_unit
        self.zone_unit = zone_unit
        self.readback_s = readback_s
        self.calls = []                 # (domain, service, entity_id, service_data)
        self.states = {}                # entity_id → {'s': state, 'a': attributes}
        self.entities = []              # entity registry entries
        self.device = {
            "id": DEVICE_ID, "name": "Apollo R_PRO-1 a1b2c3", "name_by_user": None,
            "manufacturer": "ApolloAutomation", "model": "R-PRO-1-ETH", "sw_version": "26.3.2.1 (ESPHome 2026.3.1)",
            "connections": [["mac", MAC.lower()]], "identifiers": [], "disabled_by": None,
        }
        self.other_devices = [
            {"id": "plug1", "name": "Smart plug", "manufacturer": "Athom", "model": "Plug V2",
             "connections": [["mac", "11:22:33:44:55:66"]], "identifiers": [], "disabled_by": None},
        ]
        for domain, platform, name, state, attrs, disabled in r_pro_1(length_unit, zone_unit):
            eid = entity_id(domain, name)
            self.entities.append({
                "entity_id": eid, "device_id": DEVICE_ID, "platform": "esphome",
                "unique_id": unique_id(platform, name, unique_ids),
                "original_name": name, "name": None, "translation_key": None,
                "disabled_by": "integration" if disabled else None, "hidden_by": None,
            })
            if not disabled:
                self.states[eid] = {"s": state, "a": {**attrs, "friendly_name": f"Apollo R_PRO-1 a1b2c3 {name}"}}
        # Something else from the same integration, which the addon should leave alone
        self.entities.append({"entity_id": "switch.smart_plug_relay", "device_id": "plug1", "platform": "esphome",
                              "unique_id": "11:22:33:44:55:66/0/switch/Relay", "original_name": "Relay", "disabled_by": None})
        self.states["switch.smart_plug_relay"] = {"s": "off", "a": {}}

        # The LD2450's zones: what ESPHome holds (`_zones`) and what the radar itself holds
        self._zones = {n: [0, 0, 0, 0] for n in (1, 2, 3)}       # mm: x1, y1, x2, y2
        self._zone_type = "Disabled"
        self._radar = (copy.deepcopy(self._zones), self._zone_type)
        self._zone_ids = {n: [entity_id("number", f"LD2450 Zone-{n} {c}") for c in ZONE_COORDS] for n in (1, 2, 3)}
        self._zone_numbers = {eid: (n, i) for n, ids in self._zone_ids.items() for i, eid in enumerate(ids)}
        self._zone_native = dict.fromkeys(self._zone_numbers, 0.0)   # each zone number's value in mm
        self._zone_type_select = entity_id("select", "LD2450 Zone Type")
        self.lose_zone_writes = 0       # tests: the radar doesn't take the next so many zone tables

        self._subs = []                 # (websocket, subscription id, entity ids)
        self._event_subs = []           # (websocket, subscription id, event type)
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stop = None

    # --- Running -------------------------------------------------------------------

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/api/websocket"

    def __enter__(self):
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self._thread.start()
        assert self._ready.wait(5), "the fake Home Assistant didn't start"
        return self

    def __exit__(self, *exc):
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(3)
        return False

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with serve(self._serve, "127.0.0.1", self.port, max_size=None) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            walker = asyncio.create_task(self._walk()) if self.simulate else None
            await self._stop.wait()
            if walker:
                walker.cancel()

    # --- The API ------------------------------------------------------------------------

    def eid(self, domain, name):
        return entity_id(domain, name)

    async def _serve(self, ws):
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.11.0"}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth" or msg.get("access_token") != self.token:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.11.0"}))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                reply = {"id": msg.get("id"), "type": "result", "success": True, "result": None}
                kind = msg.get("type")
                if kind == "config/device_registry/list":
                    reply["result"] = [self.device, *self.other_devices]
                elif kind == "config/entity_registry/list":
                    reply["result"] = self.entities
                elif kind == "subscribe_entities":
                    ids = list(msg.get("entity_ids") or [])
                    self._subs.append((ws, msg["id"], set(ids)))
                    await ws.send(json.dumps(reply))
                    added = {e: {"s": self.states[e]["s"], "a": self.states[e]["a"], "lc": time.time()}
                             for e in ids if e in self.states}
                    await ws.send(json.dumps({"id": msg["id"], "type": "event", "event": {"a": added}}))
                    continue
                elif kind == "subscribe_events":
                    self._event_subs.append((ws, msg["id"], msg.get("event_type")))
                elif kind == "call_service":
                    error = self._call(msg)
                    if error:
                        reply.update(success=False, error={"code": "service_validation_error", "message": error})
                    else:
                        reply["result"] = {"context": {"id": "ctx"}}
                else:
                    reply.update(success=False, error={"code": "unknown_command", "message": f"Unknown command {kind}"})
                await ws.send(json.dumps(reply))
        finally:
            self._subs = [s for s in self._subs if s[0] is not ws]
            self._event_subs = [s for s in self._event_subs if s[0] is not ws]

    def _call(self, msg):
        domain, service = msg.get("domain"), msg.get("service")
        data = msg.get("service_data") or {}
        eid = (msg.get("target") or {}).get("entity_id") or data.get("entity_id")
        self.calls.append((domain, service, eid, {k: v for k, v in data.items() if k != "entity_id"}))
        st = self.states.get(eid)
        if st is None:
            return f"Referenced entities {eid} are missing or not currently available"
        if (domain, service) == ("number", "set_value"):
            value = float(data["value"])
            if value < st["a"].get("min", -math.inf) or value > st["a"].get("max", math.inf):
                return f"Value {value} for {eid} is outside valid range {st['a'].get('min')} - {st['a'].get('max')}"
            if eid in self._zone_numbers:
                self._zone_number_written(eid, value)
            else:
                self._set(eid, num(value))
        elif (domain, service) == ("select", "select_option"):
            if data["option"] not in st["a"].get("options", []):
                return f"Option {data['option']} is not valid for entity {eid}"
            self._set(eid, data["option"])
            if eid == self._zone_type_select:
                self._zone_type = data["option"]
                self._send_zone_table()
        elif domain == "switch" and service in ("turn_on", "turn_off"):
            self._set(eid, "on" if service == "turn_on" else "off")
        elif (domain, service) == ("button", "press"):
            self._set(eid, time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
        else:
            return f"Service {domain}.{service} not found"
        return None

    def _set(self, eid, state):
        """On the server's loop: change a state and tell the subscribers."""
        st = self.states.setdefault(eid, {"s": None, "a": {}})
        if st["s"] == state:
            return
        st["s"] = state
        event = json.dumps({"c": {eid: {"+": {"s": state, "lc": time.time()}}}})
        for ws, sub_id, ids in list(self._subs):
            if eid in ids:
                asyncio.ensure_future(ws.send(f'{{"id": {sub_id}, "type": "event", "event": {event}}}'))

    # --- The LD2450's zones, as ESPHome's component handles them ---------------------------------

    @property
    def radar_zones(self):
        """{zone: [x1, y1, x2, y2]} in mm, as the radar itself holds them."""
        return copy.deepcopy(self._radar[0])

    def _show_zone_number(self, eid, mm):
        self._zone_native[eid] = float(mm)
        self._set(eid, num(mm if self.zone_unit == "mm" else converted(mm, MM_PER[self.zone_unit])))

    def _zone_number_written(self, eid, value):
        n, i = self._zone_numbers[eid]
        mm = value * MM_PER[self.zone_unit]      # Home Assistant converts to the number's own unit
        lo, hi = ZONE_LIMITS_MM[ZONE_COORDS[i]]
        if mm < lo or mm > hi:
            return                               # ESPHome logs a warning and leaves it at that
        self._show_zone_number(eid, mm)          # published before anything is sent to the radar
        self._zones[n] = [int(self._zone_native[e]) for e in self._zone_ids[n]]   # that zone's numbers as they stand
        self._send_zone_table()

    def _send_zone_table(self):
        """The whole table goes to the radar, which is then asked what it holds."""
        if self.lose_zone_writes > 0:
            self.lose_zone_writes -= 1
        else:
            self._radar = (copy.deepcopy(self._zones), self._zone_type)
        self._loop.call_later(self.readback_s / 2, self._query_zones)

    def _query_zones(self):
        # The radar's answer is what it holds now; the firmware gets to it a moment later,
        # by when another number may have been written
        self._loop.call_later(self.readback_s / 2, self._zones_read_back, copy.deepcopy(self._radar))

    def _zones_read_back(self, answer):
        zones, zone_type = answer
        self._zones, self._zone_type = copy.deepcopy(zones), zone_type
        self._set(self._zone_type_select, zone_type)
        for eid, (n, i) in self._zone_numbers.items():
            self._show_zone_number(eid, zones[n][i])

    # --- From a test's thread ---------------------------------------------------------------------

    def _run(self, fn):
        done = threading.Event()

        def run():
            try:
                fn()
            finally:
                done.set()
        self._loop.call_soon_threadsafe(run)
        done.wait(2)

    def set_state(self, eid, state):
        self._run(lambda: self._set(eid, state))

    def set_states(self, states):
        for eid, state in states.items():
            self.set_state(eid, state)

    def set_target(self, n, x_mm, y_mm, speed_mm_s=0):
        """One LD2450 target as the firmware would report it (None, None: not tracked)."""
        scale = MM_PER[self.length_unit]
        fmt = lambda v: "unknown" if v is None else str(v / scale)
        self.set_states({
            self.eid("sensor", f"LD2450 Target-{n} X"): fmt(x_mm),
            self.eid("sensor", f"LD2450 Target-{n} Y"): fmt(y_mm),
            self.eid("sensor", f"LD2450 Target-{n} Speed"): fmt(None if x_mm is None else speed_mm_s),
        })

    def enable(self, domain, name, state, attrs=None, announce=True):
        """Enable an entity that ships disabled (the user did it on the device's page).
        `announce`: fire the registry event Home Assistant sends when that happens."""
        eid = entity_id(domain, name)
        for e in self.entities:
            if e["entity_id"] == eid:
                e["disabled_by"] = None
        self.states[eid] = {"s": state, "a": dict(attrs or {})}
        if announce and self._loop:
            self.fire_event("entity_registry_updated", {"action": "update", "entity_id": eid, "changes": {"disabled_by": "integration"}})
        return eid

    def fire_event(self, event_type, data):
        """An event on Home Assistant's bus."""
        def run():
            for ws, sub_id, wanted in list(self._event_subs):
                if wanted == event_type:
                    asyncio.ensure_future(ws.send(json.dumps({
                        "id": sub_id, "type": "event",
                        "event": {"event_type": event_type, "data": data, "origin": "LOCAL"}})))
        self._loop.call_soon_threadsafe(run)

    # --- People walking about, for trying the page ----------------------------------------------

    async def _walk(self):
        t0 = time.time()
        scale = MM_PER[self.length_unit]
        while True:
            await asyncio.sleep(1)
            t = time.time() - t0
            people = [
                (1400 * math.sin(t / 7), 2400 + 1300 * math.cos(t / 9), round(300 * math.cos(t / 7))),
                (-1500 + 500 * math.sin(t / 5), 1500 + 300 * math.sin(t / 3), 0),
            ]
            if int(t / 40) % 2:
                people = people[:1]            # the second person steps out now and then
            zones, zone_type = self._radar
            inside = lambda p, z: z[0] < p[0] < z[2] and z[1] < p[1] < z[3]
            # The radar itself leaves out whoever the zone type excludes
            if zone_type == "Detection":
                tracked = [p for p in people if any(inside(p, z) for z in zones.values())]
            elif zone_type == "Filter":
                tracked = [p for p in people if not any(inside(p, z) for z in zones.values())]
            else:
                tracked = people
            for n in (1, 2, 3):
                p = tracked[n - 1] if n <= len(tracked) else None
                for name, value in (("X", p and round(p[0])), ("Y", p and round(p[1])), ("Speed", p and p[2]),
                                    ("Distance", p and round(math.hypot(p[0], p[1])))):
                    self._set(self.eid("sensor", f"LD2450 Target-{n} {name}"), "unknown" if p is None else str(value / scale))
            for n, z in self._zones.items():
                count = sum(1 for p in tracked if inside(p, z))
                self._set(self.eid("sensor", f"LD2450 Zone-{n} All Target Count"), num(count))
            self._set(self.eid("sensor", "LD2450 Presence Target Count"), num(len(tracked)))
            self._set(self.eid("binary_sensor", "LD2450 Presence"), "on" if tracked else "off")
            self._set(self.eid("binary_sensor", "LD2412 Presence"), "on")
            self._set(self.eid("binary_sensor", "LD2412 Moving Target"), "on")
            self._set(self.eid("binary_sensor", "LD2412 Still Target"), "on" if len(people) > 1 else "off")
            near = min(math.hypot(p[0], p[1]) for p in people) / 10
            cm = 2.54 if self.length_unit == "in" else 1
            for name, value in (("Moving Distance", round(math.hypot(*people[0][:2]) / 10)), ("Still Distance", round(near))):
                eid = self.eid("sensor", f"LD2412 {name}")
                if eid in self.states:
                    self._set(eid, str(value / cm))
            engineering = self.states[self.eid("switch", "LD2412 Engineering Mode")]["s"] == "on"
            for g in range(GATES):
                centre = math.hypot(*people[0][:2]) / 750
                move = max(0, round(90 - 38 * abs(g + 0.5 - centre) + 6 * math.sin(t + g)))
                still = max(0, round(60 - 25 * abs(g + 0.5 - near / 75) + 4 * math.cos(t + g)))
                self._set(self.eid("sensor", f"LD2412 g{g:02d} move energy"), num(move) if engineering else "unknown")
                self._set(self.eid("sensor", f"LD2412 g{g:02d} still energy"), num(still) if engineering else "unknown")


if __name__ == "__main__":
    import sys
    fake = FakeHA(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8123, token="anything", simulate=True,
                  readback_s=READBACK_S)
    # Shown as arcs on the map once enabled, as on a real sensor
    fake.enable("sensor", "LD2412 Moving Distance", num(0), {"unit_of_measurement": "cm"})
    fake.enable("sensor", "LD2412 Still Distance", num(0), {"unit_of_measurement": "cm"})
    with fake:
        print(f"Fake Home Assistant with an Apollo R PRO-1 on {fake.url} (token: anything)", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
