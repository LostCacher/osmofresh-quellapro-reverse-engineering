from __future__ import annotations

import struct

from quella_state import QuellaState, tds_display_value
from telemetry_protocol import TelemetryFrame, crc16, decode_telemetry, sum16_valid


def response_frame(frame_type: int, payload: bytes) -> TelemetryFrame:
    checksum = sum(payload)
    raw = payload + bytes(((checksum >> 8) & 0xFF, checksum & 0xFF))
    return TelemetryFrame(1, frame_type, 1234, raw)


def test_decodes_qt_packet_and_checks_sum16():
    frame = response_frame(8, bytes(range(16)))
    entry = struct.pack("<BBIB", 1, 8, 1234, len(frame.raw)) + frame.raw
    header = b"QT" + struct.pack("<BBIIBHHI", 1, 1, 7, 9000, 1, len(entry), 4, 0)
    wire = header + entry + struct.pack("<H", crc16((header + entry)[2:]))

    packet = decode_telemetry(wire)

    assert packet.sequence == 7
    assert packet.frames[0].raw == frame.raw
    assert sum16_valid(packet.frames[0]) is True


def test_decodes_qt_v2_wifi_rssi_and_keeps_v1_compatible():
    frame = response_frame(8, bytes(range(16)))
    entry = struct.pack("<BBIB", 1, 8, 1234, len(frame.raw)) + frame.raw
    header = b"QT" + struct.pack("<BBIIBHHI", 2, 1, 8, 9100, 1, len(entry), 5, 0)
    wire_without_crc = header + struct.pack("<b", -67) + entry
    wire = wire_without_crc + struct.pack("<H", crc16(wire_without_crc[2:]))

    packet = decode_telemetry(wire)

    assert packet.wifi_rssi_dbm == -67
    assert packet.frames == (frame,)


def test_state_decodes_confirmed_fields_and_preserves_every_payload_byte():
    state = QuellaState()
    payload = bytearray(16)
    payload[1] = 0x01
    payload[3:5] = (42).to_bytes(2, "big")
    payload[5:9] = (123456).to_bytes(4, "big")
    payload[11] = 0x10
    payload[12] = 19
    payload[14] = 246
    frame = response_frame(8, bytes(payload))

    state.observe(frame, 100.0)
    snapshot = state.snapshot({}, 100.5)

    assert snapshot["fields"]["dispensing_active"]["value"] is True
    assert snapshot["fields"]["current_dispensed_volume_ml"]["value"] == 42
    assert snapshot["fields"]["ac_mains_voltage_v"]["value"] == 246
    stored = snapshot["frames"]["MAIN_TO_DISPLAY:A5"]
    assert stored["payload_bytes"] == list(payload)
    assert stored["checksum_valid"] is True


def test_tds_is_committed_on_update_pulse_falling_edge():
    state = QuellaState()
    measuring = bytearray(16)
    measuring[3] = 0x37
    measuring[11] = 0x10
    measuring[13] = 0x01
    state.observe(response_frame(9, bytes(measuring)), 10.0)

    committed = bytearray(measuring)
    committed[11] &= ~0x10
    state.observe(response_frame(9, bytes(committed)), 10.1)
    snapshot = state.snapshot({}, 10.1)

    assert snapshot["fields"]["tds_raw"]["value"] == 0x37
    assert snapshot["fields"]["tds_display"]["value"] == 3
    assert tds_display_value(48) == 2

    # D11.bit3 is a periodic scheduler/refresh pulse observed roughly every
    # 20 seconds. It must not latch a new D3 value as TDS.
    periodic = bytearray(committed)
    periodic[3] = 0x40
    periodic[11] = 0x08
    periodic[13] = 0x00
    state.observe(response_frame(9, bytes(periodic)), 10.2)
    periodic[11] = 0x00
    state.observe(response_frame(9, bytes(periodic)), 10.3)
    assert state.snapshot({}, 10.3)["fields"]["tds_raw"]["value"] == 0x37


def test_b2_d14_is_exposed_as_booster_pump_power_with_compatibility_alias():
    state = QuellaState()
    payload = bytearray(16)
    payload[14] = 83

    state.observe(response_frame(9, bytes(payload)), 12.0)
    fields = state.snapshot({}, 12.1)["fields"]
    item = fields["booster_pump_power_w"]

    assert item["value"] == 83
    assert item["raw"] == 83
    assert item["unit"] == ""
    assert item["source"] == "E5 B2"
    assert fields["b2_d14_candidate_raw"]["value"] == 83


def test_external_empty_latch_is_visible_in_json_until_start_abort_ack():
    state = QuellaState()
    low = bytearray(16)
    low[4] = 0x01
    low[15] = 0xB2
    state.observe(response_frame(9, bytes(low)), 10.0)

    available = bytearray(low)
    available[4] = 0x00
    state.observe(response_frame(9, bytes(available)), 10.1)
    assert state.snapshot({}, 10.1)["fields"]["external_empty_stop_latched"]["value"] is True

    start = bytearray(16)
    start[1] = 0x01
    state.observe(TelemetryFrame(0, 4, 1234, (
        b"\xAA" + bytes(start) + (sum(start) & 0xFFFF).to_bytes(2, "big")
    )), 10.2)
    assert state.snapshot({}, 10.2)["fields"]["external_empty_reset_armed"]["value"] is True

    start[1] = 0x00
    state.observe(TelemetryFrame(
        0, 4, 1235,
        b"\xAA" + bytes(start) + (sum(start) & 0xFFFF).to_bytes(2, "big"),
    ), 10.3)
    fields = state.snapshot({}, 10.3)["fields"]
    assert fields["external_empty_stop_latched"]["value"] is False
    assert fields["external_empty_reset_armed"]["value"] is False
