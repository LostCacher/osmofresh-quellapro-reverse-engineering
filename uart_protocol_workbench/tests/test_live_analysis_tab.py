import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from gui.live_analysis_tab import LiveAnalysisTab, highlighted_frame_html
from protocol.checksum import sum16_bytes
from protocol.knowledge import ProtocolKnowledge
from protocol.live_analysis import LiveChangeAnalyzer


def test_only_changed_raw_byte_is_highlighted_and_bits_are_described():
    analyzer = LiveChangeAnalyzer()
    before = bytes((0xAA, 0x10, 0x20, *([0x00] * 16)))
    after = bytes((0xAA, 0x10, 0x21, *([0x00] * 16)))
    analyzer.observe("DISPLAY -> EMU", "AA", before, 1.0)
    difference = analyzer.observe("DISPLAY -> EMU", "AA", after, 2.0)

    html, tooltip = highlighted_frame_html(after, difference)

    assert html.count("background-color") == 1
    assert ">21</span>" in html
    assert "Data[1] (raw offset 2): 20 → 21; bit0 0→1" == tooltip


def test_unchanged_frame_has_no_highlight_or_bit_tooltip():
    analyzer = LiveChangeAnalyzer()
    raw = bytes.fromhex("AA 10 20 30")
    analyzer.observe("DISPLAY -> EMU", "AA", raw, 1.0)
    difference = analyzer.observe("DISPLAY -> EMU", "AA", raw, 2.0)

    html, tooltip = highlighted_frame_html(raw, difference)

    assert "background-color" not in html
    assert tooltip == ""


def test_raw_frame_uses_only_label_without_duplicate_table_item():
    app = QApplication.instance() or QApplication([])
    tab = LiveAnalysisTab(None)
    raw = bytes((0xAA, *([0x00] * 18)))

    tab.observe_frame("DISPLAY -> EMU", "AA", raw, 1.0)

    assert tab.last_frames.item(0, 5) is None
    label = tab.last_frames.cellWidget(0, 5)
    assert isinstance(label, QLabel)
    assert "AA" in label.text()
    tab.close()
    assert app is not None


def test_upper_table_shows_all_number_formats_and_suppresses_repeated_transition():
    app = QApplication.instance() or QApplication([])
    tab = LiveAnalysisTab(None)
    variant_a = bytes((0xAA, 0x10, *([0x00] * 17)))
    variant_b = bytes((0xAA, 0x11, *([0x00] * 17)))

    for timestamp, raw in enumerate((variant_a, variant_b, variant_a, variant_b), 1):
        tab.observe_frame("DISPLAY -> EMU", "AA", raw, float(timestamp))

    # A→B and B→A remain distinct, but the second A→B is not added again.
    assert len(tab.records) == 2
    assert tab.changes.rowCount() == 2
    assert tab.changes.item(0, 7).text() == "10"
    assert tab.changes.item(0, 8).text() == "16"
    assert tab.changes.item(0, 9).text() == "00010000"
    assert tab.changes.item(0, 10).text() == "11"
    assert tab.changes.item(0, 11).text() == "17"
    assert tab.changes.item(0, 12).text() == "00010001"
    assert tab.changes.item(0, 13).text() == "01"
    assert tab.changes.item(0, 14).text() == "1"
    assert tab.changes.item(0, 15).text() == "00000001"
    tab.close()
    assert app is not None


def test_baseline_capture_filters_two_constantly_alternating_states():
    app = QApplication.instance() or QApplication([])
    tab = LiveAnalysisTab(None)
    variant_a = bytes((0xAA, 0x00, *([0x00] * 17)))
    variant_b = bytes((0xAA, 0x80, *([0x00] * 17)))

    tab.observe_frame("DISPLAY -> EMU", "AA", variant_a, 1.0)
    tab.observe_frame("DISPLAY -> EMU", "AA", variant_b, 1.1)
    assert tab.records

    tab.set_baseline()
    tab.observe_frame("DISPLAY -> EMU", "AA", variant_a, 1.2)
    tab.observe_frame("DISPLAY -> EMU", "AA", variant_b, 1.3)
    tab._finish_baseline_capture()

    assert tab.analyzer.baseline_variant_count == 2
    assert tab.records == []
    tab.observe_frame("DISPLAY -> EMU", "AA", variant_a, 2.0)
    tab.observe_frame("DISPLAY -> EMU", "AA", variant_b, 2.1)
    assert tab.records == []

    action_state = bytes((0xAA, 0x01, *([0x00] * 17)))
    tab.observe_frame("DISPLAY -> EMU", "AA", action_state, 2.2)
    tab.observe_frame("DISPLAY -> EMU", "AA", variant_a, 2.3)
    assert [
        (record["before_decimal"], record["after_decimal"])
        for record in tab.records
        if record["raw_offset"] == 1
    ] == [(0, 1), (1, 0)]
    tab.close()
    assert app is not None


def test_sum16_changes_can_be_filtered():
    app = QApplication.instance() or QApplication([])
    tab = LiveAnalysisTab(None)
    before = bytes((0xAA, *([0x00] * 18)))
    changed = bytearray(before)
    changed[1] = 0x01
    changed[18] = 0x01

    tab.observe_frame("DISPLAY -> EMU", "AA", before, 1.0)
    tab.observe_frame("DISPLAY -> EMU", "AA", bytes(changed), 2.0)
    assert tab.changes.rowCount() == 2

    tab.hide_checksum.setChecked(True)
    assert tab.changes.rowCount() == 1
    assert tab.changes.item(0, 6).text() == "Raw[1] / Data[0]"

    assert len(tab.records) == 2
    tab.close()
    assert app is not None


def test_aa_known_meaning_and_changed_bit_come_from_protocol_knowledge(tmp_path):
    app = QApplication.instance() or QApplication([])
    knowledge = ProtocolKnowledge(tmp_path / "protocol_knowledge.json")
    knowledge.set_category("AA", 4, "BITFIELD", "control_setpoint_00")
    knowledge.set_bit(
        "AA", 4, 6, "Device power state: 0=OFF, 1=ON", "HIGH", "control_setpoint_00"
    )
    tab = LiveAnalysisTab(knowledge)
    before = bytes((0xAA, *([0x00] * 18)))
    changed = bytearray(before)
    changed[5] = 0x40  # raw offset 5 is AA Data[4]

    tab.observe_frame("DISPLAY -> EMU", "AA", before, 1.0)
    tab.observe_frame("DISPLAY -> EMU", "AA", bytes(changed), 2.0)

    assert len(tab.records) == 1
    assert tab.records[0]["knowledge_target"] == "Data[4].bit6"
    assert tab.records[0]["known_meaning"] == "Device power state: 0=OFF, 1=ON"
    assert tab.records[0]["knowledge_confidence"] == "HIGH"
    tab.close()
    assert app is not None


def test_category_meaning_and_confidence_persist_at_aa_bit_offset(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "protocol_knowledge.json"
    knowledge = ProtocolKnowledge(path)
    tab = LiveAnalysisTab(knowledge)
    before = bytes((0xAA, *([0x00] * 18)))
    changed = bytearray(before)
    changed[8] = 0x08  # raw offset 8 is AA Data[7].bit3
    tab.observe_frame("DISPLAY -> EMU", "AA", before, 1.0)
    tab.observe_frame("DISPLAY -> EMU", "AA", bytes(changed), 2.0)

    category = tab.changes.cellWidget(0, 17)
    category.setCurrentText("BITFIELD")
    assert tab.records[0]["knowledge_target"] == "Data[7].bit3"
    meaning = tab.changes.item(0, 18)
    meaning.setText("Observed touch command")
    confidence = tab.changes.cellWidget(0, 19)
    confidence.setCurrentText("HIGH")

    saved = ProtocolKnowledge(path)
    assert saved.field("AA", 7, "control_setpoint_00")["category"] == "BITFIELD"
    assert saved.bit("AA", 7, 3, "control_setpoint_00")["meaning"] == "Observed touch command"
    assert saved.bit("AA", 7, 3, "control_setpoint_00")["confidence"] == "HIGH"
    tab.close()
    assert app is not None


def test_a3_and_e1_live_changes_use_separate_aa_knowledge(tmp_path):
    app = QApplication.instance() or QApplication([])
    knowledge = ProtocolKnowledge(tmp_path / "protocol_knowledge.json")
    knowledge.set_field("AA", 0, "A3 byte", "HIGH", "system_process_a3")
    knowledge.set_field("AA", 0, "E1 byte", "MEDIUM", "system_process_e1")
    tab = LiveAnalysisTab(knowledge)

    for payload_class, expected in ((0xA3, "A3 byte"), (0xE1, "E1 byte")):
        before = bytearray(19)
        before[0] = 0xAA
        before[16] = payload_class
        after = bytearray(before)
        after[1] = 1
        tab.observe_frame("DISPLAY -> EMU", "AA", bytes(before), float(payload_class))
        tab.observe_frame("DISPLAY -> EMU", "AA", bytes(after), float(payload_class) + 0.1)
        assert tab.records[-1]["known_meaning"] == expected

    tab.close()
    assert app is not None


def test_multi_bit_change_lists_every_bit_meaning_instead_of_byte_unknown(tmp_path):
    app = QApplication.instance() or QApplication([])
    knowledge = ProtocolKnowledge(tmp_path / "protocol_knowledge.json")
    knowledge.set_category("AA", 1, "BITFIELD", "control_setpoint_00")
    knowledge.set_bit("AA", 1, 7, "Dispensing active", "HIGH", "control_setpoint_00")
    knowledge.set_bit("AA", 1, 0, "Feed pump active", "CONFIRMED", "control_setpoint_00")
    tab = LiveAnalysisTab(knowledge)
    before = bytearray(19)
    before[0] = 0xAA
    after = bytearray(before)
    after[2] = 0x81  # AA raw offset 2 is Data[1], bits 7 and 0.

    tab.observe_frame("DISPLAY -> EMU", "AA", bytes(before), 1.0)
    tab.observe_frame("DISPLAY -> EMU", "AA", bytes(after), 2.0)

    record = tab.records[0]
    assert record["knowledge_target"] == "Data[1].bits7,0"
    assert record["known_meaning"] == (
        "bit7 0→1: Dispensing active; bit0 0→1: Feed pump active"
    )
    assert record["knowledge_confidence"] == "MIXED"
    assert [entry["bit"] for entry in record["knowledge_bits"]] == [7, 0]
    assert not (tab.changes.item(0, 18).flags() & Qt.ItemFlag.ItemIsEditable)
    assert not tab.changes.cellWidget(0, 19).isEnabled()
    tab.close()
    assert app is not None


def test_non_bitfield_multi_bit_change_uses_byte_meaning(tmp_path):
    app = QApplication.instance() or QApplication([])
    knowledge = ProtocolKnowledge(tmp_path / "protocol_knowledge.json")
    knowledge.set_category("A5", 3, "VALUE")
    knowledge.set_field("A5", 3, "Measured byte value", "HIGH")
    tab = LiveAnalysisTab(knowledge)
    before = bytes(18)
    after = bytearray(before)
    after[3] = 0x03

    tab.observe_frame("EMU -> DISPLAY", "A5", before, 1.0)
    tab.observe_frame("EMU -> DISPLAY", "A5", bytes(after), 2.0)

    assert tab.records[0]["known_meaning"] == "Measured byte value"
    assert tab.records[0]["knowledge_target"] == "Data[3]"
    assert tab.records[0]["knowledge_bits"] == []
    tab.close()
    assert app is not None


def test_a5_payload_change_and_dependent_checksum_are_classified_and_filtered():
    app = QApplication.instance() or QApplication([])
    tab = LiveAnalysisTab(None)
    old_payload = bytes.fromhex(
        "00 00 19 00 96 00 04 0D 5D 00 00 84 19 00 ED 00"
    )
    new_payload = bytes.fromhex(
        "00 00 19 00 96 00 04 0D 5D 00 00 84 19 00 EE 00"
    )
    old = old_payload + bytes(sum16_bytes(old_payload))
    new = new_payload + bytes(sum16_bytes(new_payload))

    tab.observe_frame("MAIN -> EMU(DISPLAY)", "A5", old, 1.0)
    tab.observe_frame("MAIN -> EMU(DISPLAY)", "A5", new, 2.0)

    assert [(record["raw_offset"], record["logical_offset"]) for record in tab.records] == [
        (14, "Data[14]"),
        (17, "SUM16 Low"),
    ]
    checksum = tab.records[1]
    assert checksum["byte_role"] == "checksum_low"
    assert checksum["category"] == "CHECKSUM"
    assert checksum["known_meaning"] == "SUM16_L"
    assert checksum["knowledge_confidence"] == "CONFIRMED"
    assert tab.changes.item(1, 6).text() == "Raw[17] / SUM16 Low"
    assert tab.changes.cellWidget(1, 17).currentText() == "CHECKSUM"
    assert tab.changes.item(1, 18).text() == "SUM16_L"
    assert tab.changes.cellWidget(1, 19).currentText() == "CONFIRMED"

    tab.hide_checksum.setChecked(True)
    assert tab.changes.rowCount() == 1
    assert tab.changes.item(0, 6).text() == "Raw[14] / Data[14]"
    tab.close()
    assert app is not None


def test_e5_and_aa_checksum_changes_never_fall_through_as_unknown():
    analyzer = LiveChangeAnalyzer()
    cases = (
        ("MAIN -> EMU(DISPLAY)", "E5 B2", 18, 16, 17),
        ("MAIN -> EMU(DISPLAY)", "E5 E1", 18, 16, 17),
        ("DISPLAY -> EMU", "AA", 19, 17, 18),
    )
    for index, (direction, frame_type, length, high, low) in enumerate(cases):
        before = bytes(length)
        after = bytearray(before)
        after[high] = 1
        after[low] = 2
        analyzer.observe(direction, frame_type, before, float(index * 2 + 1))
        difference = analyzer.observe(
            direction, frame_type, bytes(after), float(index * 2 + 2)
        )
        assert [change.byte_role.value for change in difference.changes] == [
            "checksum_high",
            "checksum_low",
        ]
