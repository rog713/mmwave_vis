"""A stand-in for Home Assistant's WebSocket API with an Apollo R PRO-1 on it.

Enough of the API for the ESPHome driver: authentication, the device and entity
registries, subscribe_entities (the compressed state stream) and call_service
for numbers, selects, switches and buttons. The sensor's entities carry the
names Apollo's firmware gives them, with the same ones disabled by default.

The tests start it on a free port. Run it by hand to try the page without a
sensor (people then walk around in front of it):

    python tests/fake_ha.py            # listens on 127.0.0.1:8123
    ZIGBEE_STACK=esphome HA_URL=http://127.0.0.1:8123 HA_TOKEN=anything \\
        DATA_DIR=/tmp/mmwave python mmwave_vis/app.py
"""
import asyncio
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


def object_id(name):
    """ESPHome's object id for an entity name: lower case, spaces to underscores."""
    return re.sub(r'[^a-z0-9_-]', '_', name.lower())


def entity_id(domain, name):
    """Home Assistant's entity_id: the device's name, then the entity's, slugified."""
    return f"{domain}.{PREFIX}_{re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')}"


def r_pro_1(length_unit="mm"):
    """[(domain, ESPHome platform, name, state, attributes, disabled)] for an R PRO-1.

    `length_unit`: what Home Assistant shows the LD2450's lengths in ("in" on a
    US-customary install, where it converts millimetres for display).
    """
    rows = []

    def add(domain, name, state, attrs=None, disabled=False, platform=None):
        rows.append((domain, platform or domain, name, state, dict(attrs or {}), disabled))

    length = {"unit_of_measurement": length_unit, "device_class": "distance"}
    scale = {"mm": 1, "in": 1 / 25.4}[length_unit]
    for n in (1, 2, 3):
        add("sensor", f"LD2450 Target-{n} X", "unknown", length)
        add("sensor", f"LD2450 Target-{n} Y", "unknown", length)
        add("sensor", f"LD2450 Target-{n} Speed", "unknown", {"unit_of_measurement": "mm/s", "device_class": "speed"})
        add("sensor", f"LD2450 Target-{n} Angle", "unknown", {"unit_of_measurement": "°"})
        add("sensor", f"LD2450 Target-{n} Distance", "unknown", length)
        add("sensor", f"LD2450 Target-{n} Resolution", "unknown", length, disabled=True)
        add("sensor", f"LD2450 Target-{n} Direction", "NA", platform="text_sensor")
        for c, lo, hi in (("X1", -4860, 4860), ("Y1", 0, 7560), ("X2", -4860, 4860), ("Y2", 0, 7560)):
            add("number", f"LD2450 Zone-{n} {c}", "0",
                {**length, "min": round(lo * scale, 3), "max": round(hi * scale, 3), "step": 1, "mode": "auto"})
        for kind in ("All", "Still", "Moving"):
            add("sensor", f"LD2450 Zone-{n} {kind} Target Count", "0")
    add("sensor", "LD2450 Presence Target Count", "0")
    add("sensor", "LD2450 Still Target Count", "0")
    add("sensor", "LD2450 Moving Target Count", "0")
    for radar in ("LD2450", "LD2412"):
        add("binary_sensor", f"{radar} Presence", "off", {"device_class": "occupancy"})
        add("binary_sensor", f"{radar} Moving Target", "off")
        add("binary_sensor", f"{radar} Still Target", "off")
        add("sensor", f"{radar} Firmware", "V1.02.3", disabled=True, platform="text_sensor")
        add("switch", f"{radar} Bluetooth", "on")
        add("button", f"{radar} Restart", "unknown")
        add("button", f"{radar} Factory Reset", "unknown")
        add("number", f"{radar} Timeout", "5", {"unit_of_measurement": "s", "min": 0, "max": 3600 if radar == "LD2450" else 900, "step": 1})
    add("select", "LD2450 Zone Type", "Disabled", {"options": ["Disabled", "Detection", "Filter"]})
    add("select", "LD2450 Baud rate", "256000", disabled=True)
    add("switch", "LD2450 Multi Target Tracking", "on", disabled=True)
    add("switch", "LD2412 Engineering Mode", "off")
    add("number", "LD2412 min distance gate", "1", {"min": 1, "max": 12, "step": 1})
    add("number", "LD2412 max distance gate", "12", {"min": 2, "max": 13, "step": 1})
    add("number", "LD2412 Light Threshold", "128", {"min": 0, "max": 255, "step": 1}, disabled=True)
    add("select", "LD2412 Distance resolution", "0.75m", {"options": ["0.2m", "0.5m", "0.75m"]}, disabled=True)
    add("select", "LD2412 Light Function", "off", {"options": ["off", "below", "above"]}, disabled=True)
    add("select", "LD2412 Hardware output pin level", "low", {"options": ["low", "high"]}, disabled=True)
    add("button", "LD2412 Query params", "unknown")
    add("button", "LD2412 Start Dynamic Background Correction", "unknown")
    add("binary_sensor", "LD2412 Dynamic Background Correction Status", "off")
    for name in ("Moving Distance", "Still Distance", "Detection Distance"):
        add("sensor", f"LD2412 {name}", "0", {"unit_of_measurement": "cm", "device_class": "distance"}, disabled=True)
    add("sensor", "LD2412 Move Energy", "0", {"unit_of_measurement": "%"}, disabled=True)
    add("sensor", "LD2412 Still Energy", "0", {"unit_of_measurement": "%"}, disabled=True)
    for g in range(GATES):
        for kind in ("move", "still"):
            add("number", f"LD2412 g{g:02d} {kind} threshold", "40" if kind == "move" else "25",
                {"unit_of_measurement": "%", "min": 0, "max": 100, "step": 1})
            add("sensor", f"LD2412 g{g:02d} {kind} energy", "unknown", {"unit_of_measurement": "%"})
    add("sensor", "LTR390 Light", "182.4", {"unit_of_measurement": "lx", "device_class": "illuminance"})
    add("sensor", "LTR390 UV Index", "0")
    add("sensor", "CO2", "612", {"unit_of_measurement": "ppm"})
    add("sensor", "Uptime", "4211")
    add("sensor", "Apollo Firmware Version", "26.3.2.1", platform="text_sensor")
    add("sensor", "ESPHome Version", "2026.3.1", platform="text_sensor")
    add("sensor", "IP Address", "192.168.1.57", platform="text_sensor")
    add("binary_sensor", "Online", "on")
    add("light", "RGB Light", "off")
    add("update", "Firmware Update", "off", {"installed_version": "26.3.2.1", "latest_version": "26.3.2.1", "in_progress": False})
    add("button", "ESP Reboot", "unknown")
    return rows


class FakeHA:
    """`with FakeHA() as ha:` then `ha.url`; `ha.set_state()`, `ha.calls`, `ha.states`."""

    def __init__(self, port=0, token="test-token", length_unit="mm", simulate=False):
        self.port = port
        self.token = token
        self.simulate = simulate
        self.length_unit = length_unit
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
        for domain, platform, name, state, attrs, disabled in r_pro_1(length_unit):
            eid = entity_id(domain, name)
            self.entities.append({
                "entity_id": eid, "device_id": DEVICE_ID, "platform": "esphome",
                "unique_id": f"{MAC}-{platform}-{object_id(name)}",
                "original_name": name, "name": None, "has_entity_name": True,
                "disabled_by": "integration" if disabled else None,
            })
            if not disabled:
                self.states[eid] = {"s": state, "a": {**attrs, "friendly_name": f"Apollo R_PRO-1 a1b2c3 {name}"}}
        # Something else from the same integration, which the addon should leave alone
        self.entities.append({"entity_id": "switch.smart_plug_relay", "device_id": "plug1", "platform": "esphome",
                              "unique_id": "11:22:33:44:55:66-switch-relay", "original_name": "Relay", "disabled_by": None})
        self.states["switch.smart_plug_relay"] = {"s": "off", "a": {}}
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
            self._set(eid, str(int(value)) if value.is_integer() else str(round(value, 4)))
        elif (domain, service) == ("select", "select_option"):
            if data["option"] not in st["a"].get("options", []):
                return f"Option {data['option']} is not valid for entity {eid}"
            self._set(eid, data["option"])
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

    def set_state(self, eid, state):
        """From a test's thread."""
        done = threading.Event()

        def run():
            self._set(eid, state)
            done.set()
        self._loop.call_soon_threadsafe(run)
        done.wait(2)

    def set_states(self, states):
        for eid, state in states.items():
            self.set_state(eid, state)

    def set_target(self, n, x_mm, y_mm, speed_mm_s=0):
        """One LD2450 target as the firmware would report it (None, None: not tracked)."""
        scale = {"mm": 1, "in": 1 / 25.4}[self.length_unit]
        fmt = lambda v: "unknown" if v is None else str(round(v * scale, 2))
        self.set_states({
            self.eid("sensor", f"LD2450 Target-{n} X"): fmt(x_mm),
            self.eid("sensor", f"LD2450 Target-{n} Y"): fmt(y_mm),
            self.eid("sensor", f"LD2450 Target-{n} Speed"): "unknown" if x_mm is None else str(speed_mm_s),
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
        """From a test's thread: an event on Home Assistant's bus."""
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
        while True:
            await asyncio.sleep(1)
            t = time.time() - t0
            people = [
                (1400 * math.sin(t / 7), 2400 + 1300 * math.cos(t / 9), round(300 * math.cos(t / 7))),
                (-1500 + 500 * math.sin(t / 5), 1500 + 300 * math.sin(t / 3), 0),
            ]
            if int(t / 40) % 2:
                people = people[:1]            # the second person steps out now and then
            zones = []
            for n in (1, 2, 3):
                c = [float(self.states[self.eid("number", f"LD2450 Zone-{n} {k}")]["s"]) for k in ("X1", "Y1", "X2", "Y2")]
                zones.append(c)
            for n in (1, 2, 3):
                p = people[n - 1] if n <= len(people) else None
                for name, value in (("X", p and round(p[0])), ("Y", p and round(p[1])), ("Speed", p and p[2]),
                                    ("Distance", p and round(math.hypot(p[0], p[1])))):
                    self._set(self.eid("sensor", f"LD2450 Target-{n} {name}"), "unknown" if p is None else str(value))
            for n, (x1, y1, x2, y2) in enumerate(zones, 1):
                inside = sum(1 for p in people if x1 < p[0] < x2 and y1 < p[1] < y2)
                self._set(self.eid("sensor", f"LD2450 Zone-{n} All Target Count"), str(inside))
            self._set(self.eid("sensor", "LD2450 Presence Target Count"), str(len(people)))
            self._set(self.eid("binary_sensor", "LD2450 Presence"), "on")
            self._set(self.eid("binary_sensor", "LD2412 Presence"), "on")
            self._set(self.eid("binary_sensor", "LD2412 Moving Target"), "on")
            self._set(self.eid("binary_sensor", "LD2412 Still Target"), "on" if len(people) > 1 else "off")
            near = min(math.hypot(p[0], p[1]) for p in people) / 10
            for name, value in (("Moving Distance", round(math.hypot(*people[0][:2]) / 10)), ("Still Distance", round(near))):
                eid = self.eid("sensor", f"LD2412 {name}")
                if eid in self.states:
                    self._set(eid, str(value))
            engineering = self.states[self.eid("switch", "LD2412 Engineering Mode")]["s"] == "on"
            for g in range(GATES):
                centre = math.hypot(*people[0][:2]) / 750
                move = max(0, round(90 - 38 * abs(g + 0.5 - centre) + 6 * math.sin(t + g)))
                still = max(0, round(60 - 25 * abs(g + 0.5 - near / 75) + 4 * math.cos(t + g)))
                self._set(self.eid("sensor", f"LD2412 g{g:02d} move energy"), str(move) if engineering else "unknown")
                self._set(self.eid("sensor", f"LD2412 g{g:02d} still energy"), str(still) if engineering else "unknown")


if __name__ == "__main__":
    import sys
    fake = FakeHA(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8123, token="anything", simulate=True)
    # Shown as arcs on the map once enabled, as on a real sensor
    fake.enable("sensor", "LD2412 Moving Distance", "0", {"unit_of_measurement": "cm"})
    fake.enable("sensor", "LD2412 Still Distance", "0", {"unit_of_measurement": "cm"})
    with fake:
        print(f"Fake Home Assistant with an Apollo R PRO-1 on {fake.url} (token: anything)", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
