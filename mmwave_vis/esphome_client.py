"""ESPHome presence sensors (Apollo R PRO-1) through Home Assistant's WebSocket API.

The sensor isn't a Zigbee device: ESPHome exposes everything it knows as Home
Assistant entities (each target's x and y, the three zones' corners as numbers,
the zone type as a select, the LD2412's gates...). This client finds those
devices in the registries, follows their entities with one subscribe_entities
subscription and turns the states into the same socket.io events the Zigbee
drivers send, so the page doesn't care which kind of device it's showing.

Threads:
  * the listener keeps the subscription open and reconnects when it drops;
  * entity changes are collected per device for FLUSH_S and sent together,
    because one radar frame arrives as a dozen separate state changes;
  * one writer sends zone and setting changes in the order they were made,
    each on its own short connection so a refused call can be reported.

Positions update about once a second: the firmware throttles its LD2450 sensors
to that before they ever reach Home Assistant.
"""
import json
import queue
import threading
import time
import traceback
from collections import deque

from websockets.sync.client import connect as ws_connect

import esphome_entities as ee
import ha_ws

PACKET_RING = 150   # recent entity changes per device, for the diagnostics file


class _Resubscribe(Exception):
    """The devices or their entities changed: connect again to follow the new set."""


class EsphomeClient:
    FLUSH_S = 0.12               # one radar frame's worth of state changes
    RECONNECT_DELAY_S = 5
    RECONNECT_DELAY_MAX_S = 60
    REGISTRY_SETTLE_S = 2        # after Home Assistant's registries change, look at them again
    REDISCOVER_S = 3600          # ... and now and then anyway, in case a change was missed
    RESYNC_MIN_S = 20            # ... or when a page presses Sync, at most this often
    ECHO_TIMEOUT_S = 2.5         # how long a written value may take to come back as a state
    ZONE_SETTLE_S = 0.35         # the radar's read-back after each zone corner (see _do_zones)
    ZONE_PASSES = 2              # write a zone, then once more whatever didn't stick

    def __init__(self, token, url, emit, emit_to_topic, history=None, history_key=None,
                 on_device_list=None, on_entities_changed=None, debug=False):
        self.token = token
        self.url = url
        self._emit = emit                      # emit(event, data, to=sid) → that page, or every page
        self._emit_to_topic = emit_to_topic    # emit_to_topic(event, data, topic) → pages watching it
        self._history = history
        self._history_key = history_key
        self._on_device_list = on_device_list
        self._on_entities_changed = on_entities_changed   # called with the topics it happened to
        self.debug = debug

        self.connected = False
        self.error = None
        self.devices = {}          # key → entry (esphome_entities.device_entry + roles, disabled)
        self.packets = {}          # key → recent (time, role, state)
        self._index = {}           # entity_id → (key, role)
        self._states = {}          # entity_id → {'s': state, 'a': attributes}
        self._dirty = {}           # key → roles changed since the last flush
        self._timers = {}          # key → the flush waiting for the rest of the frame
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._ws = None
        self._discovered_at = 0
        self._rediscover = threading.Event()
        self._stop = threading.Event()
        self._jobs = queue.Queue()

    # --- Lifecycle -------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._run, name="esphome-listener", daemon=True).start()
        threading.Thread(target=self._write_loop, name="esphome-writer", daemon=True).start()

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()      # wakes the listener out of its wait for the next message
            except Exception:
                pass

    def _run(self):
        delay = self.RECONNECT_DELAY_S
        while not self._stop.is_set():
            try:
                self._connect_and_listen()
            except _Resubscribe:
                delay = self.RECONNECT_DELAY_S
                continue
            except Exception as e:
                was_connected, self.connected = self.connected, False
                if self._stop.is_set():
                    return
                self.error = str(e)
                if was_connected:
                    delay = self.RECONNECT_DELAY_S
                if "502" in self.error:
                    print(f"ESPHome: Home Assistant isn't ready yet (502), retrying in {delay}s...", flush=True)
                else:
                    print(f"ESPHome: connection to Home Assistant lost ({e}), retrying in {delay}s...", flush=True)
                self._emit('mqtt_status', {'connected': False, 'error': f"Home Assistant: {e}"})
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, self.RECONNECT_DELAY_MAX_S)

    def _connect_and_listen(self):
        if not self.token:
            raise RuntimeError("No Home Assistant token. As an addon it's provided automatically; "
                               "standalone, set HA_URL and HA_TOKEN (a long-lived access token).")
        # max_size=None: the registry lists pass the default 1 MiB on big installs
        with ws_connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
                        max_size=None) as ws:
            msg = json.loads(ws.recv(timeout=ha_ws.REQUEST_TIMEOUT_S))
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected first message from Home Assistant: {msg.get('type')}")
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            msg = json.loads(ws.recv(timeout=ha_ws.REQUEST_TIMEOUT_S))
            if msg.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant rejected the token (auth failed)")

            next_id = [1]

            def request(type_, **fields):
                req_id = next_id[0]
                next_id[0] += 1
                ws.send(json.dumps({"id": req_id, "type": type_, **fields}))
                deadline = time.time() + ha_ws.REQUEST_TIMEOUT_S
                while True:
                    reply = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
                    if reply.get("id") == req_id and reply.get("type") == "result":
                        if not reply.get("success"):
                            raise RuntimeError(f"{type_} failed: {(reply.get('error') or {}).get('message')}")
                        return reply.get("result")

            # Before subscribing, so no state arrives for an entity nobody knows yet
            self._apply_discovery(*self._fetch_registries(request))
            with self._lock:
                entity_ids = sorted(self._index)
                self._states = {}
            sub_id = None
            if entity_ids:
                sub_id = next_id[0]
                next_id[0] += 1
                ws.send(json.dumps({"id": sub_id, "type": "subscribe_entities", "entity_ids": entity_ids}))
            # A sensor added, renamed or removed, or an entity enabled: look again once
            # Home Assistant has finished changing its registries
            registry_subs = set()
            for event_type in ("entity_registry_updated", "device_registry_updated"):
                registry_subs.add(next_id[0])
                ws.send(json.dumps({"id": next_id[0], "type": "subscribe_events", "event_type": event_type}))
                next_id[0] += 1
            registry_changed_at = 0
            self._ws = ws
            self.connected = True
            self.error = None
            self._rediscover.clear()
            print(f"ESPHome: connected to Home Assistant, following {len(entity_ids)} entities "
                  f"of {len(self.devices)} sensor(s).", flush=True)
            self._emit('mqtt_status', {'connected': True})

            try:
                while not self._stop.is_set():
                    try:
                        msg = json.loads(ws.recv(timeout=2))
                    except TimeoutError:
                        msg = None
                    if msg and msg.get("type") == "event" and msg.get("id") == sub_id:
                        self._on_entities_event(msg.get("event") or {})
                    elif msg and msg.get("type") == "event" and msg.get("id") in registry_subs:
                        registry_changed_at = time.time()
                    elif msg and msg.get("type") == "result" and msg.get("id") == sub_id and not msg.get("success"):
                        raise RuntimeError(f"Home Assistant refused the entity subscription: {msg.get('error')}")
                    now = time.time()
                    if registry_changed_at and now - registry_changed_at >= self.REGISTRY_SETTLE_S:
                        registry_changed_at = 0
                        self._rediscover.set()
                    if self._rediscover.is_set() or now - self._discovered_at > self.REDISCOVER_S:
                        self._rediscover.clear()
                        if self._refresh_discovery():
                            raise _Resubscribe()
            finally:
                self._ws = None

    # --- Discovery ---------------------------------------------------------------

    @staticmethod
    def _fetch_registries(request):
        return (request("config/device_registry/list") or [],
                request("config/entity_registry/list") or [])

    @staticmethod
    def discover(devices, entities):
        """({key: entry}, {entity_id: (key, role)}) for the LD2450 devices in the registries."""
        by_device = {}
        for e in entities:
            if e.get("platform") == "esphome" and e.get("device_id") and e.get("entity_id"):
                by_device.setdefault(e["device_id"], []).append(e)
        found, index = {}, {}
        for dev in devices:
            entries = by_device.get(dev.get("id"))
            if not entries or dev.get("disabled_by"):
                continue
            roles, disabled = ee.entity_roles(entries)
            if not ee.is_tracker(roles, disabled):
                continue
            entry = ee.device_entry(dev)
            if not entry or entry["key"] in found:
                continue
            entry["roles"] = roles
            entry["disabled"] = disabled
            found[entry["key"]] = entry
            for role, entity_id in roles.items():
                index[entity_id] = (entry["key"], role)
        return found, index

    def _apply_discovery(self, devices, entities):
        """Take a fresh look at the registries. True when the entities to follow changed."""
        found, index = self.discover(devices, entities)
        with self._lock:
            listed = lambda d: sorted((k, e["friendly_name"]) for k, e in d.items())
            list_changed = listed(found) != listed(self.devices)
            index_changed = index != self._index
            # An entity was enabled, disabled or renamed on a sensor a page may be showing
            entities = lambda e: (e["roles"], e["disabled"])
            changed = [e["topic"] for k, e in found.items()
                       if k in self.devices and entities(e) != entities(self.devices[k])]
            for key in set(found) - set(self.devices):
                print(f"ESPHome: found {found[key]['friendly_name']} ({found[key].get('model') or 'LD2450 device'})", flush=True)
            self.devices = found
            self._index = index
            self._discovered_at = time.time()
        if list_changed and self._on_device_list:
            self._on_device_list()
        if changed and self._on_entities_changed:
            self._on_entities_changed(changed)
        return index_changed

    def _refresh_discovery(self):
        """On its own connection, so the subscription's events aren't mixed into the replies."""
        try:
            with ha_ws.HAConnection(self.token, self.url) as ha:
                registries = self._fetch_registries(ha.request)
        except ha_ws.HAError as e:
            print(f"ESPHome: couldn't refresh the device list: {e}", flush=True)
            with self._lock:
                self._discovered_at = time.time()   # try again after the usual wait
            return False
        return self._apply_discovery(*registries)

    def device_list(self):
        with self._lock:
            return [{k: v for k, v in d.items() if k not in ("roles", "disabled")} for d in self.devices.values()]

    def key_for_topic(self, topic):
        if not isinstance(topic, str) or not topic.startswith("esphome/"):
            return None
        key = topic[len("esphome/"):]
        return key if key in self.devices else None

    def ieee_for_topic(self, topic):
        # From the topic alone, so a layout can be read before the device is rediscovered
        if not isinstance(topic, str) or not topic.startswith("esphome/") or len(topic) <= len("esphome/"):
            return None
        return f"esphome:{topic[len('esphome/'):]}"

    # --- States ------------------------------------------------------------------------

    def _on_entities_event(self, event):
        """Home Assistant's compressed state stream.

        "a": {entity_id: {"s": state, "a": attributes, ...}} for entities added (all of
        them, right after subscribing); "c": {entity_id: {"+": {"s"?, "a"?: changed
        attributes}, "-": {"a": [removed attributes]}}} for changes; "r": [entity_id]
        for ones removed.
        """
        touched = []
        with self._changed:
            for entity_id, st in (event.get("a") or {}).items():
                if isinstance(st, dict):
                    self._states[entity_id] = {"s": st.get("s"), "a": dict(st.get("a") or {})}
                    touched.append(entity_id)
            for entity_id, diff in (event.get("c") or {}).items():
                cur = self._states.setdefault(entity_id, {"s": None, "a": {}})
                plus = (diff or {}).get("+") or {}
                if "s" in plus:
                    cur["s"] = plus["s"]
                cur["a"].update(plus.get("a") or {})
                for name in ((diff or {}).get("-") or {}).get("a") or []:
                    cur["a"].pop(name, None)
                touched.append(entity_id)
            for entity_id in event.get("r") or []:
                if self._states.pop(entity_id, None) is not None:
                    touched.append(entity_id)
            now = time.time()
            for entity_id in touched:
                key, role = self._index.get(entity_id, (None, None))
                if not key:
                    continue
                self._dirty.setdefault(key, set()).add(role)
                ring = self.packets.get(key)
                if ring is None:
                    ring = self.packets[key] = deque(maxlen=PACKET_RING)
                ring.append((now, role, (self._states.get(entity_id) or {}).get("s")))
                if key not in self._timers:
                    timer = threading.Timer(self.FLUSH_S, self._flush, args=(key,))
                    timer.daemon = True
                    self._timers[key] = timer
                    timer.start()
            self._changed.notify_all()
        if self.debug and touched:
            print(f"[DEBUG] ESPHome states: {len(touched)} changed ({', '.join(touched[:4])}{'…' if len(touched) > 4 else ''})", flush=True)

    def _reader(self, key):
        """read(role) → (state, unit) and read_attrs(role) → attributes, for one device.
        Call with the lock held, or accept that a frame may straddle two reads."""
        roles = (self.devices.get(key) or {}).get("roles") or {}

        def read(role):
            st = self._states.get(roles.get(role)) if role in roles else None
            return (st["s"], st["a"].get("unit_of_measurement")) if st else (None, None)

        def read_attrs(role):
            st = self._states.get(roles.get(role)) if role in roles else None
            return dict(st["a"]) if st else None

        return read, read_attrs

    def _flush(self, key):
        """Send what changed on one device since the last flush to the pages watching it."""
        with self._lock:
            self._timers.pop(key, None)
            roles = self._dirty.pop(key, set())
            dev = self.devices.get(key)
            if not dev or not roles:
                return
            read, _ = self._reader(key)
            payload = ee.build_payload(read, roles)
            targets = ee.build_targets(read) if roles & ee.TARGET_ROLES else None
            topic = dev["topic"]
        try:
            self._record(dev, payload, targets)
        except Exception as e:
            print(f"ESPHome: history error: {e}", flush=True)
        if targets is not None:
            self._emit_to_topic('new_data', {'topic': topic, 'payload': {'seq': None, 'targets': targets}}, topic)
        if payload:
            self._emit_to_topic('device_config', {'topic': topic, 'payload': payload}, topic)

    def _record(self, dev, payload, targets):
        if not self._history or not self._history_key:
            return
        hkey = self._history_key(dev["topic"], dev["ieee"])
        if not hkey:
            return
        if targets is not None:
            self._history.on_targets(hkey, targets)
        state = ee.history_state(payload)
        if state:
            self._history.on_state(hkey, state)

    def send_snapshot(self, key, sid):
        """Everything known about a device, to one page (it just selected it, or pressed Sync)."""
        with self._lock:
            dev = self.devices.get(key)
            if not dev:
                return
            read, _ = self._reader(key)
            payload = ee.build_payload(read)
            targets = ee.build_targets(read)
            reporting = read('t1_x')[0] is not None      # Home Assistant has a state for it
            topic = dev["topic"]
        if payload:
            self._emit('device_config', {'topic': topic, 'payload': payload}, to=sid)
        if reporting:
            self._emit('new_data', {'topic': topic, 'payload': {'seq': None, 'targets': targets}}, to=sid)

    def send_zones(self, key, sid=None):
        """The zones as they are now: the page asks while it waits for a save to land."""
        with self._lock:
            dev = self.devices.get(key)
            if not dev:
                return
            read, _ = self._reader(key)
            zones = ee.build_zones(read)
            topic = dev["topic"]
        if not zones:
            return
        msg = {'topic': topic, 'payload': {ee.ZONE_CATEGORY: zones}}
        if sid:
            self._emit('device_config', msg, to=sid)
        else:
            self._emit_to_topic('device_config', msg, topic)

    def send_settings(self, key, keys, sid):
        """The settings a page asked for; the ones without a state are reported unread."""
        with self._lock:
            dev = self.devices.get(key)
            if not dev:
                return
            read, _ = self._reader(key)
            values = {k: ee.setting_from_state(k, read(k)[0]) for k in keys if ee.setting(k)}
            topic = dev["topic"]
        have = {k: v for k, v in values.items() if v is not None}
        missing = [k for k, v in values.items() if v is None]
        if have:
            self._emit('device_config', {'topic': topic, 'payload': have}, to=sid)
        if missing:
            self._emit('settings_unread', {'topic': topic, 'keys': missing}, to=sid)

    def resync(self):
        """A page pressed Sync: look at the registries again soon (new device, entity enabled)."""
        if time.time() - self._discovered_at > self.RESYNC_MIN_S:
            self._rediscover.set()

    def device_info(self, key):
        with self._lock:
            dev = self.devices.get(key)
            if not dev:
                return None
            read, read_attrs = self._reader(key)
            return ee.build_info(dev, dev["roles"], dev["disabled"], read, read_attrs)

    def diagnostics(self, key):
        with self._lock:
            dev = self.devices.get(key) or {}
            roles = dev.get("roles") or {}
            states = {role: (self._states.get(eid) or {}).get("s") for role, eid in roles.items()}
            return {
                'driver': {'stack': 'esphome', 'connected': self.connected, 'error': self.error,
                           'sensors': len(self.devices), 'entities_followed': len(self._index)},
                'stack_versions': None,
                'device': json.loads(json.dumps(dev, default=str)),
                'settings': states,
                'packets': list(self.packets.get(key) or []),
            }

    # --- Writing ---------------------------------------------------------------------------

    def write_settings(self, sid, key, values):
        self._jobs.put(('settings', sid, key, values))

    def write_zones(self, sid, key, zones):
        self._jobs.put(('zones', sid, key, zones))

    def press(self, sid, key, button):
        self._jobs.put(('press', sid, key, button))

    def _write_loop(self):
        while not self._stop.is_set():
            kind, sid, key, what = self._jobs.get()
            try:
                getattr(self, f"_do_{kind}")(sid, key, what)
            except Exception:
                traceback.print_exc()
                self._emit('command_error', {'error': 'Something went wrong sending that to Home Assistant.'}, to=sid)

    def _entity(self, key, role):
        with self._lock:
            dev = self.devices.get(key) or {}
            entity_id = (dev.get("roles") or {}).get(role)
            st = self._states.get(entity_id) if entity_id else None
            return entity_id, (dict(st, a=dict(st["a"])) if st else None)

    def _wait_for(self, entity_id, predicate, timeout=None):
        """Wait for an entity's state to satisfy predicate(state, attributes)."""
        def ready():
            st = self._states.get(entity_id)
            return bool(st) and predicate(st["s"], st["a"])
        with self._changed:
            return self._changed.wait_for(ready, self.ECHO_TIMEOUT_S if timeout is None else timeout)

    @staticmethod
    def _call(ha, domain, service, entity_id, data):
        ha.request('call_service', domain=domain, service=service, service_data=data,
                   target={'entity_id': entity_id})

    def _do_settings(self, sid, key, values):
        """Write each setting; its new state then reaches the pages like any other change."""
        written, refused, errors = [], [], []
        try:
            with ha_ws.HAConnection(self.token, self.url) as ha:
                for name, value in values.items():
                    title = ee.SETTINGS[name]['title']
                    entity_id, _ = self._entity(key, name)
                    if not entity_id:
                        refused.append(name)
                        errors.append(f"{title} has no enabled entity in Home Assistant.")
                        continue
                    domain, service, data = ee.service_call(name, value)
                    try:
                        self._call(ha, domain, service, entity_id, data)
                    except ha_ws.HAConnectionError:
                        raise
                    except ha_ws.HAError as e:
                        refused.append(name)
                        errors.append(f"Couldn't change {title}: {e}")
                        continue
                    written.append((name, entity_id, value))
        except ha_ws.HAError as e:
            lost = [n for n in values if n not in refused and n not in [w[0] for w in written]]
            if lost:
                refused.extend(lost)
                errors.append(f"Couldn't change {', '.join(ee.SETTINGS[n]['title'] for n in lost)}: {e}")
        if written:
            self._emit('command_ack', {'params': [w[0] for w in written], 'status': 'sent'}, to=sid)
            # A value Home Assistant already had changes no state, so nothing would confirm it
            for name, entity_id, value in written:
                self._wait_for(entity_id, lambda s, a, n=name, v=value: ee.setting_from_state(n, s) == v)
            with self._lock:
                dev = self.devices.get(key)
                read, _ = self._reader(key)
                echo = {n: ee.setting_from_state(n, read(n)[0]) for n, _, _ in written}
            echo = {n: v for n, v in echo.items() if v is not None}
            if dev and echo:
                self._emit_to_topic('device_config', {'topic': dev["topic"], 'payload': echo}, dev["topic"])
        if refused:
            print(f"ESPHome: {' '.join(errors)}", flush=True)
            self._emit('command_error', {'error': ' '.join(errors), 'params': refused}, to=sid)

    def _do_zones(self, sid, key, zones):
        """Write zones a corner at a time.

        Every number the firmware receives makes it send the whole zone table to the
        radar and read it back, publishing all four corners again. A number sent while
        that read-back is on its way can be undone by it, so each corner is given time
        to settle, the zone is checked at the end, and what didn't stick is sent again.
        """
        problems = []
        try:
            with ha_ws.HAConnection(self.token, self.url) as ha:
                for n, coords in sorted(zones.items()):
                    for _ in range(self.ZONE_PASSES):
                        wrong = self._write_zone(ha, key, n, coords)
                        if not wrong:
                            break
                    if wrong:
                        problems.append(f"Zone {n}: {wrong}")
        except ha_ws.HAError as e:
            problems.append(str(e))
        if problems:
            print(f"ESPHome: zone write failed: {' '.join(problems)}", flush=True)
            self._emit('command_error', {'error': ' '.join(problems), 'param': ee.ZONE_CATEGORY}, to=sid)
        self.send_zones(key)

    def _write_zone(self, ha, key, n, coords):
        """One pass over a zone's four corners. Returns '' or what's still wrong."""
        for c in ee.ZONE_COORDS:
            entity_id, st = self._entity(key, f'z{n}_{c}')
            if not entity_id:
                return f"its {c.upper()} entity is missing or disabled in Home Assistant."
            attrs = (st or {}).get("a") or {}
            if st and ee.same_length(st["s"], attrs, coords[c]):
                continue
            self._call(ha, 'number', 'set_value', entity_id, {'value': ee.native_zone_value(coords[c], attrs)})
            if not self._wait_for(entity_id, lambda s, a, cm=coords[c]: ee.same_length(s, a, cm)):
                return "the sensor didn't take the new value."
            time.sleep(self.ZONE_SETTLE_S)
        for c in ee.ZONE_COORDS:
            entity_id, st = self._entity(key, f'z{n}_{c}')
            if not st or not ee.same_length(st["s"], st["a"], coords[c]):
                return "the sensor reported a different zone than was sent."
        return ''

    def _do_press(self, sid, key, button):
        entity_id, _ = self._entity(key, button)
        if not entity_id:
            self._emit('command_error', {'error': "That button's entity is missing or disabled in Home Assistant."}, to=sid)
            return
        try:
            with ha_ws.HAConnection(self.token, self.url) as ha:
                self._call(ha, 'button', 'press', entity_id, {})
        except ha_ws.HAError as e:
            self._emit('command_error', {'error': f"Couldn't press it: {e}"}, to=sid)
            return
        self._emit('command_ack', {'command': button, 'status': 'sent'}, to=sid)
