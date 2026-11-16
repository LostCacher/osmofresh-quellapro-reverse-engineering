"""Structured evidence capture for complete dispense-related UART sequences."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .knowledge import ProtocolKnowledge


CAPTURE_SCENARIOS = {
    "A": "Normal Display dispense 150 ml",
    "B": "Normal Display dispense with AA00 SUPPRESS",
    "C": "Manual Mega dispense 10 ml",
    "D": "REPLACE Display 150 ml -> 10 ml",
}


def is_display_dispense_start(raw: bytes) -> bool:
    """Return true only for a complete checksum-valid-looking AA00 start shape.

    Checksum validity is supplied separately by the parser; this predicate only
    identifies the confirmed command/class offsets without inventing a sequence.
    """
    return (
        len(raw) == 19
        and raw[0] == 0xAA
        and raw[2] == 0x01  # payload Data[1] = DISPENSE START
        and raw[16] == 0x00  # payload Data[15] = AA00 class
    )


def known_fields_for_frame(
    knowledge: ProtocolKnowledge, frame_type: str, raw: bytes
) -> tuple[list[dict[str, Any]], list[str]]:
    if frame_type == "AA" and len(raw) == 19:
        payload = raw[1:17]
        class_id = {
            0x00: "control_setpoint_00",
            0xA3: "system_process_a3",
            0xE1: "system_process_e1",
        }.get(payload[15])
        if class_id is None:
            return [], []
    elif frame_type in ("A5", "E5 B2", "E5 E1") and len(raw) == 18:
        payload = raw[:16]
        class_id = None
    else:
        return [], []

    fields: list[dict[str, Any]] = []
    searchable: list[str] = []
    for offset, value in enumerate(payload):
        try:
            entry = knowledge.field(frame_type, offset, class_id)
        except (KeyError, IndexError):
            continue
        meaning = str(entry.get("meaning", "UNKNOWN"))
        category = str(entry.get("category", "UNKNOWN"))
        if meaning.upper() == "UNKNOWN" and category.upper() == "UNKNOWN":
            continue
        field = {
            "offset": offset,
            "value_hex": f"{value:02X}",
            "value_dec": value,
            "category": category,
            "meaning": meaning,
            "confidence": str(entry.get("confidence", "UNKNOWN")),
        }
        if category == "BITFIELD":
            active_bits = []
            for bit in range(8):
                bit_entry = entry.get("bits", {}).get(str(bit), {})
                bit_meaning = str(bit_entry.get("meaning", "UNKNOWN"))
                if value & (1 << bit) and bit_meaning.upper() != "UNKNOWN":
                    active_bits.append({"bit": bit, "meaning": bit_meaning})
                    searchable.append(bit_meaning.lower())
            if active_bits:
                field["active_known_bits"] = active_bits
        fields.append(field)
        searchable.append(meaning.lower())

    joined = " ".join(searchable)
    tags = []
    for tag, needles in {
        "volume": ("volume", "menge", "dispens"),
        "temperature": ("temper",),
        "flow": ("flow", "durchfluss"),
        "status": ("status", "state", "mode", "command", "set dispense"),
    }.items():
        if any(needle in joined for needle in needles):
            tags.append(tag)
    return fields, tags


class DispenseSequenceCapture:
    def __init__(self, knowledge: ProtocolKnowledge, duration_us: int = 5_000_000) -> None:
        self.knowledge = knowledge
        self.duration_us = duration_us
        self.active = False
        self.scenario = "A"
        self.trigger = "manual"
        self.records: list[dict[str, Any]] = []
        self.frame_count = 0
        self.start_timestamp_us: int | None = None
        self.last_timestamp_us: int | None = None
        self.previous_raw: bytes | None = None

    def start(self, scenario: str, trigger: str, timestamp_us: int | None = None) -> None:
        if scenario not in CAPTURE_SCENARIOS:
            raise ValueError(f"unknown capture scenario {scenario}")
        self.active = True
        self.scenario = scenario
        self.trigger = trigger
        self.records = [{
            "record_type": "marker",
            "marker": "DISPENSE_SEQUENCE_START",
            "scenario": scenario,
            "scenario_description": CAPTURE_SCENARIOS[scenario],
            "trigger": trigger,
            "timestamp_us": timestamp_us,
        }]
        self.frame_count = 0
        self.start_timestamp_us = timestamp_us
        self.last_timestamp_us = None
        self.previous_raw = None

    def stop(self, reason: str = "duration complete") -> None:
        if not self.active:
            return
        self.records.append({
            "record_type": "marker",
            "marker": "DISPENSE_SEQUENCE_END",
            "scenario": self.scenario,
            "frame_count": self.frame_count,
            "reason": reason,
            "timestamp_us": self.last_timestamp_us,
            "elapsed_us": self.elapsed_us,
        })
        self.active = False

    @property
    def elapsed_us(self) -> int:
        if self.start_timestamp_us is None or self.last_timestamp_us is None:
            return 0
        return (self.last_timestamp_us - self.start_timestamp_us) & 0xFFFFFFFF

    def add_frame(
        self,
        timestamp_us: int,
        direction: str,
        frame_type: str,
        raw: bytes,
        checksum_valid: bool | None,
        traffic_source: str = "UART_RX",
    ) -> dict[str, Any] | None:
        if not self.active:
            return None
        if self.start_timestamp_us is None:
            self.start_timestamp_us = timestamp_us
        elapsed = (timestamp_us - self.start_timestamp_us) & 0xFFFFFFFF
        if elapsed > self.duration_us:
            return None
        delta = 0 if self.last_timestamp_us is None else (
            timestamp_us - self.last_timestamp_us
        ) & 0xFFFFFFFF
        fields, tags = known_fields_for_frame(self.knowledge, frame_type, raw)
        self.frame_count += 1
        record = {
            "record_type": "frame",
            "scenario": self.scenario,
            "frame_index": self.frame_count,
            "timestamp_us": timestamp_us,
            "elapsed_us": elapsed,
            "delta_us": delta,
            "direction": direction,
            "traffic_source": traffic_source,
            "raw": raw.hex(" ").upper(),
            "decoded_type": frame_type,
            "checksum_valid": checksum_valid,
            "known_fields": fields,
            "semantic_tags": tags,
            "contains_volume": "volume" in tags,
            "contains_temperature": "temperature" in tags,
            "contains_flow": "flow" in tags,
            "contains_status": "status" in tags,
            "identical_to_previous_frame": self.previous_raw == raw,
        }
        self.records.append(record)
        self.last_timestamp_us = timestamp_us
        self.previous_raw = bytes(raw)
        return record

    def export_jsonl(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in self.records),
            encoding="utf-8",
        )
        return target

    def export_csv(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        columns = (
            "scenario", "frame_index", "timestamp_us", "elapsed_us", "delta_us",
            "direction", "traffic_source", "decoded_type", "checksum_valid", "raw",
            "identical_to_previous_frame", "semantic_tags", "known_fields",
        )
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for record in self.records:
                if record.get("record_type") != "frame":
                    continue
                row = {key: record.get(key, "") for key in columns}
                row["semantic_tags"] = ",".join(record.get("semantic_tags", []))
                row["known_fields"] = json.dumps(record.get("known_fields", []), ensure_ascii=False)
                writer.writerow(row)
        return target
