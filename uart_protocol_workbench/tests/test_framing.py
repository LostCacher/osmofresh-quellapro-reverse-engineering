import pytest

from protocol.checksum import sum16_bytes
from protocol.framing import FrameByteRole, frame_byte_metadata, frame_layout
from protocol.parser import FrameKind, StreamParser


@pytest.mark.parametrize("frame_type", ["A5", "E5 B2", "E5 E1"])
def test_complete_response_last_two_bytes_are_always_checksum(frame_type):
    layout = frame_layout(frame_type, 18)
    assert layout.payload_start == 0
    assert layout.payload_length == 16
    assert frame_byte_metadata(frame_type, 18, 15).logical_offset == "Data[15]"
    assert frame_byte_metadata(frame_type, 18, 16).role is FrameByteRole.CHECKSUM_HIGH
    assert frame_byte_metadata(frame_type, 18, 17).role is FrameByteRole.CHECKSUM_LOW


def test_complete_aa_layout_separates_header_payload_and_checksum():
    assert frame_byte_metadata("AA", 19, 0).role is FrameByteRole.HEADER
    assert frame_byte_metadata("AA", 19, 1).logical_offset == "Data[0]"
    assert frame_byte_metadata("AA", 19, 16).logical_offset == "Data[15]"
    assert frame_byte_metadata("AA", 19, 17).role is FrameByteRole.CHECKSUM_HIGH
    assert frame_byte_metadata("AA", 19, 18).role is FrameByteRole.CHECKSUM_LOW


def test_parser_exposes_the_same_layout_for_complete_aa_frame():
    payload = bytes(range(16))
    raw = b"\xAA" + payload + bytes(sum16_bytes(payload))
    parsed = StreamParser().feed(raw)[0]
    assert parsed.kind is FrameKind.AA
    assert parsed.layout == frame_layout("AA", 19)
    assert parsed.layout.checksum_high_offset == 17
    assert parsed.layout.checksum_low_offset == 18


def test_incomplete_or_request_only_bytes_do_not_claim_response_checksum_layout():
    assert frame_layout("A5", 1) is None
    assert frame_byte_metadata("A5", 1, 0).role is FrameByteRole.UNKNOWN
