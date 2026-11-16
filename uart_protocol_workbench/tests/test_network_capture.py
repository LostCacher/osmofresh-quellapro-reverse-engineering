import json

import pytest

from gui.network_capture_worker import decode_gateway_ws_message, decode_gateway_ws_messages


def test_gateway_protocol_frame_maps_into_existing_analysis_shape():
    message = json.dumps({
        "type": "protocol_frame",
        "data": {
            "direction": "MAIN_TO_DISPLAY",
            "frame_type": "E5_B2_RESPONSE",
            "raw_hex": "00 00 00 36 21 0C 00 00 00 00 00 14 00 01 00 B2 01 30",
            "checksum": {"sum16_h": 1, "sum16_l": 48, "valid": True},
        },
    })
    direction, kind, raw, valid = decode_gateway_ws_message(message)
    assert direction == "MAIN_TO_DISPLAY"
    assert kind == "E5 B2"
    assert raw[3] == 0x36
    assert valid is True


def test_gateway_non_frame_messages_are_ignored_and_bad_direction_rejected():
    assert decode_gateway_ws_message('{"type":"status","data":{}}') is None
    with pytest.raises(ValueError):
        decode_gateway_ws_message(
            '{"type":"protocol_frame","data":{"direction":"SIDEWAYS","raw_hex":"A5"}}'
        )


def test_gateway_protocol_frame_batch_preserves_every_frame_in_order():
    decoded = decode_gateway_ws_messages(json.dumps({
        "type": "protocol_frame_batch",
        "data": {"batch_version": 1, "frames": [
            {"direction": "DISPLAY_TO_MAIN", "frame_type": "A5_REQUEST", "raw_hex": "A5"},
            {"direction": "MAIN_TO_DISPLAY", "frame_type": "CC", "raw_hex": "CC"},
        ]},
    }))
    assert decoded == [
        ("DISPLAY_TO_MAIN", "A5", b"\xA5", None),
        ("MAIN_TO_DISPLAY", "CC", b"\xCC", None),
    ]
