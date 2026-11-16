"""Stateful evaluation of confirmed Quella UART fields and all raw bytes/bits."""

from __future__ import annotations

from collections import deque
import time

from telemetry_protocol import TelemetryFrame, payload_bytes, sum16_valid


TDS_DISPLAY_UPPER_BOUNDS = (
    0x1F, 0x2A, 0x36, 0x41, 0x4C, 0x58, 0x6E, 0x79, 0x84, 0x8F,
    0x9A, 0xA4, 0xAF, 0xC4, 0xCF, 0xDA, 0xE4, 0xEF, 0xF9, 0xFF,
)


def tds_display_value(raw_value: int) -> int:
    return next(index for index, upper in enumerate(TDS_DISPLAY_UPPER_BOUNDS)
                if raw_value <= upper)


class QuellaState:
    def __init__(self) -> None:
        self.frames: dict[str, dict] = {}
        self.fields: dict[str, dict] = {}
        self._tds_candidate: int | None = None
        self._tds_update_active = False
        self._external_stop_latched = False
        self._external_reset_armed = False
        self._external_raw_available: bool | None = None
        self._flow_samples: deque[tuple[float, int]] = deque(maxlen=64)

    def _set(self, name: str, value, timestamp: float, source: str,
             unit: str = "", raw=None) -> None:
        self.fields[name] = {
            "value": value,
            "available": True,
            "timestamp": timestamp,
            "source": source,
            "unit": unit,
            "raw": raw,
        }

    def _clear(self, name: str, timestamp: float, source: str, unit: str = "") -> None:
        self.fields[name] = {
            "value": None,
            "available": False,
            "timestamp": timestamp,
            "source": source,
            "unit": unit,
            "raw": None,
        }

    def observe(self, frame: TelemetryFrame, received_at: float) -> None:
        valid = sum16_valid(frame)
        payload = payload_bytes(frame)
        key = f"{frame.direction_name}:{frame.type_name}"
        self.frames[key] = {
            "direction": frame.direction_name,
            "frame_type": frame.type_name,
            "received_at": received_at,
            "source_timestamp_us": frame.timestamp_us,
            "checksum_valid": valid,
            "raw_hex": frame.raw.hex(" ").upper(),
            "raw_bytes": list(frame.raw),
            "payload_bytes": list(payload),
        }
        if valid is False:
            return
        if frame.direction_name == "MAIN_TO_DISPLAY":
            if frame.type_name == "A5" and len(payload) == 16:
                self._observe_a5(payload, received_at)
            elif frame.type_name == "E5_B2" and len(payload) == 16:
                self._observe_b2(payload, received_at)
            elif frame.type_name == "E5_E1" and len(payload) == 16:
                self._observe_e1(payload, received_at)
        elif frame.direction_name == "DISPLAY_TO_MAIN" and len(payload) == 16:
            self._observe_aa(frame.type_name, payload, received_at)

    def _observe_a5(self, d: bytes, ts: float) -> None:
        dispensing = bool(d[1] & 0x01)
        current_ml = int.from_bytes(d[3:5], "big")
        total_ml = int.from_bytes(d[5:9], "big")
        requested = self.fields.get("requested_temperature_c", {})
        requested_c = requested.get("value") if requested.get("available") else None
        hot_outlet_valid = dispensing and requested_c is not None and requested_c > 25
        values = (
            ("dispensing_active", dispensing, "", d[1]),
            ("feed_pump_active", dispensing, "", d[1]),
            ("current_dispensed_volume_ml", current_ml, "ml", d[3:5].hex()),
            ("total_dispensed_volume_ml", total_ml, "ml", d[5:9].hex()),
            # This bit toggles cyclically while the appliance is otherwise idle.
            # Keep the observation for reverse engineering, but never interpret
            # it as machine readiness.
            ("a5_d11_bit4_cyclic", bool(d[11] & 0x10), "", d[11]),
            ("flush_active", bool(d[11] & 0x02), "", d[11]),
            ("heater_900w_active", bool(d[11] & 0x40), "", d[11]),
            ("heater_1200w_active", bool(d[11] & 0x20), "", d[11]),
            ("internal_below_low_mirror", bool(d[11] & 0x01), "", d[11]),
            ("internal_full_mirror", not bool(d[11] & 0x04), "", d[11]),
            ("inlet_temperature_c", d[12], "celsius", d[12]),
            ("heating_level_raw", d[13] & 0x0F, "", d[13]),
            ("ac_mains_voltage_v", d[14], "volt", d[14]),
        )
        for name, value, unit, raw in values:
            self._set(name, value, ts, "A5", unit, raw)
        if hot_outlet_valid:
            self._set("outlet_temperature_c", d[2], ts, "A5", "celsius", d[2])
        else:
            self._clear("outlet_temperature_c", ts, "A5 not transmitted", "celsius")
        if dispensing:
            self._flow_samples.append((ts, current_ml))
            while len(self._flow_samples) > 1 and ts - self._flow_samples[0][0] > 3.0:
                self._flow_samples.popleft()
            if len(self._flow_samples) > 1:
                first_t, first_ml = self._flow_samples[0]
                elapsed = ts - first_t
                if elapsed >= 0.5 and current_ml >= first_ml:
                    flow = (current_ml - first_ml) * 60.0 / elapsed
                    self._set("derived_flow_rate_ml_min", flow, ts, "A5 delta", "ml/min")
        else:
            self._flow_samples.clear()

    def _observe_b2(self, d: bytes, ts: float) -> None:
        external_available = not bool(d[4] & 0x01)
        self._external_raw_available = external_available
        if not external_available:
            self._external_stop_latched = True
            self._external_reset_armed = False
        values = (
            ("internal_below_low", bool(d[4] & 0x08), d[4]),
            ("internal_full", not bool(d[4] & 0x20), d[4]),
            ("external_tank_empty", bool(d[4] & 0x01), d[4]),
            ("external_water_available", not bool(d[4] & 0x01), d[4]),
            ("booster_fill_active", bool(d[11] & 0x04), d[11]),
            ("wastewater_valve_active", bool(d[11] & 0x02), d[11]),
            ("tds_update_active", bool(d[11] & 0x10), d[11]),
            ("tds_measurement_active", bool(d[13] & 0x01), d[13]),
            # Booster-pump power. A Shelly cross-check gives approximately
            # 1 raw digit = 1 watt. Feed-pump and heater power are not part of
            # this one-byte value.
            ("booster_pump_power_w", d[14], d[14]),
            ("b2_d14_candidate_raw", d[14], d[14]),
        )
        for name, value, raw in values:
            self._set(name, value, ts, "E5 B2", "", raw)
        self._publish_external_stop(ts, "E5 B2 derived latch")

        update_active = bool(d[11] & 0x10)
        measurement_active = bool(d[13] & 0x01)
        if update_active or measurement_active:
            self._tds_candidate = d[3]
        commit = self._tds_update_active and not update_active
        if commit or (measurement_active and "tds_raw" not in self.fields):
            raw_value = self._tds_candidate if self._tds_candidate is not None else d[3]
            self._set("tds_raw", raw_value, ts, "E5 B2 latch", "raw", d[3])
            self._set("tds_display", tds_display_value(raw_value), ts,
                      "captured threshold map", "display", raw_value)
        self._tds_update_active = update_active

    def _observe_e1(self, d: bytes, ts: float) -> None:
        value = int.from_bytes(d[1:3], "big")
        self._set("feed_pump_parameter_ml_min", value, ts, "E5 E1", "ml/min",
                  d[1:3].hex())

    def _observe_aa(self, frame_type: str, d: bytes, ts: float) -> None:
        if frame_type == "AA00":
            self._set("aa_control_d1", d[1], ts, "AA00", "", d[1])
            self._set("requested_temperature_c", d[2], ts, "AA00", "celsius", d[2])
            self._set("requested_volume_ml", int.from_bytes(d[3:5], "big"), ts,
                      "AA00", "ml", d[3:5].hex())
            if self._external_stop_latched and self._external_raw_available:
                if d[1] == 0x01 and self._appliance_idle():
                    self._external_reset_armed = True
                elif d[1] == 0x00 and self._external_reset_armed and self._appliance_idle():
                    self._external_stop_latched = False
                    self._external_reset_armed = False
                self._publish_external_stop(ts, "AA00 acknowledgement sequence")
        elif frame_type == "AA_A3":
            self._set("requested_process_raw", d[4], ts, "AA A3", "", d[4])
        elif frame_type == "AA_E1":
            self._set("requested_feed_pump_parameter_ml_min",
                      int.from_bytes(d[2:4], "big"), ts, "AA E1", "ml/min",
                      d[2:4].hex())

    def _appliance_idle(self) -> bool:
        for name in (
            "dispensing_active", "booster_fill_active", "flush_active",
            "heater_900w_active", "heater_1200w_active",
        ):
            item = self.fields.get(name, {})
            if item.get("available") and item.get("value") is True:
                return False
        return True

    def _publish_external_stop(self, ts: float, source: str) -> None:
        self._set("external_empty_stop_latched", self._external_stop_latched,
                  ts, source)
        self._set("external_empty_reset_armed", self._external_reset_armed,
                  ts, source)

    def snapshot(self, receiver: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        fields = {}
        for name, value in self.fields.items():
            item = dict(value)
            item["age_seconds"] = max(0.0, now - float(item["timestamp"]))
            fields[name] = item
        frames = {}
        for key, value in self.frames.items():
            item = dict(value)
            item["age_seconds"] = max(0.0, now - float(item["received_at"]))
            frames[key] = item
        return {
            "schema_version": 1,
            "written_at": now,
            "receiver": dict(receiver),
            "fields": fields,
            "frames": frames,
        }
