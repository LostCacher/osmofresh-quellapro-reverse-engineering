import json

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from gui.frame_editor import FrameEditor
from protocol.checksum import sum16_bytes
from protocol.frames import Dataset, RequestResponseSet, load_default_response_sets
from protocol.knowledge import ProtocolKnowledge, build_default_knowledge


def two_state_response_set() -> RequestResponseSet:
    """Exercise shared-data behavior without depending on user default captures."""
    return RequestResponseSet.from_config(
        Dataset.E5_B2,
        {
            "selected_state": "state_a",
            "payload_states": [
                {
                    "id": "state_a",
                    "name": "Observed state A",
                    "response": "00 00 00 00 20 0C 00 00 00 00 00 00 00 00 00 B2 00 DE",
                },
                {
                    "id": "state_b",
                    "name": "Observed state B",
                    "response": "00 00 00 49 00 0C 00 00 00 00 00 14 00 01 1C B2 01 38",
                },
            ],
        },
    )


def test_shared_value_overlays_every_payload_state_and_sum16():
    response_set = two_state_response_set()
    response_set.set_global(4, 0x25)

    page0 = response_set.response_for_state(response_set.payload_states[0])
    page1 = response_set.response_for_state(response_set.payload_states[1])

    assert (page0[4], page1[4]) == (0x25, 0x25)
    assert page0[16:18] == bytes(sum16_bytes(page0[:16]))
    assert page1[16:18] == bytes(sum16_bytes(page1[:16]))


def test_global_data_round_trip_and_old_config_compatibility():
    response_set = load_default_response_sets()[Dataset.E5_B2]
    response_set.set_global(4, 0x31)

    reloaded = RequestResponseSet.from_config(Dataset.E5_B2, response_set.to_dict())
    assert reloaded.shared_data == {4: 0x31}
    assert [reloaded.next_response()[4] for _ in range(2)] == [0x31, 0x31]

    old_config = response_set.to_dict()
    old_config.pop("shared_data")
    old_reloaded = RequestResponseSet.from_config(Dataset.E5_B2, old_config)
    assert old_reloaded.shared_data == {}


def test_config_without_globals_preserves_captured_checksum_exactly():
    raw = "00 00 00 00 20 0C 00 00 00 00 00 00 00 00 00 B2 00 7F"
    response_set = RequestResponseSet.from_config(Dataset.E5_B2, raw)

    assert response_set.selected_response()[-1] == 0x7F

    response_set.set_global(4, 0x21)
    response = response_set.selected_response()
    assert response[-2:] == bytes(sum16_bytes(response[:16]))


def test_removing_global_initializes_all_variants_with_last_value():
    response_set = two_state_response_set()
    response_set.set_global(4, 0x47)

    assert response_set.remove_global(4) == 0x47
    assert not response_set.is_global(4)
    assert [state.frame.data[4] for state in response_set.payload_states] == [0x47, 0x47]


def test_global_knowledge_persists_and_metadata_survives_both_conversions(tmp_path):
    path = tmp_path / "protocol_knowledge.json"
    knowledge = ProtocolKnowledge(path)
    knowledge.set_field(Dataset.E5_B2, 4, "Tank Status", "HIGH", "observed_state_a")
    knowledge.set_category(Dataset.E5_B2, 4, "BITFIELD", "observed_state_a")
    knowledge.set_bit(Dataset.E5_B2, 4, 3, "Internal Tank Low", "MEDIUM", "observed_state_a")
    knowledge.make_shared(Dataset.E5_B2, 4, 0x20, "observed_state_a")

    reloaded = ProtocolKnowledge(path)
    entry = reloaded.shared_fields(Dataset.E5_B2)["4"]
    assert entry["value"] == 0x20
    assert reloaded.field(Dataset.E5_B2, 4)["meaning"] == "Tank Status"
    assert reloaded.bit(Dataset.E5_B2, 4, 3)["meaning"] == "Internal Tank Low"

    assert reloaded.make_state_specific(Dataset.E5_B2, 4) == 0x20
    final = ProtocolKnowledge(path)
    assert final.field(Dataset.E5_B2, 4)["meaning"] == "Tank Status"
    assert final.bit(Dataset.E5_B2, 4, 3)["meaning"] == "Internal Tank Low"


def test_missing_global_data_is_migrated_as_empty_and_isolated_per_request(tmp_path):
    path = tmp_path / "old_protocol_knowledge.json"
    old_data = build_default_knowledge()
    old_data["schema_version"] = 6
    for dataset_data in old_data["datasets"].values():
        dataset_data.pop("shared_data", None)
    path.write_text(json.dumps(old_data), encoding="utf-8")

    knowledge = ProtocolKnowledge(path)
    assert knowledge.shared_fields(Dataset.A5) == {}
    assert knowledge.shared_fields(Dataset.E5_B2) == {}

    knowledge.make_shared(Dataset.E5_B2, 4, 0x20, "observed_state_a")
    assert knowledge.is_shared(Dataset.E5_B2, 4)
    assert not knowledge.is_shared(Dataset.A5, 4)


def test_frame_editor_moves_global_row_and_edits_single_authoritative_value(tmp_path):
    app = QApplication.instance() or QApplication([])
    knowledge = ProtocolKnowledge(tmp_path / "protocol_knowledge.json")
    response_set = two_state_response_set()
    editor = FrameEditor(response_set.payload_states[0].frame, knowledge, response_set)

    assert editor.table.item(16, 0).text() == "SUM16 High"
    assert editor.table.item(17, 0).text() == "SUM16 Low"
    assert not (editor.table.item(16, 2).flags() & Qt.ItemFlag.ItemIsEditable)

    editor._global_toggled(4, True)
    assert editor.table.isRowHidden(4)
    assert editor.global_table.rowCount() == 1
    assert editor.global_table.item(0, 0).text() == "Data[4]"

    editor.global_table.item(0, 2).setText("25")
    app.processEvents()
    assert response_set.shared_data == {4: 0x25}
    assert [response_set.next_response()[4] for _ in range(2)] == [0x25, 0x25]

    editor._global_toggled(4, False)
    assert not editor.table.isRowHidden(4)
    assert editor.global_table.rowCount() == 0
    assert [state.frame.data[4] for state in response_set.payload_states] == [0x25, 0x25]
    editor.close()
    assert app is not None


def test_frame_editor_live_values_are_read_only_overlay(tmp_path):
    app = QApplication.instance() or QApplication([])
    knowledge = ProtocolKnowledge(tmp_path / "protocol_knowledge.json")
    response_set = two_state_response_set()
    editor = FrameEditor(response_set.payload_states[0].frame, knowledge, response_set)
    configured = response_set.selected_response()
    live_data = bytearray(configured[:16])
    live_data[2] = 0x2A
    live = bytes(live_data) + bytes(sum16_bytes(live_data))

    editor.observe_live_response(live, 123.5)
    assert editor.table.item(2, 2).text() == f"{configured[2]:02X}"

    editor.live_values.setChecked(True)
    app.processEvents()
    assert editor.table.item(2, 2).text() == "2A"
    assert editor.table.item(2, 3).text() == "42"
    assert not (editor.table.item(2, 2).flags() & Qt.ItemFlag.ItemIsEditable)
    assert response_set.selected_response() == configured

    editor.live_values.setChecked(False)
    app.processEvents()
    assert editor.table.item(2, 2).text() == f"{configured[2]:02X}"
    assert response_set.selected_response() == configured
    editor.close()


def test_frame_editor_allows_bit_notes_before_byte_is_classified_as_bitfield(tmp_path):
    app = QApplication.instance() or QApplication([])
    knowledge = ProtocolKnowledge(tmp_path / "protocol_knowledge.json")
    response_set = two_state_response_set()
    editor = FrameEditor(response_set.payload_states[0].frame, knowledge, response_set)

    editor.table.selectRow(0)
    app.processEvents()

    assert knowledge.field(Dataset.E5_B2, 0)["category"] == "UNKNOWN"
    assert all(button.isEnabled() for button in editor.bit_buttons)
    assert all(edit.isEnabled() for edit in editor.bit_meanings)
    editor.bit_meanings[0].setText("Candidate flag")
    editor._save_bit_description(7)
    assert knowledge.field(Dataset.E5_B2, 0)["category"] == "BITFIELD"
    assert knowledge.bit(Dataset.E5_B2, 0, 7)["meaning"] == "Candidate flag"
    editor.close()
