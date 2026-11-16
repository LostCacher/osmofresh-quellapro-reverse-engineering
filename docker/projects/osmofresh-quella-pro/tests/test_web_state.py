from __future__ import annotations

from web_app.state import build_state, derive_mode


def sample_field(value, timestamp=100.0, unit=""):
    return {"value": value, "available": True, "timestamp": timestamp,
            "source": "test", "unit": unit, "raw": value}


def test_cyclic_a5_bit_never_changes_idle_mode():
    raw = {"receiver": {"last_packet_at": 100.0}, "fields": {
        "a5_d11_bit4_cyclic": sample_field(True),
        "dispensing_active": sample_field(False),
        "booster_fill_active": sample_field(False),
        "flush_active": sample_field(False),
        "heater_900w_active": sample_field(False),
        "heater_1200w_active": sample_field(False),
    }}
    state = build_state(raw, {"available": True, "machine": {}},
                        {"mega_link": True}, now=100.5)

    assert state["machine"]["mode"] == "IDLE"
    assert state["machine"]["a5_d11_bit4_cyclic"] is True


def test_active_modes_have_priority_and_udp_can_be_stale():
    raw = {"receiver": {"last_packet_at": 90.0}, "fields": {
        "dispensing_active": sample_field(True),
    }}
    state = build_state(raw, {}, {}, now=100.0, telemetry_stale_seconds=5)

    assert state["machine"]["mode"] == "DISPENSING"
    assert state["connectivity"]["telemetry"]["state"] == "STALE"


def test_unknown_without_any_source():
    assert derive_mode({}, {}) == "UNKNOWN"


def test_gateway_can_be_online_while_mega_link_is_offline():
    state = build_state({}, {"available": False}, {
        "_gateway_reachable": True, "_http_status": 503, "mega_link": False,
    }, now=100.0)
    assert state["connectivity"]["gateway"]["state"] == "ONLINE"
    assert state["connectivity"]["mega"]["state"] == "OFFLINE"


def test_external_empty_latch_blocks_control_even_after_water_returns():
    raw = {"receiver": {"last_packet_at": 100.0}, "fields": {
        "external_water_available": sample_field(True),
        "external_empty_stop_latched": sample_field(True),
        "external_empty_reset_armed": sample_field(False),
    }}
    state = build_state(raw, {}, {}, now=100.1)

    assert state["tanks"]["external_empty_stop_latched"]["value"] is True
    assert state["errors"]["blocked"] is True
    assert "acknowledgement required" in " ".join(state["errors"]["blocked_reasons"])
