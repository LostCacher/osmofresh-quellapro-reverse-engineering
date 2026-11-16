import json

from protocol.frames import Dataset
from protocol.knowledge import ProtocolKnowledge, build_default_knowledge


def test_knowledge_contains_every_byte_and_only_bitfield_bits(tmp_path):
    path = tmp_path / "protocol_knowledge.json"
    knowledge = ProtocolKnowledge(path)

    for dataset in Dataset:
        dataset_data = knowledge.page(dataset)
        assert [entry["offset"] for entry in dataset_data["bytes"]] == list(range(16))
        for entry in dataset_data["bytes"]:
            if entry["category"] == "BITFIELD":
                assert set(entry["bits"]) == set("01234567")
            else:
                assert "bits" not in entry


def test_byte_and_bit_meanings_are_persistent(tmp_path):
    path = tmp_path / "protocol_knowledge.json"
    knowledge = ProtocolKnowledge(path)
    knowledge.set_field(Dataset.E5_E1, 3, "Main byte note", "MEDIUM")
    knowledge.set_category(Dataset.E5_E1, 3, "BITFIELD")
    knowledge.set_bit(Dataset.E5_E1, 3, 6, "Individual bit note", "HIGH")

    reloaded = ProtocolKnowledge(path)
    assert reloaded.field(Dataset.E5_E1, 3)["meaning"] == "Main byte note"
    assert reloaded.field(Dataset.E5_E1, 3)["confidence"] == "MEDIUM"
    assert reloaded.bit(Dataset.E5_E1, 3, 6)["meaning"] == "Individual bit note"
    assert reloaded.bit(Dataset.E5_E1, 3, 6)["confidence"] == "HIGH"
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 10


def test_non_bitfield_category_removes_bits(tmp_path):
    path = tmp_path / "protocol_knowledge.json"
    knowledge = ProtocolKnowledge(path)
    knowledge.set_category(Dataset.A5, 10, "BITFIELD")
    knowledge.set_bit(Dataset.A5, 10, 7, "Temporary description", "LOW")
    knowledge.set_category(Dataset.A5, 10, "TEMPERATURE")

    entry = ProtocolKnowledge(path).field(Dataset.A5, 10)
    assert entry["category"] == "TEMPERATURE"
    assert "bits" not in entry


def test_aa_knowledge_uses_same_byte_and_bitfield_schema(tmp_path):
    path = tmp_path / "protocol_knowledge.json"
    knowledge = ProtocolKnowledge(path)

    aa = knowledge.page("AA", "control_setpoint_00")
    assert [entry["offset"] for entry in aa["bytes"]] == list(range(16))
    assert aa["header"] == {"meaning": "AA frame header (excluded from SUM16)", "confidence": "CONFIRMED"}
    assert knowledge.field("AA", 2, "control_setpoint_00")["category"] == "UNKNOWN"
    assert knowledge.field("AA", 16, "control_setpoint_00")["meaning"] == "SUM16 high byte"
    assert knowledge.field("AA", 17)["meaning"] == "SUM16 low byte"


def test_aa_meanings_and_bits_are_persistent(tmp_path):
    path = tmp_path / "protocol_knowledge.json"
    knowledge = ProtocolKnowledge(path)
    knowledge.set_field("AA", 7, "Display command field", "MEDIUM", "system_process_a3")
    knowledge.set_category("AA", 7, "BITFIELD", "system_process_a3")
    knowledge.set_bit("AA", 7, 3, "Observed touch command", "LOW", "system_process_a3")

    reloaded = ProtocolKnowledge(path)
    assert reloaded.field("AA", 7, "system_process_a3")["meaning"] == "Display command field"
    assert reloaded.bit("AA", 7, 3, "system_process_a3")["meaning"] == "Observed touch command"
    assert reloaded.field("AA", 7, "control_setpoint_00")["meaning"] == "UNKNOWN"
    assert reloaded.field("AA", 7, "system_process_e1")["meaning"] == "UNKNOWN"


def test_aa_payload_classes_have_independent_field_descriptions(tmp_path):
    knowledge = ProtocolKnowledge(tmp_path / "protocol_knowledge.json")
    knowledge.set_category("AA", 1, "BITFIELD", "control_setpoint_00")
    knowledge.set_bit("AA", 1, 0, "Control flag", "HIGH", "control_setpoint_00")
    knowledge.set_category("AA", 1, "BITFIELD", "system_process_a3")
    knowledge.set_bit("AA", 1, 0, "A3 flag", "LOW", "system_process_a3")
    knowledge.set_category("AA", 1, "BITFIELD", "system_process_e1")
    knowledge.set_bit("AA", 1, 0, "E1 flag", "MEDIUM", "system_process_e1")

    assert knowledge.bit("AA", 1, 0, "control_setpoint_00")["meaning"] == "Control flag"
    assert knowledge.bit("AA", 1, 0, "system_process_a3")["meaning"] == "A3 flag"
    assert knowledge.bit("AA", 1, 0, "system_process_e1")["meaning"] == "E1 flag"


def test_old_meta_schema_is_reset_to_unknown_without_false_pages(tmp_path):
    path = tmp_path / "protocol_knowledge.json"
    path.write_text(json.dumps({"schema_version": 7, "datasets": {"A5": {"pages": {"03": {"bytes": [{"offset": 0, "meaning": "OLD"}]}}}}}), encoding="utf-8")
    knowledge = ProtocolKnowledge(path)
    assert knowledge.data["schema_version"] == 10
    assert "pages" not in knowledge.data["datasets"]["A5"]
    assert "payload_states" not in knowledge.data["datasets"]["A5"]
    assert knowledge.field("A5", 0)["meaning"] == "UNKNOWN"


def test_schema8_payload_state_annotations_collapse_to_one_shared_field(tmp_path):
    path = tmp_path / "protocol_knowledge.json"
    empty = [
        {"offset": offset, "category": "UNKNOWN", "meaning": "UNKNOWN", "confidence": "UNKNOWN"}
        for offset in range(16)
    ]
    documented = [dict(entry) for entry in empty]
    documented[2] = {
        "offset": 2,
        "category": "TEMPERATURE",
        "meaning": "Outlet temperature",
        "confidence": "HIGH",
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": 8,
                "datasets": {
                    "A5": {
                        "selected_state": "observed_state_a",
                        "shared_data": {},
                        "payload_states": {
                            "observed_state_a": {"bytes": documented},
                            "observed_state_b": {"bytes": empty},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    knowledge = ProtocolKnowledge(path)
    assert knowledge.field("A5", 2)["meaning"] == "Outlet temperature"
    assert "payload_states" not in knowledge.data["datasets"]["A5"]


def test_schema9_aa_annotations_migrate_only_to_data15_00_class(tmp_path):
    path = tmp_path / "protocol_knowledge.json"
    old = build_default_knowledge()
    old["schema_version"] = 9
    old["knowledge_scope"] = "shared_per_frame_type"
    old_aa = old["datasets"]["AA"]
    control = old_aa.pop("payload_classes")["control_setpoint_00"]
    old_aa["bytes"] = control["bytes"]
    old_aa["bytes"][2].update(
        category="VALUE", meaning="Temperature setpoint", confidence="CONFIRMED"
    )
    path.write_text(json.dumps(old), encoding="utf-8")

    knowledge = ProtocolKnowledge(path)

    assert knowledge.field("AA", 2, "control_setpoint_00")["meaning"] == "Temperature setpoint"
    assert knowledge.field("AA", 2, "system_process_a3")["meaning"] == "UNKNOWN"
    assert knowledge.field("AA", 2, "system_process_e1")["meaning"] == "UNKNOWN"
    assert knowledge.data["schema_version"] == 10
