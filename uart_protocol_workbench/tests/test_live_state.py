from protocol.live_state import FIELD_SOURCES, QuellaLiveState


def response(payload: bytes) -> bytes:
    return payload + (sum(payload) & 0xFFFF).to_bytes(2, "big")


def aa(payload: bytes) -> bytes:
    return b"\xAA" + payload + (sum(payload) & 0xFFFF).to_bytes(2, "big")


def a5(current: int = 25, dispensing: bool = True) -> bytes:
    data = bytearray(16)
    data[1] = 1 if dispensing else 0
    data[2] = 23
    data[3:5] = current.to_bytes(2, "big")
    data[5:9] = (0x000428A3 + current).to_bytes(4, "big")
    data[10] = 0x20  # E2 inlet-temperature fault
    data[11] = 0x74  # ready, heater flags, below-high
    data[12] = 19
    data[13] = 0x04
    data[14] = 246
    return response(bytes(data))


def b2(d4: int = 0x28, d11: int = 0x06) -> bytes:
    data = bytearray(16)
    data[4] = d4
    data[11] = d11
    data[14] = 1
    data[15] = 0xB2
    return response(bytes(data))


def test_known_a5_updates_temperatures_voltage_errors_counters_and_flow():
    state = QuellaLiveState()
    control = bytearray(16)
    control[1], control[2], control[3:5] = 0x01, 60, (150).to_bytes(2, "big")
    state.observe_frame("DISPLAY_TO_MAIN", "AA", aa(bytes(control)), 100.0)
    state.observe_frame("MAIN_TO_DISPLAY", "A5", a5(20), 100.0)
    state.observe_frame("MAIN_TO_DISPLAY", "A5", a5(30), 101.0)
    snapshot = state.to_dict(101.0)

    assert snapshot["temperature"]["outlet_c"]["value"] == 23
    assert snapshot["temperature"]["inlet_c"]["value"] == 19
    assert snapshot["temperature"]["requested_c"]["value"] == 60
    assert snapshot["voltage"]["ac_mains_v"]["value"] == 246
    assert snapshot["dispense"]["current_volume_ml"]["value"] == 30
    assert snapshot["dispense"]["remaining_volume_ml"]["value"] == 120
    assert snapshot["dispense"]["progress_percent"]["value"] == 20.0
    assert snapshot["dispense"]["flow_rate_ml_min"]["value"] == 600.0
    assert snapshot["statistics"]["total_dispensed_ml"]["value"] == 0x000428A3 + 30
    assert snapshot["machine"]["error_codes"]["value"] == ["E2"]
    assert snapshot["raw_protocol"]["A5"]["checksum_valid"] is True
    assert snapshot["raw_protocol"]["A5"]["decoded"]["internal_min_mirror"] is False
    assert snapshot["raw_protocol"]["A5"]["decoded"]["internal_max_mirror"] is False


def test_known_b2_updates_tanks_fill_pump_and_valve_with_audited_polarity():
    state = QuellaLiveState()
    state.observe_frame("MAIN_TO_DISPLAY", "E5 B2", b2(), 10.0)
    values = state.to_dict(10.0)

    assert values["tanks"]["internal_min"]["value"] is True
    assert values["tanks"]["internal_max"]["value"] is False
    assert values["tanks"]["external_low"]["value"] is False
    assert values["water_quality"]["tds"]["known"] is False
    assert values["tanks"]["external_water_raw"]["value"] is True
    assert values["machine"]["filling_active"]["value"] is True
    assert values["machine"]["booster_pump_active"]["value"] is True
    assert values["machine"]["wastewater_flush_valve"]["value"] is True


def test_e1_aa_a3_and_tank_events_update_shared_state():
    state = QuellaLiveState()
    e1 = bytearray(16)
    e1[1:3], e1[15] = (750).to_bytes(2, "big"), 0xE1
    state.observe_frame("MAIN_TO_DISPLAY", "E5 E1", response(bytes(e1)), 20.0)
    a3 = bytearray(16)
    a3[4], a3[15] = 0x78, 0xA3
    state.observe_frame("DISPLAY_TO_MAIN", "AA", aa(bytes(a3)), 20.1)
    state.observe_bridge_event({
        "event": "TANK_STATUS", "external_state": "AVAILABLE_PENDING",
        "external_stable_available": False, "fill_allowed": False,
        "fill_active": True, "delay_remaining_ms": 7425, "last_update_age_ms": 25,
    }, 20.2)
    snapshot = state.to_dict(20.2)

    assert snapshot["dispense"]["feed_pump_parameter_ml_min"]["value"] == 750
    assert snapshot["raw_protocol"]["A3"]["decoded"]["requested_process"] == "FILTER"
    assert snapshot["tanks"]["external_delay_pending"]["value"] is True
    assert snapshot["tanks"]["external_delay_remaining_ms"]["value"] == 7425
    assert snapshot["tanks"]["external_water_stable"]["value"] is False


def test_stale_to_dict_unknown_fields_and_short_frames_are_safe():
    state = QuellaLiveState()
    state.observe_frame("MAIN_TO_DISPLAY", "A5", a5(), 100.0)
    state.observe_frame("MAIN_TO_DISPLAY", "A5", b"\x00\x01", 101.0)
    state.observe_frame("DISPLAY_TO_MAIN", "UNKNOWN", b"\xDE\xAD", 101.0)

    fresh = state.to_dict(101.0)
    stale = state.to_dict(103.0)
    expired = state.to_dict(106.0)
    assert fresh["temperature"]["inlet_c"]["freshness"] == "fresh"
    assert fresh["temperature"]["inlet_c"]["last_update_timestamp"] == 100.0
    assert stale["temperature"]["inlet_c"]["freshness"] == "stale"
    assert expired["temperature"]["inlet_c"]["freshness"] == "expired"
    assert expired["water_quality"]["tds"]["known"] is False
    assert "external_voltage_v" not in expired["voltage"]
    assert isinstance(expired["field_sources"], list)
    assert next(item for item in FIELD_SOURCES if item.name == "TDS").implemented is True


def test_outlet_temperature_is_available_only_during_active_hot_dispense():
    state = QuellaLiveState()
    hot = bytearray(16)
    hot[1], hot[2], hot[3:5] = 0x01, 60, (150).to_bytes(2, "big")
    state.observe_frame("DISPLAY_TO_MAIN", "AA", aa(bytes(hot)), 10.0)
    state.observe_frame("MAIN_TO_DISPLAY", "A5", a5(dispensing=True), 10.1)
    assert state.to_dict(10.1)["temperature"]["outlet_c"]["value"] == 23

    state.observe_frame("MAIN_TO_DISPLAY", "A5", a5(dispensing=False), 10.2)
    outlet = state.to_dict(10.2)["temperature"]["outlet_c"]
    assert outlet["known"] is False
    assert outlet["value"] is None


def test_feed_pump_is_derived_and_tds_is_latched_by_measurement_pulse():
    state = QuellaLiveState()
    state.observe_frame("MAIN_TO_DISPLAY", "A5", a5(dispensing=True), 20.0)

    def measured_tds(value: int, d11: int, d13: int) -> bytes:
        raw = bytearray(b2())
        raw[3], raw[11], raw[13] = value, d11, d13
        raw[16:] = (sum(raw[:16]) & 0xFFFF).to_bytes(2, "big")
        return bytes(raw)

    state.observe_frame("MAIN_TO_DISPLAY", "E5 B2", measured_tds(0x32, 0x1C, 1), 20.1)
    state.observe_frame("MAIN_TO_DISPLAY", "E5 B2", measured_tds(0x32, 0x0C, 1), 20.2)
    state.observe_frame("MAIN_TO_DISPLAY", "E5 B2", measured_tds(0, 0, 0), 20.3)
    snapshot = state.to_dict(20.3)
    assert snapshot["machine"]["feed_pump_active"]["value"] is True
    assert snapshot["machine"]["feed_pump_active"]["confidence"] == "DERIVED"
    assert snapshot["water_quality"]["tds"]["value"] == 0x32

    state.observe_frame("MAIN_TO_DISPLAY", "E5 B2", measured_tds(0, 0x14, 1), 21.0)
    assert state.to_dict(21.0)["water_quality"]["tds"]["value"] == 0x32
    state.observe_frame("MAIN_TO_DISPLAY", "E5 B2", measured_tds(0, 0x04, 1), 21.1)
    assert state.to_dict(21.1)["water_quality"]["tds"]["value"] == 0


def test_bridge_session_updates_origin_and_separate_targets():
    state = QuellaLiveState()
    state.observe_bridge_event({
        "event": "REMOTE_DISPENSE_START", "session_origin": "MANUAL/REMOTE",
        "target_ml": 50, "elapsed_us": 125_000, "override_active": True,
    }, 50.0)
    snapshot = state.to_dict(50.0)

    assert snapshot["machine"]["session_origin"]["value"] == "MANUAL/REMOTE"
    assert snapshot["machine"]["remote_target_ml"]["value"] == 50
    assert snapshot["machine"]["override_target_ml"]["value"] == 0
    assert snapshot["dispense"]["target_volume_ml"]["value"] == 50

