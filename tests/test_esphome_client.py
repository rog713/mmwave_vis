"""
Tests for EsphomeClient against a stand-in Home Assistant (tests/fake_ha.py) with
an Apollo R PRO-1 on it: discovery, live positions, and writing zones, settings
and buttons through real WebSocket connections on localhost.
"""

import os
import sys
import threading
import time

import pytest

pytest.importorskip("websockets.asyncio.server")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'mmwave_vis'))

import esphome_entities as ee
from esphome_client import EsphomeClient
from tests.fake_ha import FakeHA, entity_id

KEY = "aabbcca1b2c3"
TOPIC = f"esphome/{KEY}"
SID = "browser-1"


class Page:
    """Collects what the client emits, as the socket.io layer would send it."""

    def __init__(self):
        self.events = []            # (event, data, sid or None)
        self._cond = threading.Condition()

    def emit(self, event, data=None, to=None):
        with self._cond:
            self.events.append((event, data, to))
            self._cond.notify_all()

    def emit_to_topic(self, event, data, topic):
        self.emit(event, data, to=f"topic:{topic}")

    def wait(self, event, match=lambda data: True, timeout=5, since=0):
        """The first `event` from index `since` on whose data satisfies match()."""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                for name, data, _ in self.events[since:]:
                    if name == event and match(data):
                        return data
                left = deadline - time.time()
                if left <= 0:
                    raise AssertionError(f"no {event} matched; got {[e[0] for e in self.events[since:]]}")
                self._cond.wait(left)

    def mark(self):
        with self._cond:
            return len(self.events)

    def all(self, event, since=0):
        with self._cond:
            return [data for name, data, _ in self.events[since:] if name == event]


class History:
    def __init__(self):
        self.targets, self.states = [], []

    def on_targets(self, key, targets):
        self.targets.append((key, targets))

    def on_state(self, key, state):
        self.states.append((key, state))


def wait_until(check, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def make_client(ha, page, token=None, history=None):
    client = EsphomeClient(ha.token if token is None else token, ha.url, page.emit, page.emit_to_topic,
                           history=history, history_key=lambda topic, ieee: f"ieee:{ieee}")
    client.FLUSH_S = 0.05
    client.ZONE_SETTLE_S = 0.02
    client.ECHO_TIMEOUT_S = 1.5
    client.RECONNECT_DELAY_S = 0.2
    return client


@pytest.fixture
def setup():
    """(fake Home Assistant, connected client, page)."""
    page = Page()
    with FakeHA() as ha:
        client = make_client(ha, page, history=History())
        client.start()
        assert wait_until(lambda: client.connected and KEY in client.devices), client.error
        # The states of every followed entity arrive right after subscribing
        assert wait_until(lambda: len(client._states) >= len(client._index))
        try:
            yield ha, client, page
        finally:
            client.stop()


def zone_ids(n):
    return {c: entity_id("number", f"LD2450 Zone-{n} {c.upper()}") for c in ee.ZONE_COORDS}


# ===========================================================================
# Discovery
# ===========================================================================

def test_finds_the_r_pro_1_and_nothing_else(setup):
    ha, client, page = setup
    devices = client.device_list()
    assert [d["topic"] for d in devices] == [TOPIC]
    assert devices[0]["friendly_name"] == "Apollo R_PRO-1 a1b2c3"
    assert devices[0]["ieee"] == f"esphome:{KEY}"
    assert devices[0]["model"] == "R-PRO-1-ETH"
    assert "roles" not in devices[0]
    assert "switch.smart_plug_relay" not in client._index
    # Disabled entities have no state to follow
    assert entity_id("sensor", "LD2412 Moving Distance") not in client._index
    assert page.wait("mqtt_status", lambda d: d["connected"] is True)


def test_topic_and_storage_key(setup):
    _, client, _ = setup
    assert client.key_for_topic(TOPIC) == KEY
    assert client.key_for_topic("esphome/unknown") is None
    assert client.key_for_topic("zigbee2mqtt/Hall") is None
    assert client.ieee_for_topic(TOPIC) == f"esphome:{KEY}"
    assert client.ieee_for_topic("esphome/") is None
    assert client.ieee_for_topic(None) is None


def test_snapshot_for_a_page_that_selects_the_sensor(setup):
    _, client, page = setup
    at = page.mark()
    client.send_snapshot(KEY, SID)
    config = page.wait("device_config", since=at)
    assert config["topic"] == TOPIC
    payload = config["payload"]
    assert payload["occupancy"] is False
    assert payload["illuminance"] == 182
    assert payload["ld2450_zone_type"] == "Disabled"
    assert payload["ld2450_timeout"] == 5
    assert payload["ld2412_g05_move_threshold"] == 40
    assert payload[ee.ZONE_CATEGORY] == {f"area{n}": {"x_min": 0, "x_max": 0, "y_min": 0, "y_max": 0} for n in (1, 2, 3)}
    assert payload["esphome"]["available"] is True
    assert "ld2412_distance_resolution" not in payload           # disabled in Home Assistant
    assert page.wait("new_data", since=at)["payload"]["targets"] == []
    # Only to the page that asked
    assert all(to == SID for name, _, to in page.events[at:] if name in ("device_config", "new_data"))


def test_device_info(setup):
    _, client, _ = setup
    info = client.device_info(KEY)
    assert info["firmware"]["build"] == "26.3.2.1"
    assert info["ip_address"] == "192.168.1.57"
    assert info["update"]["available"] is False
    assert "ld2412_distance_resolution" in info["unsupported_settings"]
    assert client.device_info("nope") is None


# ===========================================================================
# Live data
# ===========================================================================

def test_a_radar_frame_becomes_one_target_report(setup):
    ha, client, page = setup
    at = page.mark()
    ha.set_target(1, -1234, 2466, -320)
    frame = page.wait("new_data", lambda d: len(d["payload"]["targets"]) == 1 and d["payload"]["targets"][0]["dop"] != 0, since=at)
    assert frame["topic"] == TOPIC
    assert frame["payload"]["targets"] == [{"id": 1, "x": -123, "y": 247, "z": 0, "dop": -32}]
    # And it was recorded for the heat map under the sensor's own key
    assert wait_until(lambda: client._history.targets and client._history.targets[-1] ==
                      (f"ieee:esphome:{KEY}", frame["payload"]["targets"]))


def test_a_target_that_leaves_is_dropped(setup):
    ha, _, page = setup
    ha.set_target(2, 500, 1500)
    page.wait("new_data", lambda d: [t["id"] for t in d["payload"]["targets"]] == [2])
    at = page.mark()
    ha.set_target(2, None, None)
    page.wait("new_data", lambda d: d["payload"]["targets"] == [], since=at)


def test_presence_and_zone_counts_reach_the_page_and_the_timeline(setup):
    ha, client, page = setup
    at = page.mark()
    ha.set_state(entity_id("binary_sensor", "LD2412 Presence"), "on")
    ha.set_state(entity_id("sensor", "LD2450 Zone-2 All Target Count"), "1")
    page.wait("device_config", lambda d: d["payload"].get("occupancy") is True, since=at)
    page.wait("device_config", lambda d: d["payload"].get("mmwave_area2_occupancy") is True, since=at)
    page.wait("device_config", lambda d: (d["payload"].get("esphome") or {}).get("ld2412_presence") is True, since=at)
    assert wait_until(lambda: any(s.get("occupancy") is True for _, s in client._history.states))
    assert wait_until(lambda: any(s.get("area2") is True for _, s in client._history.states))


def test_positions_on_a_us_customary_install():
    page = Page()
    with FakeHA(length_unit="in") as ha:
        client = make_client(ha, page)
        client.start()
        try:
            assert wait_until(lambda: client.connected and len(client._states) >= len(client._index))
            ha.set_target(1, 1000, 2000)
            frame = page.wait("new_data", lambda d: d["payload"]["targets"] and d["payload"]["targets"][0]["y"] == 200)
            assert frame["payload"]["targets"][0]["x"] == 100
        finally:
            client.stop()


# ===========================================================================
# Zones
# ===========================================================================

def test_zone_write_sets_the_four_corners(setup):
    ha, client, page = setup
    ids = zone_ids(1)
    client.write_zones(SID, KEY, {1: {"x1": -200, "y1": 80, "x2": -20, "y2": 220}})
    want = {"x1": "-2000", "y1": "800", "x2": "-200", "y2": "2200"}
    assert wait_until(lambda: all(ha.states[ids[c]]["s"] == want[c] for c in want))
    zones = page.wait("device_config", lambda d: (d["payload"].get(ee.ZONE_CATEGORY) or {}).get("area1") ==
                      {"x_min": -200, "x_max": -20, "y_min": 80, "y_max": 220})
    assert zones["topic"] == TOPIC
    assert [(c[0], c[1], c[2]) for c in ha.calls] == [("number", "set_value", ids[c]) for c in ee.ZONE_COORDS]
    assert not page.all("command_error")


def test_zone_write_only_sends_the_corners_that_change(setup):
    ha, client, page = setup
    ids = zone_ids(2)
    client.write_zones(SID, KEY, {2: {"x1": 100, "y1": 100, "x2": 300, "y2": 300}})
    assert wait_until(lambda: ha.states[ids["y2"]]["s"] == "3000")
    assert wait_until(lambda: len(ha.calls) == 4)
    del ha.calls[:]
    client.write_zones(SID, KEY, {2: {"x1": 100, "y1": 100, "x2": 350, "y2": 300}})
    assert wait_until(lambda: ha.states[ids["x2"]]["s"] == "3500")
    page.wait("device_config", lambda d: (d["payload"].get(ee.ZONE_CATEGORY) or {}).get("area2", {}).get("x_max") == 350)
    assert [(c[2], c[3]) for c in ha.calls] == [(ids["x2"], {"value": 3500})]


def test_clearing_a_zone_writes_zeros(setup):
    ha, client, _ = setup
    ids = zone_ids(3)
    client.write_zones(SID, KEY, {3: {"x1": -50, "y1": 10, "x2": 50, "y2": 90}})
    assert wait_until(lambda: ha.states[ids["y2"]]["s"] == "900")
    client.write_zones(SID, KEY, {3: {"x1": 0, "y1": 0, "x2": 0, "y2": 0}})
    assert wait_until(lambda: all(ha.states[e]["s"] == "0" for e in ids.values()))


def test_zone_write_in_inches():
    page = Page()
    with FakeHA(length_unit="in") as ha:
        client = make_client(ha, page)
        client.start()
        try:
            assert wait_until(lambda: client.connected and len(client._states) >= len(client._index))
            client.write_zones(SID, KEY, {1: {"x1": -254, "y1": 0, "x2": 0, "y2": 127}})
            page.wait("device_config", lambda d: (d["payload"].get(ee.ZONE_CATEGORY) or {}).get("area1") ==
                      {"x_min": -254, "x_max": 0, "y_min": 0, "y_max": 127})
            ids = zone_ids(1)
            assert float(ha.states[ids["x1"]]["s"]) == pytest.approx(-100)
            assert float(ha.states[ids["y2"]]["s"]) == pytest.approx(50)
            assert not page.all("command_error")
        finally:
            client.stop()


def test_zone_write_reports_a_missing_entity(setup):
    _, client, page = setup
    with client._lock:
        del client.devices[KEY]["roles"]["z1_y2"]
    client.write_zones(SID, KEY, {1: {"x1": -100, "y1": 0, "x2": 100, "y2": 200}})
    error = page.wait("command_error")
    assert error["param"] == ee.ZONE_CATEGORY
    assert "Zone 1" in error["error"] and "Y2" in error["error"]


def test_zones_are_resent_when_the_page_asks(setup):
    _, client, page = setup
    at = page.mark()
    client.send_zones(KEY, SID)
    assert set(page.wait("device_config", since=at)["payload"]) == {ee.ZONE_CATEGORY}


# ===========================================================================
# Settings and buttons
# ===========================================================================

def test_settings_are_written_to_their_entities(setup):
    ha, client, page = setup
    client.write_settings(SID, KEY, {"ld2450_zone_type": "Filter", "ld2412_engineering_mode": "ON", "ld2450_timeout": 12})
    ack = page.wait("command_ack")
    assert ack == {"params": ["ld2450_zone_type", "ld2412_engineering_mode", "ld2450_timeout"], "status": "sent"}
    assert ha.states[entity_id("select", "LD2450 Zone Type")]["s"] == "Filter"
    assert ha.states[entity_id("switch", "LD2412 Engineering Mode")]["s"] == "on"
    assert ha.states[entity_id("number", "LD2450 Timeout")]["s"] == "12"
    # The page counts a change as saved when the new value comes back
    echo = page.wait("device_config", lambda d: d["payload"].get("ld2450_zone_type") == "Filter")
    assert echo["topic"] == TOPIC
    page.wait("device_config", lambda d: d["payload"].get("ld2412_engineering_mode") == "ON")
    page.wait("device_config", lambda d: d["payload"].get("ld2450_timeout") == 12)


def test_a_value_the_sensor_already_has_is_still_confirmed(setup):
    _, client, page = setup
    at = page.mark()
    client.write_settings(SID, KEY, {"ld2450_timeout": 5})
    page.wait("device_config", lambda d: d["payload"] == {"ld2450_timeout": 5}, since=at)


def test_a_setting_whose_entity_is_disabled_is_refused(setup):
    ha, client, page = setup
    client.write_settings(SID, KEY, {"ld2412_distance_resolution": "0.5m", "ld2450_timeout": 30})
    error = page.wait("command_error")
    assert error["params"] == ["ld2412_distance_resolution"]
    assert "Distance resolution" in error["error"]
    assert page.wait("command_ack")["params"] == ["ld2450_timeout"]
    assert ha.states[entity_id("number", "LD2450 Timeout")]["s"] == "30"


def test_settings_asked_for_are_sent_and_the_missing_ones_named(setup):
    _, client, page = setup
    at = page.mark()
    client.send_settings(KEY, ["ld2450_timeout", "ld2412_distance_resolution", "not_a_setting"], SID)
    assert page.wait("device_config", since=at)["payload"] == {"ld2450_timeout": 5}
    assert page.wait("settings_unread", since=at) == {"topic": TOPIC, "keys": ["ld2412_distance_resolution"]}


def test_pressing_a_button(setup):
    ha, client, page = setup
    client.press(SID, KEY, "ld2412_calibrate")
    assert page.wait("command_ack") == {"command": "ld2412_calibrate", "status": "sent"}
    assert ("button", "press", entity_id("button", "LD2412 Start Dynamic Background Correction"), {}) in ha.calls


# ===========================================================================
# Connection
# ===========================================================================

def test_a_rejected_token_is_reported():
    page = Page()
    with FakeHA() as ha:
        client = make_client(ha, page, token="wrong")
        client.start()
        try:
            status = page.wait("mqtt_status", lambda d: d["connected"] is False)
            assert "rejected the token" in status["error"]
            assert client.connected is False and client.device_list() == []
        finally:
            client.stop()


def test_an_entity_enabled_in_home_assistant_is_followed(setup):
    ha, client, page = setup
    client.REGISTRY_SETTLE_S = 0.2
    told = []
    client._on_entities_changed = told.append
    assert "ld2412_still_distance" not in client.devices[KEY]["roles"]
    # Home Assistant announces the registry change; nobody has to press anything
    eid = ha.enable("sensor", "LD2412 Still Distance", "142", {"unit_of_measurement": "cm"})
    assert wait_until(lambda: client.devices[KEY]["roles"].get("ld2412_still_distance") == eid, timeout=8)
    assert wait_until(lambda: client.connected and eid in client._states)
    assert "ld2412_still_distance" not in client.devices[KEY]["disabled"]
    assert told == [[TOPIC]]           # so the pages showing it refresh its details


def test_sync_looks_at_the_registries_again(setup):
    ha, client, page = setup
    eid = ha.enable("sensor", "LD2412 Moving Distance", "187", {"unit_of_measurement": "cm"}, announce=False)
    assert "ld2412_moving_distance" not in client.devices[KEY]["roles"]
    client.resync()                    # too soon after the last look: nothing happens
    assert not client._rediscover.is_set()
    client._discovered_at = 0          # as if Sync was pressed a while after the last look
    client.resync()
    assert wait_until(lambda: client.devices[KEY]["roles"].get("ld2412_moving_distance") == eid)
    assert wait_until(lambda: client.connected and eid in client._states)
    at = page.mark()
    client.send_snapshot(KEY, SID)
    assert page.wait("device_config", since=at)["payload"]["esphome"]["ld2412_moving_distance"] == 187
    assert "ld2412_moving_distance" not in client.device_info(KEY)["disabled_entities"]


def test_diagnostics_hold_states_and_recent_changes(setup):
    ha, client, _ = setup
    ha.set_state(entity_id("binary_sensor", "LD2450 Presence"), "on")
    assert wait_until(lambda: any(role == "ld2450_presence" and state == "on"
                                  for _, role, state in client.diagnostics(KEY)["packets"]))
    parts = client.diagnostics(KEY)
    assert parts["driver"]["stack"] == "esphome" and parts["driver"]["connected"] is True
    assert parts["settings"]["ld2450_presence"] == "on"
    assert parts["device"]["topic"] == TOPIC
    assert client.diagnostics(None)["device"] == {}
