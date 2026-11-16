"""Persistent, human-readable protocol knowledge for bytes and bitfields."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .frames import Dataset


CONFIDENCE_LEVELS = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "STRONG", "CONFIRMED")
FIELD_CATEGORIES = (
    "UNKNOWN", "BITFIELD", "VALUE", "ENUM", "COUNTER", "TEMPERATURE", "POWER"
)
DEFAULT_KNOWLEDGE_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "protocol_knowledge.json"
)

DATASET_NAMES = ("A5", "E5 B2", "E5 E1", "AA")
AA_PAYLOAD_CLASSES = {
    "control_setpoint_00": {
        "name": "CONTROL / SETPOINT",
        "discriminator": {"offset": 15, "value": "00"},
    },
    "system_process_a3": {
        "name": "SYSTEM / PROCESS A3",
        "discriminator": {"offset": 15, "value": "A3"},
    },
    "system_process_e1": {
        "name": "SYSTEM / PROCESS E1",
        "discriminator": {"offset": 15, "value": "E1"},
    },
}


def _description(meaning: str = "UNKNOWN", confidence: str = "UNKNOWN") -> dict[str, str]:
    return {"meaning": meaning, "confidence": confidence}


def _empty_bits() -> dict[str, dict[str, str]]:
    return {str(bit): _description() for bit in range(7, -1, -1)}


def _empty_bytes() -> list[dict]:
    byte_entries = []
    for offset in range(16):
        entry: dict = {
            "offset": offset,
            "category": "UNKNOWN",
            "meaning": "UNKNOWN",
            "confidence": "UNKNOWN",
        }
        byte_entries.append(entry)
    return byte_entries


def _empty_dataset(dataset_name: str) -> dict:
    dataset = {
        "bytes": _empty_bytes(),
        "shared_data": {},
        "sum16_high": _description("SUM16 high byte", "CONFIRMED"),
        "sum16_low": _description("SUM16 low byte", "CONFIRMED"),
    }
    if dataset_name == "AA":
        dataset.pop("bytes")
        dataset["payload_classes"] = {
            class_id: {
                **deepcopy(description),
                "bytes": _empty_bytes(),
            }
            for class_id, description in AA_PAYLOAD_CLASSES.items()
        }
        dataset["header"] = _description("AA frame header (excluded from SUM16)", "CONFIRMED")
    return dataset


def build_default_knowledge() -> dict:
    """Return a clean knowledge model with no inherited field interpretations."""
    return {
        "schema_version": 10,
        "checksum_model": "SUM16_BE",
        "knowledge_scope": "shared_per_frame_type_except_aa_payload_class",
        "datasets": {
            dataset_name: _empty_dataset(dataset_name)
            for dataset_name in DATASET_NAMES
        },
    }


class ProtocolKnowledge:
    """Load, migrate, and atomically persist protocol annotations."""

    def __init__(self, path: Path | str = DEFAULT_KNOWLEDGE_PATH) -> None:
        self.path = Path(path)
        defaults = build_default_knowledge()
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = self._merge(defaults, loaded)
        else:
            self.data = defaults
        # Persist a schema reset or subsequent user edits atomically.
        self.save()

    @staticmethod
    def _merge(defaults: dict, loaded: dict) -> dict:
        if not isinstance(loaded, dict):
            raise ValueError("protocol knowledge must be a JSON object")
        if loaded.get("schema_version") == 10:
            return loaded
        if loaded.get("schema_version") == 9:
            loaded_datasets = loaded.get("datasets", {})
            if not isinstance(loaded_datasets, dict):
                return defaults
            for dataset_name in ("A5", "E5 B2", "E5 E1"):
                source = loaded_datasets.get(dataset_name)
                if isinstance(source, dict):
                    defaults["datasets"][dataset_name] = source
            source_aa = loaded_datasets.get("AA", {})
            target_aa = defaults["datasets"]["AA"]
            if isinstance(source_aa, dict):
                source_bytes = source_aa.get("bytes", [])
                target_bytes = target_aa["payload_classes"]["control_setpoint_00"]["bytes"]
                if isinstance(source_bytes, list):
                    for offset, source in enumerate(source_bytes[:16]):
                        if isinstance(source, dict):
                            ProtocolKnowledge._merge_field(target_bytes[offset], source)
                for key in ("shared_data", "header", "sum16_high", "sum16_low"):
                    if key in source_aa:
                        target_aa[key] = source_aa[key]
            return defaults
        if loaded.get("schema_version") == 8:
            loaded_datasets = loaded.get("datasets", {})
            if not isinstance(loaded_datasets, dict):
                return defaults
            for dataset_name, target_dataset in defaults["datasets"].items():
                source_dataset = loaded_datasets.get(dataset_name, {})
                if not isinstance(source_dataset, dict):
                    continue
                raw_shared = source_dataset.get("shared_data", {})
                if isinstance(raw_shared, dict):
                    for raw_offset, entry in raw_shared.items():
                        try:
                            offset = int(raw_offset)
                            value = int(entry.get("value") if isinstance(entry, dict) else entry)
                        except (TypeError, ValueError):
                            continue
                        if 0 <= offset < 16 and 0 <= value <= 0xFF:
                            target_dataset["shared_data"][str(offset)] = {
                                "offset": offset,
                                "value": value,
                            }
                states = source_dataset.get("payload_states", {})
                if not isinstance(states, dict):
                    continue
                selected = str(source_dataset.get("selected_state", ""))
                ordered = []
                if selected in states:
                    ordered.append(states[selected])
                ordered.extend(state for key, state in states.items() if key != selected)
                for state in ordered:
                    if not isinstance(state, dict):
                        continue
                    source_bytes = state.get("bytes", [])
                    if not isinstance(source_bytes, list):
                        continue
                    for offset, source in enumerate(source_bytes[:16]):
                        if isinstance(source, dict):
                            target_bytes = (
                                target_dataset["payload_classes"]["control_setpoint_00"]["bytes"]
                                if dataset_name == "AA"
                                else target_dataset["bytes"]
                            )
                            ProtocolKnowledge._merge_field(target_bytes[offset], source)
            return defaults
        # Schema 7 and earlier encoded the checksum high byte as a false Meta
        # identity. The user explicitly requested a clean interpretation reset;
        # raw captures remain in default_frames/startup profiles, not here.
        return defaults

    @staticmethod
    def _merge_field(target: dict, source: dict) -> None:
        """Merge one legacy state annotation into shared frame-type knowledge."""
        for key in ("category", "meaning", "confidence"):
            value = source.get(key)
            if (
                isinstance(value, str)
                and value.strip()
                and value.strip().upper() != "UNKNOWN"
                and str(target.get(key, "UNKNOWN")).upper() == "UNKNOWN"
            ):
                target[key] = value
        if target.get("category") != "BITFIELD":
            target.pop("bits", None)
            return
        target_bits = target.setdefault("bits", _empty_bits())
        source_bits = source.get("bits", {})
        if not isinstance(source_bits, dict):
            return
        for bit, source_bit in source_bits.items():
            if bit not in target_bits or not isinstance(source_bit, dict):
                continue
            for key in ("meaning", "confidence"):
                value = source_bit.get(key)
                if (
                    isinstance(value, str)
                    and value.strip()
                    and value.strip().upper() != "UNKNOWN"
                    and target_bits[bit].get(key, "UNKNOWN").upper() == "UNKNOWN"
                ):
                    target_bits[bit][key] = value

    @staticmethod
    def _clean(value: str) -> str:
        return value.strip() or "UNKNOWN"

    def _dataset_name(self, dataset: Dataset | str) -> str:
        name = dataset.value if isinstance(dataset, Dataset) else str(dataset)
        if name not in self.data["datasets"]:
            raise KeyError(name)
        return name

    def page(self, dataset: Dataset | str, state_id: str | None = None) -> dict:
        """Return frame annotations, split by payload class for AA only."""
        dataset_name = self._dataset_name(dataset)
        dataset_data = self.data["datasets"][dataset_name]
        if dataset_name != "AA":
            return dataset_data
        class_id = self._aa_class_id(state_id)
        class_data = dataset_data["payload_classes"][class_id]
        return {
            **class_data,
            "header": dataset_data["header"],
            "sum16_high": dataset_data["sum16_high"],
            "sum16_low": dataset_data["sum16_low"],
        }

    @staticmethod
    def _aa_class_id(state_id: str | None) -> str:
        aliases = {
            None: "control_setpoint_00",
            "control_setpoint": "control_setpoint_00",
        }
        class_id = aliases.get(state_id, state_id)
        if class_id not in AA_PAYLOAD_CLASSES:
            raise KeyError(f"unknown AA payload class: {state_id}")
        return str(class_id)

    def shared_fields(self, dataset: Dataset | str) -> dict[str, dict]:
        dataset_name = self._dataset_name(dataset)
        return self.data["datasets"][dataset_name].setdefault("shared_data", {})

    def is_shared(self, dataset: Dataset | str, offset: int) -> bool:
        return str(offset) in self.shared_fields(dataset)

    def make_shared(
        self,
        dataset: Dataset | str,
        offset: int,
        value: int,
        state_id: str | None = None,
    ) -> dict:
        if not 0 <= offset < 16 or not 0 <= value <= 0xFF:
            raise ValueError("shared Data offset or value is invalid")
        shared = self.shared_fields(dataset)
        if str(offset) in shared:
            entry = shared[str(offset)]
        else:
            entry = {"offset": offset, "value": value}
            shared[str(offset)] = entry
        entry["value"] = value
        self.save()
        return entry

    def make_state_specific(self, dataset: Dataset | str, offset: int) -> int:
        shared = self.shared_fields(dataset)
        if str(offset) not in shared:
            raise KeyError(offset)
        shared_entry = shared.pop(str(offset))
        value = int(shared_entry["value"])
        self.save()
        return value

    def set_shared_value(self, dataset: Dataset | str, offset: int, value: int) -> None:
        if not 0 <= value <= 0xFF:
            raise ValueError("shared Data value must be between 00 and FF")
        entry = self.shared_fields(dataset).get(str(offset))
        if entry is None:
            raise KeyError(offset)
        entry["value"] = value
        self.save()

    def field(self, dataset: Dataset | str, row: int, state_id: str | None = None) -> dict:
        dataset_data = self.page(dataset, state_id)
        if 0 <= row < 16:
            return dataset_data["bytes"][row]
        if row == 16:
            return dataset_data["sum16_high"]
        if row == 17:
            return dataset_data["sum16_low"]
        raise IndexError(row)

    def is_bitfield(
        self, dataset: Dataset | str, offset: int, state_id: str | None = None
    ) -> bool:
        return 0 <= offset < 16 and self.field(dataset, offset, state_id)["category"] == "BITFIELD"

    def bit(
        self, dataset: Dataset | str, offset: int, bit: int, state_id: str | None = None
    ) -> dict[str, str]:
        if not 0 <= bit < 8 or not self.is_bitfield(dataset, offset, state_id):
            raise ValueError(f"{self._dataset_name(dataset)} Data[{offset}] is not a BITFIELD")
        return self.field(dataset, offset, state_id)["bits"][str(bit)]

    def set_field(
        self, dataset: Dataset | str, row: int, meaning: str, confidence: str,
        state_id: str | None = None,
    ) -> None:
        entry = self.field(dataset, row, state_id)
        entry.update(meaning=self._clean(meaning), confidence=self._clean(confidence))
        self.save()

    def set_category(
        self, dataset: Dataset | str, offset: int, category: str,
        state_id: str | None = None,
    ) -> None:
        if category not in FIELD_CATEGORIES or not 0 <= offset < 16:
            raise ValueError(f"invalid field category: {category}")
        entry = self.field(dataset, offset, state_id)
        entry["category"] = category
        if category == "BITFIELD":
            entry.setdefault("bits", _empty_bits())
        else:
            entry.pop("bits", None)
        self.save()

    def set_bit(
        self, dataset: Dataset | str, offset: int, bit: int, meaning: str, confidence: str,
        state_id: str | None = None,
    ) -> None:
        entry = self.bit(dataset, offset, bit, state_id)
        entry.update(meaning=self._clean(meaning), confidence=self._clean(confidence))
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)
