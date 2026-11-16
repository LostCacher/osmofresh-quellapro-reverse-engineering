import struct

import pytest

from protocol.gateway_protocol import (
    GatewayDecoder, GatewayMessage, GatewayPacket, decode_protocol_frame, decode_status, encode,
)


def test_gateway_envelope_round_trip_fragmentation_and_resync():
    wire = encode(GatewayPacket(GatewayMessage.DISPENSE, 0x1234, struct.pack("<HB", 50, 25)))
    decoder = GatewayDecoder()
    assert decoder.feed(b"noise" + wire[:5]) == []
    assert decoder.feed(wire[5:]) == [GatewayPacket(GatewayMessage.DISPENSE, 0x1234, b"\x32\x00\x19")]


def test_gateway_decoder_rejects_bad_crc_then_accepts_next_packet():
    bad = bytearray(encode(GatewayPacket(GatewayMessage.PING, 1, b"x")))
    bad[-1] ^= 0x80
    good = encode(GatewayPacket(GatewayMessage.GET_STATUS, 2, b""))
    assert GatewayDecoder().feed(bytes(bad) + good) == [GatewayPacket(GatewayMessage.GET_STATUS, 2, b"")]


def test_status_known_mask_preserves_unknowns_as_none():
    payload = bytearray(46)
    payload[0] = 1
    struct.pack_into("<I", payload, 1, 1234)
    known = (1 << 9) | (1 << 13) | (1 << 23) | (1 << 29)
    struct.pack_into("<I", payload, 5, known)
    struct.pack_into("<H", payload, 17, 42)
    payload[25], payload[34], payload[45] = 25, 229, 0x36
    status = decode_status(bytes(payload))
    assert status["dispense"]["current_ml"] == 42
    assert status["dispense"]["target_ml"] is None
    assert status["dispense"]["requested_temp_c"] == 25
    assert status["electrical"]["ac_mains_v"] == 229
    assert status["water"]["tds"] == 0x36
    assert status["water"]["tds_raw"] == 0x36
    assert status["water"]["tds_calculated"] == 2
    assert status["water"]["tds_mapping"] == "CAPTURED_DISPLAY_THRESHOLDS_V1"
    assert "external_voltage" not in status["electrical"]


def test_status_rejects_wrong_length_or_version():
    with pytest.raises(ValueError):
        decode_status(b"\x01")
    with pytest.raises(ValueError):
        decode_status(bytes(46))


def test_protocol_frame_decodes_all_b2_data_bytes_and_checksum():
    raw = bytes.fromhex("00 00 00 36 21 0C 00 00 00 00 00 14 00 01 00 B2 01 30")
    payload = bytearray(13) + raw
    payload[0:4] = bytes((1, 1, 9, 0x07))
    struct.pack_into("<I", payload, 4, 123456)
    struct.pack_into("<I", payload, 8, 42)
    payload[12] = len(raw)
    frame = decode_protocol_frame(bytes(payload))
    assert frame["direction"] == "MAIN_TO_DISPLAY"
    assert frame["frame_type"] == "E5_B2_RESPONSE"
    assert frame["data_dec"] == list(raw[:16])
    assert frame["data_hex"][3] == "36"
    assert frame["data_bin"][4] == "00100001"
    assert frame["checksum"] == {"sum16_h": 1, "sum16_l": 0x30, "valid": True}


def test_protocol_frame_aa_uses_logical_d0_at_raw_offset_one():
    raw = bytes.fromhex("AA 00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E A3 00 00")
    payload = bytearray(13) + raw
    payload[0:4] = bytes((1, 0, 5, 0x03))
    payload[12] = len(raw)
    frame = decode_protocol_frame(bytes(payload))
    assert frame["frame_type"] == "AA_A3"
    assert frame["data_offset_base"] == 1
    assert frame["data_dec"] == list(raw[1:17])
    assert frame["checksum"]["valid"] is False
