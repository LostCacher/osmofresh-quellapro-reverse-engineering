"""SUM16 checksum helpers used by both protocol directions."""

from collections.abc import Sequence

from .framing import frame_layout


def calculate_sum16(data: Sequence[int] | bytes | bytearray) -> int:
    """Return the integer sum of exactly 16 payload bytes."""
    if len(data) != 16:
        raise ValueError(f"SUM16 requires 16 data bytes, got {len(data)}")
    if any(not 0 <= int(value) <= 0xFF for value in data):
        raise ValueError("all data values must be bytes")
    return sum(int(value) for value in data)


def sum16_bytes(data: Sequence[int] | bytes | bytearray) -> tuple[int, int]:
    """Return the transmitted high and low byte of the 16-byte sum."""
    checksum = calculate_sum16(data)
    return (checksum >> 8) & 0xFF, checksum & 0xFF


def validate_response(frame: Sequence[int] | bytes | bytearray) -> bool:
    """Validate Data[0..15] + SUM16_H + SUM16_L."""
    layout = frame_layout("A5", len(frame))
    if layout is None:
        return False
    payload = frame[
        layout.payload_start : layout.payload_start + layout.payload_length
    ]
    stored = (frame[layout.checksum_high_offset], frame[layout.checksum_low_offset])
    return stored == sum16_bytes(payload)


def validate_aa_frame(frame: Sequence[int] | bytes | bytearray) -> bool:
    """Validate AA + Data[0..15] + SUM16_H + SUM16_L; AA is excluded."""
    layout = frame_layout("AA", len(frame))
    if layout is None or frame[layout.header_offset] != 0xAA:
        return False
    payload = frame[
        layout.payload_start : layout.payload_start + layout.payload_length
    ]
    stored = (frame[layout.checksum_high_offset], frame[layout.checksum_low_offset])
    return stored == sum16_bytes(payload)
