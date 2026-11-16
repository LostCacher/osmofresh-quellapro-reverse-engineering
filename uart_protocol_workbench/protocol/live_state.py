"""Central, serializable runtime state built only from audited protocol fields."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import time
from typing import Any

from .checksum import validate_aa_frame, validate_response
from .tds_mapping import tds_display_value


@dataclass(frozen=True)
class FieldSource:
    name: str
    frame: str
    direction: str
    offset: str
    rule: str
    unit: str
    confidence: str
    implemented: bool = True


FIELD_SOURCES = (
    FieldSource("Dispensing active", "A5", "MAIN_TO_DISPLAY", "Data[1].bit0", "1=active", "bool", "LOW"),
    FieldSource("Outlet temperature", "A5", "MAIN_TO_DISPLAY", "Data[2]", "valid during active dispense with requested temperature >25 °C", "°C", "CONFIRMED"),
    FieldSource("Feed pump active", "A5", "MAIN_TO_DISPLAY", "Data[1].bit0", "derived from confirmed dispensing-active state", "bool", "DERIVED"),
    FieldSource("Current dispensed volume", "A5", "MAIN_TO_DISPLAY", "Data[3..4]", "big-endian", "ml", "CONFIRMED"),
    FieldSource("Total dispensed amount", "A5", "MAIN_TO_DISPLAY", "Data[5..8]", "big-endian", "ml", "MEDIUM..STRONG"),
    FieldSource("Error flags", "A5", "MAIN_TO_DISPLAY", "Data[9..10]", "known set bits mapped to E-codes", "bitfield", "STRONG"),
    FieldSource("A5 D11.bit4 cyclic", "A5", "MAIN_TO_DISPLAY", "Data[11].bit4", "cyclic observation; meaning unknown", "bool", "UNKNOWN"),
    FieldSource("Base/operating flag", "A5", "MAIN_TO_DISPLAY", "Data[11].bit7", "1=base/operating flag", "bool", "LOW"),
    FieldSource("900 W heater control", "A5", "MAIN_TO_DISPLAY", "Data[11].bit6", "1=active", "bool", "MEDIUM"),
    FieldSource("1200 W heater control", "A5", "MAIN_TO_DISPLAY", "Data[11].bit5", "1=active", "bool", "MEDIUM"),
    FieldSource("Flush active", "A5", "MAIN_TO_DISPLAY", "Data[11].bit1", "1=active", "bool", "STRONG"),
    FieldSource("Internal MIN mirror", "A5", "MAIN_TO_DISPLAY", "Data[11].bit0", "1=below low", "bool", "CONFIRMED"),
    FieldSource("Internal MAX mirror", "A5", "MAIN_TO_DISPLAY", "Data[11].bit2", "0=above high", "bool", "CONFIRMED"),
    FieldSource("Inlet temperature", "A5", "MAIN_TO_DISPLAY", "Data[12]", "unsigned byte", "°C", "CONFIRMED"),
    FieldSource("Heating level", "A5", "MAIN_TO_DISPLAY", "Data[13].bits0..3", "raw heating/control nibble", "raw", "HIGH"),
    FieldSource("AC mains voltage", "A5", "MAIN_TO_DISPLAY", "Data[14]", "unsigned byte", "V", "CONFIRMED"),
    FieldSource("Internal tank MIN", "E5 B2", "MAIN_TO_DISPLAY", "Data[4].bit3", "1=below low", "bool", "CONFIRMED"),
    FieldSource("Internal tank MAX", "E5 B2", "MAIN_TO_DISPLAY", "Data[4].bit5", "0=above high", "bool", "CONFIRMED"),
    FieldSource("External tank LOW", "E5 B2", "MAIN_TO_DISPLAY", "Data[4].bit0", "1=empty", "bool", "CONFIRMED"),
    FieldSource("Booster/fill active", "E5 B2", "MAIN_TO_DISPLAY", "Data[11].bit2", "1=active", "bool", "CONFIRMED"),
    FieldSource("Wastewater flush valve", "E5 B2", "MAIN_TO_DISPLAY", "Data[11].bit1", "1=energized/open", "bool", "CONFIRMED"),
    FieldSource("Booster-pump power", "E5 B2", "MAIN_TO_DISPLAY", "Data[14]", "approximately 1 digit = 1 W; feed pump and heater excluded", "W", "STRONG"),
    FieldSource("Feed-pump control parameter", "E5 E1", "MAIN_TO_DISPLAY", "Data[1..2]", "big-endian", "ml/min", "MEDIUM"),
    FieldSource("AA dispense command", "AA00", "DISPLAY_TO_MAIN", "Data[1]", "00 abort, 01 start, 80 idle/refresh", "enum", "CONFIRMED"),
    FieldSource("Requested temperature", "AA00", "DISPLAY_TO_MAIN", "Data[2]", "unsigned byte", "°C", "CONFIRMED"),
    FieldSource("Requested volume", "AA00", "DISPLAY_TO_MAIN", "Data[3..4]", "big-endian", "ml", "CONFIRMED"),
    FieldSource("Requested process", "AA A3", "DISPLAY_TO_MAIN", "Data[4]", "68 base, 78 filter, 7C flush", "enum", "UNKNOWN"),
    FieldSource("Requested feed-pump parameter", "AA E1", "DISPLAY_TO_MAIN", "Data[2..3]", "big-endian", "ml/min", "CONFIRMED"),
    FieldSource("TDS", "E5 B2", "MAIN_TO_DISPLAY", "Data[3], D11.bit4, D13.bit0", "D3 is latched on the falling edge of D11.bit4; display value uses the captured 20-range threshold table", "raw / display", "CONFIRMED"),
)


@dataclass
class LiveValue:
    value: Any = None
    known: bool = False
    timestamp: float | None = None
    source_frame: str = "—"
    raw: Any = None
    unit: str = ""
    confidence: str = "UNKNOWN"
    note: str = ""
    fresh_seconds: float = 2.0
    expire_seconds: float = 5.0

    def set(self, value: Any, timestamp: float, source: str, *, raw: Any = None) -> None:
        self.value, self.known, self.timestamp = value, True, timestamp
        self.source_frame, self.raw = source, raw

    def clear(self, source: str = "—", *, note: str | None = None) -> None:
        self.value, self.known, self.timestamp = None, False, None
        self.source_frame, self.raw = source, None
        if note is not None:
            self.note = note

    def snapshot(self, now: float) -> dict[str, Any]:
        age = None if self.timestamp is None else max(0.0, now - self.timestamp)
        freshness = "unknown"
        if self.known and age is not None:
            freshness = "fresh" if age < self.fresh_seconds else ("stale" if age <= self.expire_seconds else "expired")
        return {
            "value": self.value, "known": self.known, "last_update_timestamp": self.timestamp,
            "age_ms": None if age is None else round(age * 1000),
            "freshness": freshness, "source_frame": self.source_frame, "raw": self.raw,
            "unit": self.unit, "confidence": self.confidence, "note": self.note,
        }


@dataclass
class RawFrameState:
    raw: bytes = b""
    timestamp: float | None = None
    direction: str = "—"
    checksum_valid: bool | None = None
    decoded: dict[str, Any] | None = None

    def snapshot(self, now: float) -> dict[str, Any]:
        age = None if self.timestamp is None else max(0.0, now - self.timestamp)
        return {
            "timestamp": self.timestamp, "age_ms": None if age is None else round(age * 1000),
            "raw": self.raw.hex(" ").upper(), "direction": self.direction,
            "checksum_valid": self.checksum_valid, "decoded": self.decoded or {},
            "freshness": "unknown" if age is None else ("fresh" if age < 2 else ("stale" if age <= 5 else "expired")),
        }


class QuellaLiveState:
    """Frame-driven state. GUI reads snapshots; frame parsing never touches widgets."""

    GROUP_FIELDS = {
        "machine": ("overall_mode", "requested_process", "a5_d11_bit4_cyclic", "dispensing_active", "filling_active", "flushing_active",
                    "heating_active", "heater_900w", "heater_1200w", "booster_pump_active",
                    "feed_pump_active", "wastewater_flush_valve",
                    "error_state", "error_codes", "communication_state",
                    "session_origin", "override_target_ml", "remote_target_ml"),
        "tanks": ("internal_min", "internal_max", "external_low", "external_water_raw",
                  "external_water_stable", "external_stop_latched", "external_reset_armed",
                  "external_delay_pending", "external_delay_remaining_ms",
                  "fill_allowed", "fill_active", "last_b2_age_ms"),
        "dispense": ("requested_volume_ml", "current_volume_ml", "target_volume_ml", "remaining_volume_ml",
                     "flow_rate_ml_min", "progress_percent", "elapsed_seconds",
                     "aa_control_d1", "a5_dispensing", "feed_pump_parameter_ml_min"),
        "temperature": ("inlet_c", "outlet_c", "requested_c", "heating_level_raw"),
        "water_quality": ("tds", "tds_calculated"),
        "voltage": ("ac_mains_v", "booster_pump_power_w"),
        "statistics": ("total_dispensed_ml", "current_amount_counter_ml"),
        "unknown": ("water_quality_state", "filter_counter",
                    "flow_pulse_counter", "secondary_temperature_raw", "fill_counter", "flush_counter",
                    "pump_runtime", "valve_runtime", "dispense_count", "lifetime_counter",
                    "error_counter", "other_valve_states"),
    }

    def __init__(self) -> None:
        self.fields = {name: LiveValue() for names in self.GROUP_FIELDS.values() for name in names}
        self.raw_frames = {name: RawFrameState() for name in ("A3", "A5", "E5 B2", "E5 E1", "AA00", "CC")}
        self._flow_samples: deque[tuple[float, int]] = deque(maxlen=64)
        self._dispense_started_at: float | None = None
        self._tds_candidate: int | None = None
        self._tds_update_active = False
        self._set_metadata()

    def _set_metadata(self) -> None:
        meta = {
            "inlet_c": ("°C", "CONFIRMED"), "outlet_c": ("°C", "CONFIRMED"),
            "requested_c": ("°C", "CONFIRMED"), "current_volume_ml": ("ml", "CONFIRMED"),
            "requested_volume_ml": ("ml", "CONFIRMED"), "target_volume_ml": ("ml", "CONFIRMED"),
            "remaining_volume_ml": ("ml", "DERIVED"), "flow_rate_ml_min": ("ml/min", "DERIVED"),
            "feed_pump_parameter_ml_min": ("ml/min", "MEDIUM"), "feed_pump_active": ("", "DERIVED"),
            "ac_mains_v": ("V", "CONFIRMED"),
            "booster_pump_power_w": ("W", "STRONG"), "tds": ("raw", "CONFIRMED"),
            "tds_calculated": ("display", "CONFIRMED"),
            "total_dispensed_ml": ("ml", "MEDIUM..STRONG"), "progress_percent": ("%", "DERIVED"),
            "elapsed_seconds": ("s", "DERIVED"),
            "last_b2_age_ms": ("ms", "DERIVED"),
        }
        for name, (unit, confidence) in meta.items():
            self.fields[name].unit, self.fields[name].confidence = unit, confidence
        for name in ("flow_pulse_counter",
                     "fill_counter", "flush_counter", "pump_runtime", "valve_runtime", "dispense_count",
                     "lifetime_counter", "error_counter", "secondary_temperature_raw", "filter_counter",
                     "water_quality_state", "other_valve_states"):
            self.fields[name].note = "No audited UART mapping available"
        self.fields["tds"].note = "Latched E5 B2 Data[3] raw value"
        self.fields["tds_calculated"].note = "Value calculated from the captured original-display threshold mapping"
        self.fields["feed_pump_active"].note = "Derived from confirmed A5 dispensing-active state"
        self.fields["outlet_c"].note = "Available only during active hot-water dispensing (requested temperature > 25 °C)"
        self.fields["flow_rate_ml_min"].note = "Derived from a rolling A5 Δvolume/Δtime window; not a direct flow sensor field"
        self.fields["booster_pump_power_w"].note = (
            "E5 B2 Data[14]; booster-pump power. Shelly cross-check supports "
            "approximately 1 digit = 1 W; feed-pump and heater power are excluded."
        )

    def _set(self, name: str, value: Any, timestamp: float, source: str, raw: Any = None) -> None:
        self.fields[name].set(value, timestamp, source, raw=raw)

    def _clear(self, name: str, source: str, note: str | None = None) -> None:
        self.fields[name].clear(source, note=note)

    def observe_frame(self, direction: str, frame_type: str, raw: bytes, timestamp: float) -> None:
        data = bytes(raw)
        if frame_type in ("A5", "E5 B2", "E5 E1"):
            valid = validate_response(data)
        elif frame_type == "AA":
            valid = validate_aa_frame(data)
        elif frame_type == "CC":
            valid = data == b"\xCC"
        else:
            return
        self._set("communication_state", "OK" if valid else "CHECKSUM ERROR", timestamp, frame_type, data.hex(" ").upper())
        if frame_type == "CC":
            self.raw_frames["CC"] = RawFrameState(data, timestamp, direction, valid, {"ack": True})
            return
        raw_key = frame_type
        if frame_type == "AA" and len(data) == 19:
            raw_key = "A3" if data[16] == 0xA3 else ("AA00" if data[16] == 0x00 else "")
        if raw_key in self.raw_frames:
            self.raw_frames[raw_key] = RawFrameState(data, timestamp, direction, valid, {})
        if not valid:
            return
        if frame_type == "A5":
            self._observe_a5(direction, data, timestamp)
        elif frame_type == "E5 B2":
            self._observe_b2(direction, data, timestamp)
        elif frame_type == "E5 E1":
            self._observe_e1(direction, data, timestamp)
        elif frame_type == "AA":
            self._observe_aa(direction, data, timestamp)
        self._update_derived(timestamp)

    def _observe_a5(self, direction: str, d: bytes, ts: float) -> None:
        dispensing = bool(d[1] & 0x01)
        current = int.from_bytes(d[3:5], "big")
        total = int.from_bytes(d[5:9], "big")
        errors = []
        for offset, mapping in ((9, {6: "E0", 5: "E6", 0: "E8"}),
                                (10, {7: "E9", 6: "E7", 5: "E2", 4: "E5", 3: "E4", 2: "EA", 0: "E3"})):
            errors.extend(code for bit, code in mapping.items() if d[offset] & (1 << bit))
        heating_level = d[13] & 0x0F
        requested_c = self.fields["requested_c"].value if self.fields["requested_c"].known else None
        hot_outlet_valid = dispensing and requested_c is not None and requested_c > 25
        decoded = {"dispensing": dispensing, "outlet_c": d[2] if hot_outlet_valid else None, "current_ml": current,
                   "total_ml": total, "errors": errors, "base_operating_flag": bool(d[11] & 0x80),
                   "internal_min_mirror": bool(d[11] & 0x01),
                   "internal_max_mirror": not bool(d[11] & 0x04),
                   "inlet_c": d[12], "ac_mains_v": d[14]}
        self.raw_frames["A5"] = RawFrameState(d, ts, direction, True, decoded)
        for name, value, raw in (
            ("dispensing_active", dispensing, d[1]), ("a5_dispensing", dispensing, d[1]),
            ("feed_pump_active", dispensing, d[1]),
            ("current_volume_ml", current, d[3:5].hex(" ").upper()),
            ("current_amount_counter_ml", current, d[3:5].hex(" ").upper()),
            ("total_dispensed_ml", total, d[5:9].hex(" ").upper()),
            ("a5_d11_bit4_cyclic", bool(d[11] & 0x10), d[11]),
            ("flushing_active", bool(d[11] & 0x02), d[11]), ("heater_900w", bool(d[11] & 0x40), d[11]),
            ("heater_1200w", bool(d[11] & 0x20), d[11]),
            ("heating_active", bool((d[11] & 0x60) or heating_level), d[11:14].hex(" ").upper()),
            ("heating_level_raw", heating_level, d[13]), ("inlet_c", d[12], d[12]),
            ("ac_mains_v", d[14], d[14]), ("error_state", bool(errors), d[9:11].hex(" ").upper()),
            ("error_codes", errors, d[9:11].hex(" ").upper()),
        ):
            self._set(name, value, ts, "A5", raw)
        if hot_outlet_valid:
            self._set("outlet_c", d[2], ts, "A5", d[2])
        else:
            self._clear("outlet_c", "A5 (not currently transmitted)")
        if dispensing:
            if self._dispense_started_at is None:
                self._dispense_started_at = ts
                self._flow_samples.clear()
            self._flow_samples.append((ts, current))
        elif self._dispense_started_at is not None:
            self._dispense_started_at = None
            self._flow_samples.clear()

    def _observe_b2(self, direction: str, d: bytes, ts: float) -> None:
        internal_min = bool(d[4] & 0x08)
        internal_max = not bool(d[4] & 0x20)
        external_low = bool(d[4] & 0x01)
        fill = bool(d[11] & 0x04)
        valve = bool(d[11] & 0x02)
        tds_update = bool(d[11] & 0x10)
        tds_measurement = bool(d[13] & 0x01)
        if tds_update or tds_measurement:
            self._tds_candidate = d[3]
        tds_commit = self._tds_update_active and not tds_update
        if tds_commit or (tds_measurement and not self.fields["tds"].known):
            tds_raw = self._tds_candidate if self._tds_candidate is not None else d[3]
            self._set("tds", tds_raw, ts, "E5 B2 TDS latch", d[3])
            self._set("tds_calculated", tds_display_value(tds_raw), ts, "Captured TDS thresholds", tds_raw)
        self._tds_update_active = tds_update
        decoded = {"internal_min": internal_min, "internal_max": internal_max,
                   "tds_data": d[3], "tds_update_pulse": tds_update,
                   "tds_measurement_active": tds_measurement,
                   "tds_latched": self.fields["tds"].value if self.fields["tds"].known else None,
                   "external_low": external_low, "fill_active": fill,
                   "wastewater_flush_valve": valve, "booster_pump_power_w": d[14]}
        self.raw_frames["E5 B2"] = RawFrameState(d, ts, direction, True, decoded)
        for name, value, raw in (
            ("internal_min", internal_min, d[4]), ("internal_max", internal_max, d[4]),
            ("booster_pump_power_w", d[14], d[14]),
            ("external_low", external_low, d[4]), ("external_water_raw", not external_low, d[4]),
            ("filling_active", fill, d[11]), ("fill_active", fill, d[11]),
            ("booster_pump_active", fill, d[11]), ("wastewater_flush_valve", valve, d[11]),
        ):
            self._set(name, value, ts, "E5 B2", raw)
        self._set("last_b2_age_ms", 0, ts, "E5 B2", d.hex(" ").upper())

    def _observe_e1(self, direction: str, d: bytes, ts: float) -> None:
        parameter = int.from_bytes(d[1:3], "big")
        decoded = {"feed_pump_parameter_ml_min": parameter}
        self.raw_frames["E5 E1"] = RawFrameState(d, ts, direction, True, decoded)
        self._set("feed_pump_parameter_ml_min", parameter, ts, "E5 E1", d[1:3].hex(" ").upper())

    def _observe_aa(self, direction: str, d: bytes, ts: float) -> None:
        payload_class = d[16]
        if payload_class == 0x00:
            command, requested_c, volume = d[2], d[3], int.from_bytes(d[4:6], "big")
            decoded = {"control_d1": command, "requested_c": requested_c, "requested_volume_ml": volume}
            self.raw_frames["AA00"] = RawFrameState(d, ts, direction, True, decoded)
            self._set("aa_control_d1", f"0x{command:02X}", ts, "AA00", command)
            self._set("requested_c", requested_c, ts, "AA00", requested_c)
            self._set("requested_volume_ml", volume, ts, "AA00", d[4:6].hex(" ").upper())
        elif payload_class == 0xA3:
            process = {0x68: "BASE", 0x78: "FILTER", 0x7C: "FLUSH"}.get(d[5], f"RAW 0x{d[5]:02X}")
            self.raw_frames["A3"] = RawFrameState(d, ts, direction, True, {"requested_process": process})
            self._set("requested_process", process, ts, "AA A3", d[5])
        elif payload_class == 0xE1:
            requested = int.from_bytes(d[3:5], "big")
            self._set("feed_pump_parameter_ml_min", requested, ts, "AA E1", d[3:5].hex(" ").upper())

    def observe_bridge_event(self, event: dict[str, Any], timestamp: float) -> None:
        kind = str(event.get("event", ""))
        if kind.startswith("DISPENSE_") or kind.startswith("REMOTE_DISPENSE_"):
            origin = str(event.get("session_origin", "NONE"))
            target = int(event.get("target_ml", 0))
            if not bool(event.get("override_active", False)) and not bool(event.get("aborting", False)):
                origin = "NONE"
            self._set("session_origin", origin, timestamp, "Mega session")
            self._set("override_target_ml", target if origin == "DISPLAY_OVERRIDE" else 0, timestamp, "Mega session")
            self._set("remote_target_ml", target if origin == "MANUAL/REMOTE" else 0, timestamp, "Mega session")
            if target:
                self._set("target_volume_ml", target, timestamp, "Mega session")
            elapsed = int(event.get("elapsed_us", 0)) / 1_000_000.0
            self._set("elapsed_seconds", elapsed, timestamp, "Mega session")
        if kind == "TANK_STATUS" or kind.startswith(("EXTERNAL_", "INTERNAL_TANK_", "FILL_STATE_")):
            for name, key in (
                ("external_water_stable", "external_stable_available"),
                ("external_stop_latched", "external_stop_latched"),
                ("external_reset_armed", "external_reset_armed"),
                ("fill_allowed", "fill_allowed"), ("fill_active", "fill_active"),
            ):
                if key in event:
                    self._set(name, bool(event[key]), timestamp, "Mega tank state")
            state = str(event.get("external_state", "UNKNOWN"))
            self._set("external_delay_pending", state == "AVAILABLE_PENDING", timestamp, "Mega tank state")
            self._set("external_delay_remaining_ms", int(event.get("delay_remaining_ms", 0)), timestamp, "Mega tank state")
            age = int(event.get("last_update_age_ms", 0xFFFFFFFF))
            if age != 0xFFFFFFFF:
                self._set("last_b2_age_ms", age, timestamp, "Mega tank state")

    def _update_derived(self, timestamp: float) -> None:
        def value(name: str):
            field = self.fields[name]
            return field.value if field.known else None

        def recent(name: str):
            field = self.fields[name]
            return field.value if field.known and field.timestamp is not None and timestamp - field.timestamp <= field.expire_seconds else None

        mode = "ERROR" if recent("error_state") else (
            "DISPENSING" if recent("dispensing_active") else (
                "FILL" if recent("filling_active") else (
                    "FLUSH" if recent("flushing_active") else ("HEATING" if recent("heating_active") else "IDLE")
                )
            )
        )
        self._set("overall_mode", mode, timestamp, "derived")
        requested = value("requested_volume_ml")
        session_target = value("target_volume_ml")
        target = session_target or requested
        current = value("current_volume_ml")
        if target is not None:
            self._set("target_volume_ml", target, timestamp, "derived")
        if target is not None and current is not None:
            self._set("remaining_volume_ml", max(0, target - current), timestamp, "derived")
            self._set("progress_percent", min(100.0, (100.0 * current / target) if target else 0.0), timestamp, "derived")
        if self._dispense_started_at is not None:
            self._set("elapsed_seconds", max(0.0, timestamp - self._dispense_started_at), timestamp, "derived")
        while len(self._flow_samples) > 1 and timestamp - self._flow_samples[0][0] > 3.0:
            self._flow_samples.popleft()
        if len(self._flow_samples) > 1:
            first_t, first_ml = self._flow_samples[0]
            last_t, last_ml = self._flow_samples[-1]
            if last_t - first_t >= 0.5 and last_ml >= first_ml:
                self._set("flow_rate_ml_min", (last_ml - first_ml) * 60.0 / (last_t - first_t), timestamp, "A5 derived", "Δvolume/Δtime")

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        result = {
            group: {name: self.fields[name].snapshot(now) for name in names}
            for group, names in self.GROUP_FIELDS.items()
        }
        b2 = self.raw_frames["E5 B2"]
        if b2.timestamp is not None:
            age_ms = round(max(0.0, now - b2.timestamp) * 1000)
            result["tanks"]["last_b2_age_ms"].update(
                value=age_ms, known=True, age_ms=age_ms,
                freshness="fresh" if age_ms < 2000 else ("stale" if age_ms <= 5000 else "expired"),
                source_frame="E5 B2", raw=None, unit="ms", confidence="DERIVED",
            )
        result["raw_protocol"] = {name: frame.snapshot(now) for name, frame in self.raw_frames.items()}
        result["field_sources"] = [asdict(item) for item in FIELD_SOURCES]
        return result
