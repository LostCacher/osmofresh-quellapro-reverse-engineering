from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


ACTIVE_MODES = ("ERROR", "DISPENSING", "FILLING", "FLUSHING", "HEATING")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def field(raw: dict, name: str, now: float, stale_after: float = 5.0) -> dict:
    item = dict(raw.get("fields", {}).get(name, {}))
    timestamp = item.get("timestamp")
    age = None if timestamp is None else max(0.0, now - float(timestamp))
    available = bool(item.get("available")) and item.get("value") is not None
    quality = "unknown" if not available else ("fresh" if age is not None and age <= stale_after else "stale")
    return {
        "value": item.get("value") if available else None,
        "available": available,
        "timestamp": timestamp,
        "age_seconds": age,
        "quality": quality,
        "source": item.get("source"),
        "unit": item.get("unit", ""),
        "raw": item.get("raw"),
    }


def scalar(item: dict | None, default=None):
    return default if not isinstance(item, dict) or item.get("value") is None else item["value"]


def derive_mode(fields: dict[str, dict], esp: dict) -> str:
    machine = esp.get("machine", {}) if isinstance(esp, dict) else {}
    if machine.get("error") or scalar(fields.get("error_active"), False):
        return "ERROR"
    if machine.get("dispensing") or scalar(fields.get("dispensing_active"), False):
        return "DISPENSING"
    if machine.get("filling") or scalar(fields.get("booster_fill_active"), False):
        return "FILLING"
    if machine.get("flushing") or scalar(fields.get("flush_active"), False):
        return "FLUSHING"
    heating = machine.get("heating") or scalar(fields.get("heater_900w_active"), False) or scalar(fields.get("heater_1200w_active"), False)
    if heating:
        return "HEATING"
    if not fields and not esp:
        return "UNKNOWN"
    return "IDLE"


def build_state(raw: dict, esp_status: dict, esp_health: dict, *, now: float | None = None,
                telemetry_stale_seconds: float = 5.0) -> dict:
    now = time.time() if now is None else now
    names = (
        "dispensing_active", "feed_pump_active", "current_dispensed_volume_ml",
        "total_dispensed_volume_ml", "a5_d11_bit4_cyclic", "flush_active",
        "heater_900w_active", "heater_1200w_active", "internal_below_low_mirror",
        "internal_full_mirror", "inlet_temperature_c", "outlet_temperature_c",
        "heating_level_raw", "ac_mains_voltage_v", "internal_below_low", "internal_full",
        "external_tank_empty", "external_water_available", "booster_fill_active",
        "external_empty_stop_latched", "external_empty_reset_armed",
        "wastewater_valve_active", "tds_update_active", "tds_measurement_active",
        "tds_raw", "tds_display", "booster_pump_power_w", "b2_d14_candidate_raw",
        "feed_pump_parameter_ml_min", "requested_temperature_c",
        "requested_volume_ml", "derived_flow_rate_ml_min", "requested_process_raw",
    )
    fields = {name: field(raw, name, now) for name in names}
    receiver = dict(raw.get("receiver", {}))
    last_packet = receiver.get("last_packet_at")
    telemetry_age = None if last_packet is None else max(0.0, now - float(last_packet))
    telemetry_online = telemetry_age is not None and telemetry_age <= telemetry_stale_seconds
    esp_available = bool(esp_status.get("available"))
    gateway_reachable = bool(esp_health.get("_gateway_reachable", esp_available))
    mega_online = bool(esp_health.get("mega_link"))
    mode = derive_mode(fields, esp_status)
    tanks = esp_status.get("tanks", {}) if esp_available else {}
    dispense = esp_status.get("dispense", {}) if esp_available else {}
    runtime = esp_status.get("runtime", {}) if esp_available else {}
    machine = esp_status.get("machine", {}) if esp_available else {}
    error_code = machine.get("error_code_raw")
    blocks = []
    internal_below_low = scalar(fields["internal_below_low"])
    external_available = scalar(fields["external_water_available"])
    fill_active = scalar(fields["booster_fill_active"])
    flush_active = scalar(fields["flush_active"])
    external_stop_latched = scalar(fields["external_empty_stop_latched"], False)
    if internal_below_low is True:
        blocks.append("Internal tank below minimum")
    if external_available is False:
        blocks.append("External tank not available")
    elif external_stop_latched:
        blocks.append("External tank empty stop is latched; acknowledgement required")
    if fill_active is True:
        blocks.append("Fill active")
    if flush_active is True:
        blocks.append("Flush active")
    target_ml = scalar(fields["requested_volume_ml"])
    current_ml = scalar(fields["current_dispensed_volume_ml"])
    remaining_ml = None
    progress_percent = None
    if target_ml is not None and current_ml is not None:
        remaining_ml = max(0, target_ml - current_ml)
        progress_percent = min(100.0, current_ml * 100.0 / target_ml) if target_ml else 0.0
    return {
        "schema_version": 2,
        "server_time": now,
        "connectivity": {
            "gateway": {"state": "ONLINE" if gateway_reachable else "OFFLINE", "online": gateway_reachable,
                        "error": esp_health.get("_error")},
            "mega": {"state": "ONLINE" if mega_online else "OFFLINE", "online": mega_online,
                     "last_seen_ms": esp_health.get("mega_last_seen_ms")},
            "telemetry": {"state": "ONLINE" if telemetry_online else ("STALE" if last_packet else "OFFLINE"),
                          "online": telemetry_online, "age_seconds": telemetry_age,
                          "packet_loss": receiver.get("lost_packets_total", 0),
                          "malformed": receiver.get("malformed_packets_total", 0)},
        },
        "machine": {"mode": mode, "pump": machine.get("pump"), "valve": machine.get("valve"),
                    "dispensing": mode == "DISPENSING", "filling": mode == "FILLING",
                    "flushing": mode == "FLUSHING", "heating": mode == "HEATING",
                    "error": mode == "ERROR", "a5_d11_bit4_cyclic": scalar(fields["a5_d11_bit4_cyclic"]),
                    "session_origin": runtime.get("session_origin"), "override_active": runtime.get("override_active")},
        "tanks": {
            "internal_below_low": fields["internal_below_low"], "internal_full": fields["internal_full"],
            "external_raw_available": fields["external_water_available"],
            "external_empty_stop_latched": fields["external_empty_stop_latched"],
            "external_empty_reset_armed": fields["external_empty_reset_armed"],
            "external_stable_available": {"value": tanks.get("external_stable_available"), "available": tanks.get("external_stable_available") is not None},
            "fill_allowed": {"value": tanks.get("fill_allowed"), "available": tanks.get("fill_allowed") is not None},
            "fill_active": fields["booster_fill_active"],
            "fill_delay_remaining_ms": tanks.get("fill_delay_remaining_ms"),
        },
        "dispense": {
            "target_ml": target_ml,
            "current_ml": current_ml,
            "remaining_ml": remaining_ml, "progress_percent": progress_percent,
            "flow_ml_s": dispense.get("flow_ml_s"), "flow_ml_min": scalar(fields["derived_flow_rate_ml_min"]),
            "requested_temp_c": dispense.get("requested_temp_c", scalar(fields["requested_temperature_c"])),
        },
        "temperatures": {"inlet": fields["inlet_temperature_c"], "outlet": fields["outlet_temperature_c"],
                         "requested": fields["requested_temperature_c"]},
        "water_quality": {"tds_raw": fields["tds_raw"], "tds_display": fields["tds_display"],
                          "measurement_active": fields["tds_measurement_active"]},
        "electrical": {
            "ac_mains_voltage": fields["ac_mains_voltage_v"],
            "booster_pump_power_w": fields["booster_pump_power_w"],
            "b2_d14_candidate_raw": fields["b2_d14_candidate_raw"],
        },
        "counters": {"total_dispensed_ml": fields["total_dispensed_volume_ml"]},
        "states": {name: fields[name] for name in names},
        "errors": {"active": mode == "ERROR", "code_raw": error_code,
                   "blocked": bool(blocks), "blocked_reasons": blocks},
        "debug": {"receiver": receiver, "frames": raw.get("frames", {}), "esp_health": esp_health},
    }
