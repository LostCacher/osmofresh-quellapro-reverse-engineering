"""Editable response frame model and known-field metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .checksum import sum16_bytes, validate_response
from .framing import frame_layout


class Dataset(str, Enum):
    A5 = "A5"
    E5_B2 = "E5 B2"
    E5_E1 = "E5 E1"


DEFAULT_FRAME_HEX = {
    Dataset.A5: "00 00 19 00 96 00 03 B8 7A 00 00 94 19 00 F9 00 03 8A",
    Dataset.E5_B2: "00 00 00 00 20 0C 00 00 00 00 00 00 00 00 00 B2 00 DE",
    Dataset.E5_E1: "00 02 EE 35 00 00 00 00 00 00 00 00 00 08 CA E1 02 D8",
}

DEFAULT_RESPONSE_CONFIG = {
    Dataset.A5: {
        "selected_state": "observed_state_a",
        "payload_states": [
            {
                "id": "observed_state_a",
                "name": "Observed payload state A",
                "response": DEFAULT_FRAME_HEX[Dataset.A5],
            }
        ],
    },
    Dataset.E5_B2: {
        "selected_state": "observed_state_a",
        "payload_states": [
            {
                "id": "observed_state_a",
                "name": "Observed payload state A",
                "response": DEFAULT_FRAME_HEX[Dataset.E5_B2],
            },
            {
                "id": "observed_state_b",
                "name": "Observed payload state B — discriminator unknown",
                "response": "00 00 00 49 00 0C 00 00 00 00 00 14 00 01 1C B2 01 38",
            },
        ],
    },
    Dataset.E5_E1: {
        "selected_state": "observed_state_a",
        "payload_states": [
            {
                "id": "observed_state_a",
                "name": "Observed payload state A",
                "response": DEFAULT_FRAME_HEX[Dataset.E5_E1],
            }
        ],
    },
}


def load_default_frame_hex() -> dict[Dataset, str]:
    """Load user-editable defaults, falling back to built-in known-good values."""
    path = Path(__file__).resolve().parent.parent / "config" / "default_frames.json"
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        result = {}
        for dataset in Dataset:
            value = values[dataset.value]
            if isinstance(value, str):
                result[dataset] = value
            else:
                states = value.get("payload_states", value.get("variants", []))
                result[dataset] = str(states[0]["response"])
        return result
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return dict(DEFAULT_FRAME_HEX)


def parse_hex_bytes(text: str, expected_length: int | None = None) -> bytes:
    tokens = text.replace(",", " ").split()
    try:
        value = bytes(int(token, 16) for token in tokens)
    except ValueError as exc:
        raise ValueError("use two-digit hexadecimal bytes separated by spaces") from exc
    if any(len(token.removeprefix("0x")) > 2 for token in tokens):
        raise ValueError("hex values must be between 00 and FF")
    if expected_length is not None and len(value) != expected_length:
        raise ValueError(f"expected {expected_length} bytes, got {len(value)}")
    return value


@dataclass
class ResponseFrame:
    dataset: Dataset
    data: bytearray
    checksum_high: int
    checksum_low: int
    auto_checksum: bool = True

    @classmethod
    def from_hex(cls, dataset: Dataset, text: str) -> "ResponseFrame":
        raw = parse_hex_bytes(text, 18)
        layout = frame_layout(dataset.value, len(raw))
        if layout is None:
            raise ValueError(f"unsupported complete frame layout for {dataset.value}")
        payload = raw[
            layout.payload_start : layout.payload_start + layout.payload_length
        ]
        return cls(
            dataset,
            bytearray(payload),
            raw[layout.checksum_high_offset],
            raw[layout.checksum_low_offset],
        )

    @classmethod
    def default(cls, dataset: Dataset) -> "ResponseFrame":
        return cls.from_hex(dataset, load_default_frame_hex()[dataset])

    def set_data(self, offset: int, value: int) -> None:
        if not 0 <= offset < 16 or not 0 <= value <= 0xFF:
            raise ValueError("invalid frame offset or byte value")
        self.data[offset] = value
        if self.auto_checksum:
            self.recalculate()

    def recalculate(self) -> None:
        self.checksum_high, self.checksum_low = sum16_bytes(self.data)

    def to_bytes(self) -> bytes:
        return bytes(self.data) + bytes((self.checksum_high, self.checksum_low))

    def is_valid(self) -> bool:
        return validate_response(self.to_bytes())

    def response_id_matches_request(self) -> bool | None:
        """Check the captured E5 Data[15] observation; A5 remains unknown."""
        expected = {
            Dataset.E5_B2: 0xB2,
            Dataset.E5_E1: 0xE1,
        }.get(self.dataset)
        return None if expected is None else self.data[15] == expected

    def to_hex(self) -> str:
        return self.to_bytes().hex(" ").upper()


@dataclass
class ResponsePayloadState:
    frame: ResponseFrame
    state_id: str
    name: str = ""
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.state_id,
            "name": self.name or self.state_id,
            "enabled": self.enabled,
            "response": self.frame.to_hex(),
        }


@dataclass
class RequestResponseSet:
    dataset: Dataset
    payload_states: list[ResponsePayloadState]
    selected_state: str | None = None
    shared_data: dict[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.payload_states:
            raise ValueError(
                f"{self.dataset.value} requires at least one payload state"
            )
        identifiers = [state.state_id for state in self.payload_states]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"duplicate payload state ID in {self.dataset.value}")
        self.shared_data = {
            int(offset): int(value) for offset, value in self.shared_data.items()
        }
        if any(
            not 0 <= offset < 16 or not 0 <= value <= 0xFF
            for offset, value in self.shared_data.items()
        ):
            raise ValueError("shared Data offsets and values must be bytes")
        if self.selected_state not in identifiers:
            self.selected_state = identifiers[0]
        self._normalize_shared_backing_values()

    @classmethod
    def from_config(cls, dataset: Dataset, value: object) -> "RequestResponseSet":
        if isinstance(value, str):
            frame = ResponseFrame.from_hex(dataset, value)
            return cls(
                dataset,
                [
                    ResponsePayloadState(
                        frame, "observed_state_a", "Observed payload state A"
                    )
                ],
            )
        if not isinstance(value, dict):
            raise ValueError(f"invalid response configuration for {dataset.value}")
        raw_states = value.get("payload_states", value.get("variants", []))
        if not isinstance(raw_states, list) or not raw_states:
            raise ValueError(f"{dataset.value}.payload_states must be a non-empty list")
        states = []
        for index, item in enumerate(raw_states):
            if not isinstance(item, dict):
                raise ValueError(f"invalid {dataset.value} payload state")
            frame = ResponseFrame.from_hex(dataset, str(item["response"]))
            state_id = str(item.get("id", f"observed_state_{chr(97 + index)}"))
            old_name = str(item.get("name", ""))
            if "Meta" in old_name or "Page" in old_name:
                old_name = (
                    f"Observed payload state {chr(65 + index)} — discriminator unknown"
                )
            states.append(
                ResponsePayloadState(
                    frame,
                    state_id,
                    old_name or f"Observed payload state {chr(65 + index)}",
                    bool(item.get("enabled", True)),
                )
            )
        selected_state = value.get("selected_state")
        raw_shared_data = value.get("shared_data", value.get("global_data", {}))
        if not isinstance(raw_shared_data, dict):
            raise ValueError(f"{dataset.value}.shared_data must be an object")
        shared_data = {}
        for raw_offset, raw_entry in raw_shared_data.items():
            entry_value = (
                raw_entry.get("value") if isinstance(raw_entry, dict) else raw_entry
            )
            shared_data[int(raw_offset)] = int(entry_value)
        return cls(
            dataset,
            states,
            str(selected_state) if selected_state else None,
            shared_data,
        )

    def enabled_states(self) -> list[ResponsePayloadState]:
        return [state for state in self.payload_states if state.enabled]

    def selected_payload_state(self) -> ResponsePayloadState:
        enabled = self.enabled_states() or self.payload_states
        return next(
            (state for state in enabled if state.state_id == self.selected_state),
            enabled[0],
        )

    def is_global(self, offset: int) -> bool:
        return offset in self.shared_data

    def value_at(self, state: ResponsePayloadState, offset: int) -> int:
        if not 0 <= offset < 16:
            raise IndexError(offset)
        return self.shared_data.get(offset, state.frame.data[offset])

    def set_global(self, offset: int, value: int) -> None:
        if not 0 <= offset < 16 or not 0 <= value <= 0xFF:
            raise ValueError("shared Data offset or value is invalid")
        self.shared_data[offset] = value
        self._normalize_shared_backing_values()

    def remove_global(self, offset: int) -> int:
        if offset not in self.shared_data:
            raise KeyError(offset)
        value = self.shared_data.pop(offset)
        for state in self.payload_states:
            state.frame.set_data(offset, value)
        return value

    def _normalize_shared_backing_values(self) -> None:
        """Keep editor snapshots coherent; shared_data remains authoritative."""
        for state in self.payload_states:
            for offset, value in self.shared_data.items():
                state.frame.data[offset] = value
            if self.shared_data and state.frame.auto_checksum:
                state.frame.recalculate()

    def response_for_state(self, state: ResponsePayloadState) -> bytes:
        data = bytearray(state.frame.data)
        for offset, value in self.shared_data.items():
            data[offset] = value
        # Captured bytes are authoritative. Auto checksum is applied when the
        # user edits Data (or a shared value), never merely because a frame is sent.
        return bytes(data) + bytes(
            (state.frame.checksum_high, state.frame.checksum_low)
        )

    def selected_response(self) -> bytes:
        return self.response_for_state(self.selected_payload_state())

    def next_response(self) -> bytes:
        return self.selected_response()

    def reset(self) -> None:
        pass

    def to_dict(self) -> dict:
        return {
            "shared_data": {
                str(offset): {"value": value}
                for offset, value in sorted(self.shared_data.items())
            },
            "selected_state": self.selected_state,
            "payload_states": [state.to_dict() for state in self.payload_states],
        }

    def clone(self) -> "RequestResponseSet":
        return RequestResponseSet.from_config(self.dataset, self.to_dict())


def load_default_response_sets() -> dict[Dataset, RequestResponseSet]:
    path = Path(__file__).resolve().parent.parent / "config" / "default_frames.json"
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        values = {
            dataset.value: config for dataset, config in DEFAULT_RESPONSE_CONFIG.items()
        }
    result = {}
    for dataset in Dataset:
        value = values.get(dataset.value, DEFAULT_RESPONSE_CONFIG[dataset])
        try:
            result[dataset] = RequestResponseSet.from_config(dataset, value)
        except (KeyError, TypeError, ValueError):
            result[dataset] = RequestResponseSet.from_config(
                dataset, DEFAULT_RESPONSE_CONFIG[dataset]
            )
    return result


@dataclass(frozen=True)
class FrameKey:
    direction: str
    request_type: str
    payload_class: str | None


def frame_payload_class(frame_type: str, raw: bytes) -> str | None:
    """Classify known AA payload families without using checksum bytes as identity."""
    layout = frame_layout(frame_type, len(raw))
    if frame_type == "AA" and layout is not None:
        class_byte = raw[layout.payload_start + 15]
        if class_byte == 0x00:
            return "CONTROL / SETPOINT (Data[15]=00 candidate)"
        if class_byte == 0xA3:
            return "SYSTEM / PROCESS (Data[15]=A3 candidate)"
        if class_byte == 0xE1:
            return "SYSTEM / PROCESS (Data[15]=E1 candidate)"
        return "UNKNOWN AA PAYLOAD CLASS"
    return None


def frame_key(direction: str, frame_type: str, raw: bytes) -> FrameKey:
    return FrameKey(direction, frame_type, frame_payload_class(frame_type, raw))
