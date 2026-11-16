import json

from protocol.checksum import sum16_bytes
from protocol.dispense_capture import (
    DispenseSequenceCapture,
    is_display_dispense_start,
)
from protocol.knowledge import ProtocolKnowledge


def aa_frame(payload: bytes) -> bytes:
    return b"\xAA" + payload + bytes(sum16_bytes(payload))


def test_capture_records_both_directions_deltas_semantics_and_markers(tmp_path):
    knowledge = ProtocolKnowledge(tmp_path / "knowledge.json")
    knowledge.set_category("AA", 2, "TEMPERATURE", "control_setpoint_00")
    knowledge.set_field("AA", 2, "Set temperature", "CONFIRMED", "control_setpoint_00")
    knowledge.set_category("AA", 3, "VALUE", "control_setpoint_00")
    knowledge.set_field("AA", 3, "Set volume HIGH", "CONFIRMED", "control_setpoint_00")
    capture = DispenseSequenceCapture(knowledge)
    payload = bytes.fromhex("00 01 19 00 96 00 00 00 00 00 00 00 00 00 00 00")
    aa = aa_frame(payload)
    response = bytes.fromhex("00 00 19 00 96 00 03 B8 7A 00 00 94 19 00 F9 00 03 8A")

    capture.start("A", "automatic", 100_000)
    first = capture.add_frame(100_000, "DISPLAY_TO_MAIN", "AA", aa, True)
    second = capture.add_frame(121_000, "MAIN_TO_DISPLAY", "A5", response, True)
    capture.stop()

    assert first["delta_us"] == 0
    assert first["contains_temperature"] and first["contains_volume"]
    assert second["delta_us"] == 21_000
    assert capture.records[0]["marker"] == "DISPENSE_SEQUENCE_START"
    assert capture.records[-1]["marker"] == "DISPENSE_SEQUENCE_END"


def test_capture_exports_comparable_jsonl_and_csv(tmp_path):
    knowledge = ProtocolKnowledge(tmp_path / "knowledge.json")
    capture = DispenseSequenceCapture(knowledge)
    payload = bytes.fromhex("00 01 19 00 0A 00 00 00 00 00 00 00 00 00 00 00")
    frame = aa_frame(payload)
    capture.start("C", "manual button", 50)
    capture.add_frame(50, "DISPLAY_TO_MAIN", "AA", frame, True)
    capture.stop()

    jsonl = capture.export_jsonl(tmp_path / "capture.jsonl")
    csv_path = capture.export_csv(tmp_path / "capture.csv")
    records = [json.loads(line) for line in jsonl.read_text().splitlines()]

    assert records[0]["scenario"] == "C"
    assert records[1]["frame_index"] == 1
    assert "decoded_type" in records[1]
    assert "known_fields" in csv_path.read_text()


def test_only_complete_aa00_dispense_start_has_trigger_shape():
    payload = bytearray(16)
    payload[1] = 0x01
    payload[2] = 0x19
    payload[3:5] = (150).to_bytes(2, "big")
    frame = aa_frame(payload)

    assert is_display_dispense_start(frame)
    assert not is_display_dispense_start(frame[:-1])
    stopped = bytearray(frame)
    stopped[2] = 0x00
    assert not is_display_dispense_start(bytes(stopped))
