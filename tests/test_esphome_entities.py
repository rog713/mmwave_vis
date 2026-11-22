"""
Tests for esphome_entities: telling an ESPHome presence sensor's Home Assistant
entities apart, converting their units, and building what the page shows.

The sensor here is the Apollo R PRO-1 as tests/fake_ha.py describes it, with the
entity names Apollo's firmware uses.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'mmwave_vis'))

import esphome_entities as ee
from tests.fake_ha import MAC, r_pro_1, entity_id, unique_id


def registry(length_unit="mm", unique_ids="name"):
    """The R PRO-1's entity registry entries, as Home Assistant lists them."""
    return [{
        "entity_id": entity_id(domain, name), "original_name": name,
        "unique_id": unique_id(platform, name, unique_ids),
        "disabled_by": "integration" if disabled else None,
    } for domain, platform, name, _state, _attrs, disabled in r_pro_1(length_unit)]


def reader(values):
    """read(role) over {role: state} or {role: (state, unit)}."""
    def read(role):
        v = values.get(role)
        if v is None:
            return None, None
        return v if isinstance(v, tuple) else (v, None)
    return read


# ===========================================================================
# Which entity is which
# ===========================================================================

def test_slug_matches_however_the_name_is_written():
    assert ee.slug("LD2450 Target-1 X") == "ld2450_target_1_x"
    assert ee.slug("ld2450_target-1_x") == "ld2450_target_1_x"
    assert ee.slug("  LD2412 g03 move threshold ") == "ld2412_g03_move_threshold"


def test_r_pro_1_entities_get_their_roles():
    roles, disabled = ee.entity_roles(registry())
    assert roles["t1_x"] == entity_id("sensor", "LD2450 Target-1 X")
    assert roles["t3_y"] == entity_id("sensor", "LD2450 Target-3 Y")
    assert roles["z2_x1"] == entity_id("number", "LD2450 Zone-2 X1")
    assert roles["z3_count"] == entity_id("sensor", "LD2450 Zone-3 All Target Count")
    assert roles["ld2450_presence"] == entity_id("binary_sensor", "LD2450 Presence")
    assert roles["ld2412_presence"] == entity_id("binary_sensor", "LD2412 Presence")
    assert roles["ld2450_zone_type"] == entity_id("select", "LD2450 Zone Type")
    assert roles["ld2412_g07_still_threshold"] == entity_id("number", "LD2412 g07 still threshold")
    assert roles["g13_move"] == entity_id("sensor", "LD2412 g13 move energy")
    assert roles["illuminance"] == entity_id("sensor", "LTR390 Light")
    assert roles["ld2412_calibrate"] == entity_id("button", "LD2412 Start Dynamic Background Correction")
    assert roles["update"] == entity_id("update", "Firmware Update")
    assert ee.is_tracker(roles, disabled)


def test_entities_apollo_ships_disabled_are_listed_separately():
    roles, disabled = ee.entity_roles(registry())
    for role in ("ld2412_moving_distance", "ld2412_still_distance", "ld2412_distance_resolution", "ld2450_multi_target"):
        assert role not in roles
        assert role in disabled


def test_every_catalogue_setting_exists_on_the_r_pro_1():
    roles, disabled = ee.entity_roles(registry())
    for key in ee.SETTINGS:
        assert key in roles or key in disabled, key


def test_presence_is_not_mistaken_for_the_target_count():
    roles, _ = ee.entity_roles(registry())
    assert roles["target_count"] == entity_id("sensor", "LD2450 Presence Target Count")
    assert roles["ld2450_presence"].startswith("binary_sensor.")


@pytest.mark.parametrize("unique", [
    f"{MAC}/0/sensor/LD2450 Target-1 X",        # Home Assistant now: the name, after the sub-device
    f"{MAC}-sensor-ld2450_target-1_x",          # before: ESPHome's object id for the name
    f"{MAC}-sensor-LD2450 Target-1 X",          # or the name
    "aabbcca1b2c3-sensor-ld2450_target-1_x",
])
def test_role_from_the_unique_id_when_the_entity_was_renamed(unique):
    assert ee.role_of({"entity_id": "sensor.couch_x", "original_name": None, "unique_id": unique}) == "t1_x"


def test_name_in_a_unique_id():
    assert ee.name_in_unique_id(f"{MAC}/0/number/LD2450 Zone-2 X1") == "LD2450 Zone-2 X1"
    assert ee.name_in_unique_id(f"{MAC}/0/text_sensor/Apollo Firmware Version") == "Apollo Firmware Version"
    assert ee.name_in_unique_id(f"{MAC}-binary_sensor-ld2450_presence") == "ld2450_presence"
    # An entity of a sub-device, in the older forms
    assert ee.name_in_unique_id(f"{MAC}-sensor-ld2450_target-1_x@3232") == "ld2450_target-1_x"
    assert ee.name_in_unique_id(f"{MAC}/3232/sensor/Battery") == "Battery"
    for other in (None, "", 17, "0x0c2a6ffffef6f6a7-1-1030", "not a unique id"):
        assert ee.name_in_unique_id(other) is None


@pytest.mark.parametrize("unique_ids", ["name", "object_id"])
def test_every_role_is_found_from_the_unique_id_alone(unique_ids):
    """Entities renamed in Home Assistant and listed without their original name."""
    named = registry()
    bare = [{**e, "original_name": None, "entity_id": f"{e['entity_id'].split('.')[0]}.thing_{i}"}
            for i, e in enumerate(registry(unique_ids=unique_ids))]
    by_name, _ = ee.entity_roles(named)
    by_unique_id, _ = ee.entity_roles(bare)
    assert set(by_unique_id) == set(by_name)


def test_role_from_the_entity_id_alone():
    assert ee.role_of({"entity_id": "sensor.living_room_ld2450_target_2_y"}) == "t2_y"
    assert ee.role_of({"entity_id": "number.x_ld2450_zone_1_x2"}) == "z1_x2"
    # The whole name has to match, not just its tail
    assert ee.role_of({"entity_id": "sensor.living_room_ld2450_zone_1_still_target_count"}) == "z1_still_count"
    assert ee.role_of({"entity_id": "sensor.living_room_temperature"}) is None
    assert ee.role_of({"entity_id": "switch.smart_plug_relay", "original_name": "Relay"}) is None


def test_text_sensors_are_sensors_in_home_assistant():
    entry = {"entity_id": "sensor.r_pro_1_apollo_firmware_version", "original_name": "Apollo Firmware Version",
             "unique_id": f"{MAC}-text_sensor-apollo_firmware_version"}
    assert ee.role_of(entry) == "firmware"
    assert ee.role_of({"entity_id": "sensor.fw", "unique_id": f"{MAC}/0/text_sensor/Apollo Firmware Version"}) == "firmware"


def test_same_named_entities_of_other_kinds_get_no_role():
    # The firmware has a "Firmware Update" button beside its update entity
    assert ee.role_of({"entity_id": entity_id("update", "Firmware Update"), "original_name": "Firmware Update"}) == "update"
    assert ee.role_of({"entity_id": entity_id("button", "Firmware Update"), "original_name": "Firmware Update"}) is None
    # ... and target counts for the whole radar that aren't a zone's
    assert ee.role_of({"entity_id": entity_id("sensor", "LD2450 Still Target Count")}) is None
    assert ee.role_of({"entity_id": entity_id("sensor", "LD2412 Light")}) is None


def test_a_device_without_position_entities_is_not_a_tracker():
    roles, disabled = ee.entity_roles([{"entity_id": "binary_sensor.msr_2_ld2412_presence", "original_name": "LD2412 Presence"}])
    assert roles == {"ld2412_presence": "binary_sensor.msr_2_ld2412_presence"}
    assert not ee.is_tracker(roles, disabled)


def test_device_entry_is_keyed_by_mac():
    entry = ee.device_entry({"id": "dev1", "name": "Apollo R_PRO-1 a1b2c3", "name_by_user": "Living room",
                             "connections": [["mac", "aa:bb:cc:a1:b2:c3"]], "model": "R-PRO-1-ETH"})
    assert entry["key"] == "aabbcca1b2c3"
    assert entry["topic"] == "esphome/aabbcca1b2c3"
    assert entry["friendly_name"] == "Living room"
    assert entry["ieee"] == "esphome:aabbcca1b2c3"
    assert ee.device_entry({"id": "dev2", "name": "No MAC"})["key"] == "dev2"
    assert ee.device_entry({}) is None


# ===========================================================================
# Units
# ===========================================================================

def test_lengths_convert_from_whatever_home_assistant_shows():
    assert ee.length_cm("1234", "mm") == pytest.approx(123.4)
    assert ee.length_cm("48.6", "in") == pytest.approx(123.444)
    assert ee.length_cm("2.5", "m") == pytest.approx(250)
    assert ee.length_cm("150", None) == pytest.approx(15)             # ESPHome's millimetres
    assert ee.length_cm("150", None, default="cm") == pytest.approx(150)
    assert ee.length_cm("12", "furlong") is None


@pytest.mark.parametrize("state", [None, "", "unknown", "unavailable", "nan", "abc", True])
def test_states_that_are_not_numbers(state):
    assert ee.number(state) is None
    assert ee.length_cm(state, "mm") is None


def test_speed_in_cm_per_second():
    assert ee.speed_cm_s("-320", "mm/s") == pytest.approx(-32)
    assert ee.speed_cm_s("1", "m/s") == pytest.approx(100)
    assert ee.speed_cm_s("160", None) == pytest.approx(16)


def test_lengths_convert_back_for_writing():
    assert ee.length_from_cm(123, "mm") == pytest.approx(1230)
    assert ee.length_from_cm(254, "in") == pytest.approx(100)
    assert ee.length_from_cm(50, None) == pytest.approx(500)


# ===========================================================================
# Targets
# ===========================================================================

def test_targets_in_whole_centimetres():
    targets = ee.build_targets(reader({
        "t1_x": ("-1234", "mm"), "t1_y": ("2466", "mm"), "t1_speed": ("-320", "mm/s"),
        "t2_x": ("unknown", "mm"), "t2_y": ("unknown", "mm"),
    }))
    assert targets == [{"id": 1, "x": -123, "y": 247, "z": 0, "dop": -32}]


def test_untracked_targets_are_left_out():
    read = reader({
        "t1_x": "0.0", "t1_y": "0.0", "t1_speed": "0.0",        # older firmware: 0, 0
        "t2_x": "unavailable", "t2_y": "unavailable",
        "t3_x": "500", "t3_y": "unknown",
    })
    assert ee.build_targets(read) == []


def test_a_target_straight_ahead_is_kept():
    assert ee.build_targets(reader({"t2_x": "0", "t2_y": "1800"})) == [{"id": 2, "x": 0, "y": 180, "z": 0, "dop": 0}]


def test_targets_on_a_us_customary_install():
    targets = ee.build_targets(reader({"t1_x": ("39.37007874015748", "in"), "t1_y": ("78.74015748031496", "in"),
                                       "t1_speed": ("0.0", "in/s")}))
    assert targets == [{"id": 1, "x": 100, "y": 200, "z": 0, "dop": 0}]


# ===========================================================================
# Zones
# ===========================================================================

def test_zone_is_reported_with_min_below_max():
    read = reader({"z1_x1": "500", "z1_y1": "2200", "z1_x2": "-1500", "z1_y2": "800"})
    assert ee.build_zone(read, 1) == {"x_min": -150, "x_max": 50, "y_min": 80, "y_max": 220}


def test_zone_waits_for_all_four_corners():
    assert ee.build_zone(reader({"z1_x1": "500", "z1_y1": "0", "z1_x2": "unknown", "z1_y2": "0"}), 1) is None
    assert ee.build_zone(reader({}), 2) is None


def test_zones_only_lists_the_ones_reported():
    read = reader({**{f"z1_{c}": "0" for c in ee.ZONE_COORDS},
                   "z3_x1": "-1000", "z3_y1": "0", "z3_x2": "1000", "z3_y2": "3000"})
    assert ee.build_zones(read) == {
        "area1": {"x_min": 0, "x_max": 0, "y_min": 0, "y_max": 0},          # an unused zone
        "area3": {"x_min": -100, "x_max": 100, "y_min": 0, "y_max": 300},
    }


def test_zone_write_from_the_page():
    zones, error = ee.parse_zone_write({"area2": {"width_min": -200, "width_max": -20, "depth_min": 80, "depth_max": 220}})
    assert error is None
    assert zones == {2: {"x1": -200, "y1": 80, "x2": -20, "y2": 220}}


def test_zone_write_puts_corners_in_the_order_the_radar_counts():
    zones, _ = ee.parse_zone_write({"area1": {"width_min": 100, "width_max": -100, "depth_min": 300, "depth_max": 50}})
    assert zones == {1: {"x1": -100, "y1": 50, "x2": 100, "y2": 300}}


def test_zone_write_clears_with_zeros_and_ignores_heights():
    zones, error = ee.parse_zone_write({"area3": {"width_min": 0, "width_max": 0, "depth_min": 0, "depth_max": 0,
                                                  "height_min": 0, "height_max": 0}})
    assert error is None and zones == {3: {"x1": 0, "y1": 0, "x2": 0, "y2": 0}}


@pytest.mark.parametrize("value", [
    None, {}, [],
    {"area4": {"width_min": 0, "width_max": 10, "depth_min": 0, "depth_max": 10}},      # the LD2450 has three
    {"area0": {"width_min": 0, "width_max": 10, "depth_min": 0, "depth_max": 10}},
    {"area1": "big"},
    {"area1": {"width_min": 0, "width_max": 10, "depth_min": 0}},                        # a corner missing
    {"area1": {"width_min": 0, "width_max": 10.5, "depth_min": 0, "depth_max": 10}},
    {"area1": {"width_min": -487, "width_max": 10, "depth_min": 0, "depth_max": 10}},    # x beyond ±486 cm
    {"area1": {"width_min": 0, "width_max": 10, "depth_min": -1, "depth_max": 10}},      # behind the sensor
    {"area1": {"width_min": 0, "width_max": 10, "depth_min": 0, "depth_max": 757}},
    {"area1": {"width_min": 0, "width_max": 10, "depth_min": 0, "depth_max": 10, "colour": "red"}},
])
def test_zone_write_refuses(value):
    zones, error = ee.parse_zone_write(value)
    assert zones is None and error


def test_zone_value_in_the_entitys_unit():
    mm = {"unit_of_measurement": "mm", "min": -4860, "max": 4860}
    assert ee.native_zone_value(-123, mm) == -1230
    assert isinstance(ee.native_zone_value(-123, mm), int)
    assert ee.native_zone_value(100, None) == 1000


def test_zone_value_in_inches_never_passes_the_sensors_range():
    # Home Assistant rounds the range it reports outwards (-4860 mm is -191.339 in), and
    # the sensor ignores a value that converts back to even a hair more than its own
    inches = {"unit_of_measurement": "in", "min": -191.4, "max": 191.4}
    assert ee.native_zone_value(254, inches) == 100
    assert ee.native_zone_value(-123, inches) == -48.425
    for cm in (-486, 486, -123, 7, 50, 333):
        mm = ee.native_zone_value(cm, inches) * 25.4
        assert abs(mm) <= abs(cm) * 10 + 1e-9, cm          # never further out than asked
        assert abs(cm) * 10 - abs(mm) < 0.03, cm           # and a whisker short at most


def test_zone_value_stays_inside_the_entitys_range():
    y = {"unit_of_measurement": "mm", "min": 0, "max": 7560}
    assert ee.native_zone_value(800, y) == 7560
    assert ee.native_zone_value(-5, y) == 0


def test_same_length_allows_for_unit_rounding():
    mm, inches = {"unit_of_measurement": "mm"}, {"unit_of_measurement": "in"}
    assert ee.same_length("1230.0", mm, 123)
    assert ee.same_length("1229.0", mm, 123)            # the sensor dropped a fraction of a millimetre
    assert not ee.same_length("1240.0", mm, 123)
    assert not ee.same_length("1220.0", mm, 123)
    assert ee.same_length("1230", None, 123)            # no unit given: millimetres
    assert not ee.same_length("unknown", mm, 0)
    # In inches Home Assistant shows one decimal: 1229 mm is 48.4 in, 499 mm is 19.6 in
    assert ee.same_length("48.425", inches, 123)
    assert ee.same_length("48.4", inches, 123)
    assert ee.same_length("19.6", inches, 50)
    assert not ee.same_length("19.5", inches, 50)
    assert not ee.same_length("48.0", inches, 123)
    assert not ee.same_length("5.0", {"unit_of_measurement": "furlongs"}, 123)


# ===========================================================================
# What the page is sent
# ===========================================================================

def test_occupied_when_either_radar_sees_someone():
    assert ee.build_payload(reader({"ld2450_presence": "off", "ld2412_presence": "on"}))["occupancy"] is True
    assert ee.build_payload(reader({"ld2450_presence": "off", "ld2412_presence": "off"}))["occupancy"] is False
    assert ee.build_payload(reader({"ld2450_presence": "on"}))["occupancy"] is True
    assert "occupancy" not in ee.build_payload(reader({"ld2450_presence": "unavailable", "ld2412_presence": "unknown"}))


def test_zone_counts_become_area_occupancy():
    payload = ee.build_payload(reader({"z1_count": "2", "z2_count": "0", "z3_count": "unknown"}))
    assert payload["mmwave_area1_occupancy"] is True
    assert payload["mmwave_area2_occupancy"] is False
    assert "mmwave_area3_occupancy" not in payload


def test_payload_carries_settings_light_and_zones():
    payload = ee.build_payload(reader({
        "illuminance": ("182.4", "lx"), "ld2450_timeout": "12.0", "ld2450_zone_type": "Detection",
        "ld2412_engineering_mode": "on", "ld2412_g03_move_threshold": "55",
        "z2_x1": "-2000", "z2_y1": "800", "z2_x2": "-200", "z2_y2": "2200",
    }))
    assert payload["illuminance"] == 182
    assert payload["ld2450_timeout"] == 12
    assert payload["ld2450_zone_type"] == "Detection"
    assert payload["ld2412_engineering_mode"] == "ON"
    assert payload["ld2412_g03_move_threshold"] == 55
    assert payload[ee.ZONE_CATEGORY] == {"area2": {"x_min": -200, "x_max": -20, "y_min": 80, "y_max": 220}}


def test_live_readings_for_each_radar():
    live = ee.build_payload(reader({
        "ld2450_presence": "on", "ld2412_presence": "on", "ld2412_moving": "on", "ld2412_still": "off",
        "ld2412_moving_distance": ("187", "cm"), "ld2412_still_distance": ("unknown", "cm"),
        "ld2412_move_energy": ("64.0", "%"), "t1_x": "100",
        **{f"g{g}_move": str(g * 5) for g in range(ee.GATE_COUNT)},
    }))["esphome"]
    assert live["ld2450_presence"] is True and live["ld2412_still"] is False
    assert live["ld2412_moving_distance"] == 187
    assert live["ld2412_still_distance"] is None          # no still target: the page drops its arc
    assert live["ld2412_move_energy"] == 64
    assert live["gate_move"] == [g * 5 for g in range(ee.GATE_COUNT)]
    assert "gate_still" not in live
    assert live["available"] is True


def test_a_sensor_that_dropped_off_the_network_is_unavailable():
    payload = ee.build_payload(reader({"t1_x": "unavailable", "t1_y": "unavailable", "ld2450_presence": "unavailable"}))
    assert payload["esphome"]["available"] is False
    assert "occupancy" not in payload


def test_only_what_changed_is_sent():
    read = reader({"ld2450_presence": "on", "ld2412_presence": "off", "illuminance": "50",
                   "ld2450_timeout": "5", "z1_count": "1", "t1_x": "100", "t1_y": "200"})
    assert ee.build_payload(read, {"illuminance"}) == {"illuminance": 50}
    assert ee.build_payload(read, {"ld2412_presence"}) == {"occupancy": True, "esphome": {"ld2412_presence": False}}
    assert ee.build_payload(read, {"z1_count"}) == {"mmwave_area1_occupancy": True}
    assert ee.build_payload(read, {"t1_speed"}) == {"esphome": {"available": True}}
    assert ee.build_payload(read, {"ld2450_firmware"}) == {}


def test_history_state_takes_occupancy_and_zones():
    assert ee.history_state({"occupancy": True, "mmwave_area2_occupancy": False, "illuminance": 5}) == {
        "occupancy": True, "area2": False}
    assert ee.history_state({"ld2450_timeout": 5}) == {}


# ===========================================================================
# Settings catalogue
# ===========================================================================

def test_catalogue_matches_the_shape_the_config_tab_reads():
    groups = {g["id"] for g in ee.CATALOG["groups"]}
    keys = [s["key"] for s in ee.CATALOG["settings"]]
    assert len(keys) == len(set(keys))
    for s in ee.CATALOG["settings"]:
        assert s["group"] in groups, s["key"]
        assert s["title"] and s["help"], s["key"]
        assert s["control"] in ("number", "select", "toggle"), s["key"]
        assert s["entity"].split(".")[0] in ("number", "select", "switch"), s["key"]
        if s["control"] == "number":
            assert isinstance(s["min"], int) and isinstance(s["max"], int) and s["min"] < s["max"], s["key"]
        else:
            assert len(s["options"]) >= 2, s["key"]
        if s["control"] == "toggle":
            assert s["on"] == "ON" and {o["value"] for o in s["options"]} == {"ON", "OFF"}, s["key"]
        if "default" in s and s.get("options"):
            assert s["default"] in [o["value"] for o in s["options"]], s["key"]


def test_every_gate_has_both_thresholds():
    for g in range(ee.GATE_COUNT):
        for kind in ("move", "still"):
            s = ee.setting(f"ld2412_g{g:02d}_{kind}_threshold")
            assert s and (s["min"], s["max"]) == (0, 100)


@pytest.mark.parametrize("key,value", [
    ("ld2450_timeout", 30), ("ld2450_timeout", "30"), ("ld2450_timeout", 0), ("ld2450_timeout", 3600),
    ("ld2450_zone_type", "Filter"), ("ld2412_engineering_mode", "ON"), ("ld2412_engineering_mode", "OFF"),
    ("ld2412_g00_move_threshold", 100), ("ld2412_distance_resolution", "0.5m"),
])
def test_validate_accepts(key, value):
    assert ee.validate(key, value) == (True, None)


@pytest.mark.parametrize("key,value", [
    ("ld2450_timeout", 3601), ("ld2450_timeout", -1), ("ld2450_timeout", 2.5), ("ld2450_timeout", True),
    ("ld2450_timeout", "soon"), ("ld2450_zone_type", "detection"), ("ld2450_zone_type", 1),
    ("ld2412_engineering_mode", True), ("ld2412_engineering_mode", "on"),
    ("ld2412_g00_move_threshold", 101), ("mmWaveHoldTime", 30), ("no_such_setting", 1), (None, 1),
])
def test_validate_refuses(key, value):
    ok, error = ee.validate(key, value)
    assert ok is False and error


def test_setting_values_from_entity_states():
    assert ee.setting_from_state("ld2450_timeout", "12.0") == 12
    assert ee.setting_from_state("ld2450_zone_type", "Filter") == "Filter"
    assert ee.setting_from_state("ld2412_engineering_mode", "on") == "ON"
    assert ee.setting_from_state("ld2412_bluetooth", "off") == "OFF"
    for state in (None, "unknown", "unavailable"):
        assert ee.setting_from_state("ld2450_timeout", state) is None
    assert ee.setting_from_state("no_such_setting", "1") is None


def test_service_calls_for_each_kind_of_entity():
    assert ee.service_call("ld2450_timeout", "30") == ("number", "set_value", {"value": 30})
    assert ee.service_call("ld2450_zone_type", "Filter") == ("select", "select_option", {"option": "Filter"})
    assert ee.service_call("ld2412_engineering_mode", "ON") == ("switch", "turn_on", {})
    assert ee.service_call("ld2412_engineering_mode", "OFF") == ("switch", "turn_off", {})


# ===========================================================================
# Device details
# ===========================================================================

def test_info_for_the_config_tab():
    roles, disabled = ee.entity_roles(registry())
    states = {"firmware": "26.3.2.1", "esphome_version": "2026.3.1", "ip_address": "192.168.1.57", "update": "on"}
    attrs = {"update": {"installed_version": "26.3.2.1", "latest_version": "26.4.0.1", "in_progress": False}}
    entry = ee.device_entry({"id": "dev1", "name": "Apollo R_PRO-1", "manufacturer": "ApolloAutomation",
                             "model": "R-PRO-1-ETH", "connections": [["mac", MAC]]})
    info = ee.build_info(entry, roles, disabled, reader(states), lambda role: attrs.get(role))
    assert info["stack"] == "esphome"
    assert info["firmware"] == {"ota": "ESPHome 2026.3.1", "build": "26.3.2.1", "date": None}
    assert info["model"] == "ApolloAutomation R-PRO-1-ETH"
    assert info["ip_address"] == "192.168.1.57"
    assert info["ha_device_id"] == "dev1"
    assert info["update"] == {"entity_id": roles["update"], "installed": "26.3.2.1", "latest": "26.4.0.1",
                              "available": True, "in_progress": False}
    assert info["entities"]["area2"] == roles["z2_count"]
    assert info["entities"]["ld2412_presence"] == roles["ld2412_presence"]
    assert "ld2412_distance_resolution" in info["unsupported_settings"]
    assert "ld2450_timeout" not in info["unsupported_settings"]
    assert "ld2412_moving_distance" in info["disabled_entities"]
    assert info["missing_tracking"] == []
    assert info["mmwave_version"] is None          # the radars' firmware sensors ship disabled


def test_info_says_when_the_position_entities_are_disabled():
    entries = registry()
    for e in entries:
        if e["original_name"] in ("LD2450 Target-1 X", "LD2450 Target-1 Y"):
            e["disabled_by"] = "user"
    roles, disabled = ee.entity_roles(entries)
    assert ee.is_tracker(roles, disabled)          # still listed, so the page can say what to enable
    info = ee.build_info({"ieee": "esphome:x"}, roles, disabled, reader({}), lambda role: None)
    assert info["missing_tracking"] == ["t1_x", "t1_y"]
    assert info["firmware"]["build"] is None
