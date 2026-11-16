"""Authoritative byte boundaries for complete UART protocol frames."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FrameByteRole(str, Enum):
    HEADER = "header"
    PAYLOAD = "payload"
    CHECKSUM_HIGH = "checksum_high"
    CHECKSUM_LOW = "checksum_low"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FrameLayout:
    frame_type: str
    total_length: int
    payload_start: int
    payload_length: int
    checksum_high_offset: int
    checksum_low_offset: int
    header_offset: int | None = None


@dataclass(frozen=True)
class FrameByteMetadata:
    raw_offset: int
    role: FrameByteRole
    logical_offset: str
    payload_offset: int | None = None

    @property
    def is_checksum(self) -> bool:
        return self.role in (
            FrameByteRole.CHECKSUM_HIGH,
            FrameByteRole.CHECKSUM_LOW,
        )


RESPONSE_TYPES = ("A5", "E5 B2", "E5 E1")


def frame_layout(frame_type: str, total_length: int) -> FrameLayout | None:
    """Return a layout only for a complete, currently known protocol frame."""
    if frame_type in RESPONSE_TYPES and total_length == 18:
        return FrameLayout(frame_type, 18, 0, 16, 16, 17)
    if frame_type == "AA" and total_length == 19:
        return FrameLayout(frame_type, 19, 1, 16, 17, 18, 0)
    return None


def frame_byte_metadata(
    frame_type: str, total_length: int, raw_offset: int
) -> FrameByteMetadata:
    """Describe one raw byte without using direction or legacy Meta concepts."""
    if not 0 <= raw_offset < total_length:
        raise IndexError(raw_offset)
    layout = frame_layout(frame_type, total_length)
    if layout is None:
        return FrameByteMetadata(
            raw_offset, FrameByteRole.UNKNOWN, f"Byte[{raw_offset}]"
        )
    if raw_offset == layout.header_offset:
        return FrameByteMetadata(raw_offset, FrameByteRole.HEADER, "AA Header")
    if raw_offset == layout.checksum_high_offset:
        return FrameByteMetadata(
            raw_offset, FrameByteRole.CHECKSUM_HIGH, "SUM16 High"
        )
    if raw_offset == layout.checksum_low_offset:
        return FrameByteMetadata(
            raw_offset, FrameByteRole.CHECKSUM_LOW, "SUM16 Low"
        )
    payload_offset = raw_offset - layout.payload_start
    if 0 <= payload_offset < layout.payload_length:
        return FrameByteMetadata(
            raw_offset,
            FrameByteRole.PAYLOAD,
            f"Data[{payload_offset}]",
            payload_offset,
        )
    return FrameByteMetadata(raw_offset, FrameByteRole.UNKNOWN, f"Byte[{raw_offset}]")


def frame_metadata(frame_type: str, raw: bytes) -> tuple[FrameByteMetadata, ...]:
    return tuple(
        frame_byte_metadata(frame_type, len(raw), offset)
        for offset in range(len(raw))
    )
