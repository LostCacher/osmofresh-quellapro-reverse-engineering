import struct

import pytest

from protocol.gateway_protocol import crc16
from protocol.telemetry import (
    QgRawBatch, SequenceTracker, TelemetryCrcError, TelemetryDecodeError,
    TelemetryFrame, TelemetryPacket, decode_qg_raw_batch, decode_telemetry,
    direction_name, encode_qg_raw_batch, encode_telemetry,
)
from telemetry_receiver import TelemetryReceiver


def response(payload: bytes) -> bytes:
    return payload + (sum(payload) & 0xFFFF).to_bytes(2, "big")


def aa(payload: bytes) -> bytes:
    return b"\xAA" + payload + (sum(payload) & 0xFFFF).to_bytes(2, "big")


def sample_packet(sequence: int = 1) -> TelemetryPacket:
    a5 = bytearray(16)
    a5[1], a5[3:5], a5[11], a5[12], a5[14] = 1, (25).to_bytes(2, "big"), 0x10, 19, 230
    b2_active = bytearray(16)
    b2_active[3], b2_active[4], b2_active[11], b2_active[13], b2_active[15] = 0x36, 0x08, 0x14, 1, 0xB2
    control = bytearray(16)
    control[1], control[2], control[3:5] = 1, 25, (150).to_bytes(2, "big")
    return TelemetryPacket(sequence, 123456, 17, 2, (
        TelemetryFrame(0, 4, 1000, aa(bytes(control))),
        TelemetryFrame(1, 8, 2000, response(bytes(a5))),
        TelemetryFrame(1, 9, 3000, response(bytes(b2_active))),
    ))


def test_qt_encode_decode_multiple_frames_timestamps_and_directions():
    packet = sample_packet(123)
    decoded = decode_telemetry(encode_telemetry(packet))
    assert decoded == packet
    assert decoded.frames[0].timestamp_us == 1000
    assert direction_name(decoded.frames[0].direction) == "DISPLAY->MAIN"
    assert direction_name(decoded.frames[1].direction) == "MAIN->DISPLAY"


def test_qg_raw_batch_encode_decode_and_drop_counter():
    source = sample_packet().frames
    batch = QgRawBatch(0x1234, 7, source)
    decoded = decode_qg_raw_batch(encode_qg_raw_batch(batch))
    assert decoded == batch
    assert decoded.frames[2].timestamp_us == 3000


def test_qg_raw_batch_rejects_truncation_and_trailing_bytes():
    wire = encode_qg_raw_batch(QgRawBatch(1, 0, sample_packet().frames))
    with pytest.raises(TelemetryDecodeError):
        decode_qg_raw_batch(wire[:-1])
    with pytest.raises(TelemetryDecodeError):
        decode_qg_raw_batch(wire + b"\x00")


def test_qt_sequence_gap_wrap_duplicate_and_out_of_order():
    tracker = SequenceTracker()
    assert tracker.observe(1) == 0
    assert tracker.observe(2) == 0
    assert tracker.observe(5) == 2
    assert tracker.lost == 2
    assert tracker.observe(5) == 0
    assert tracker.duplicates == 1
    assert tracker.observe(4) == 0
    assert tracker.out_of_order == 1
    wrap = SequenceTracker()
    wrap.observe(0xFFFFFFFF)
    assert wrap.observe(0) == 0


def test_qt_rejects_malformed_truncated_crc_and_direction():
    wire = bytearray(encode_telemetry(sample_packet()))
    with pytest.raises(TelemetryDecodeError):
        decode_telemetry(b"bad")
    with pytest.raises(TelemetryDecodeError):
        decode_telemetry(bytes(wire[:-3]))
    bad_crc = wire[:]
    bad_crc[-1] ^= 0x80
    with pytest.raises(TelemetryCrcError):
        decode_telemetry(bytes(bad_crc))
    bad_direction = wire[:]
    bad_direction[21] = 7
    struct.pack_into("<H", bad_direction, len(bad_direction) - 2, crc16(bad_direction[2:-2]))
    with pytest.raises(TelemetryDecodeError):
        decode_telemetry(bytes(bad_direction))


def test_receiver_reuses_live_state_and_tds_mapping():
    receiver = TelemetryReceiver()
    assert receiver.process_datagram(encode_telemetry(sample_packet()), received_at=100.0)
    snapshot = receiver.state.to_dict(100.0)
    assert snapshot["dispense"]["requested_volume_ml"]["value"] == 150
    assert snapshot["dispense"]["current_volume_ml"]["value"] == 25
    assert snapshot["temperature"]["inlet_c"]["value"] == 19
    assert snapshot["tanks"]["internal_min"]["value"] is True
    assert snapshot["water_quality"]["tds"]["value"] == 0x36
    assert snapshot["water_quality"]["tds_calculated"]["value"] == 2
    assert receiver.sequence.packets == 1
    assert receiver.last_mega_dropped == 2
    assert receiver.render() is not None


def test_receiver_counts_crc_and_malformed_without_crashing():
    receiver = TelemetryReceiver()
    assert not receiver.process_datagram(b"short")
    wire = bytearray(encode_telemetry(sample_packet()))
    wire[-1] ^= 1
    assert not receiver.process_datagram(bytes(wire))
    assert receiver.counters.malformed_packets == 1
    assert receiver.counters.crc_errors == 1
