"""Stateful parser for arbitrarily fragmented UART reads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .checksum import validate_aa_frame
from .framing import FrameLayout, frame_layout


class FrameKind(str, Enum):
    A5 = "A5"
    E5_B2 = "E5 B2"
    E5_E1 = "E5 E1"
    AA = "AA"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ParsedFrame:
    kind: FrameKind
    raw: bytes
    checksum_valid: bool | None = None

    @property
    def layout(self) -> FrameLayout | None:
        """Shared byte-boundary metadata for complete frames."""
        return frame_layout(self.kind.value, len(self.raw))


class StreamParser:
    """Recognize requests without losing partial or unknown input bytes."""

    AA_LENGTH = 19

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def pending(self) -> bytes:
        return bytes(self._buffer)

    def reset(self) -> bytes:
        pending = bytes(self._buffer)
        self._buffer.clear()
        return pending

    def feed(self, chunk: bytes | bytearray) -> list[ParsedFrame]:
        self._buffer.extend(chunk)
        result: list[ParsedFrame] = []
        while self._buffer:
            first = self._buffer[0]
            if first == 0xA5:
                del self._buffer[0]
                result.append(ParsedFrame(FrameKind.A5, b"\xA5"))
            elif first == 0xE5:
                if len(self._buffer) < 2:
                    break
                second = self._buffer[1]
                if second in (0xB2, 0xE1):
                    raw = bytes(self._buffer[:2])
                    del self._buffer[:2]
                    kind = FrameKind.E5_B2 if second == 0xB2 else FrameKind.E5_E1
                    result.append(ParsedFrame(kind, raw))
                else:
                    del self._buffer[0]
                    result.append(ParsedFrame(FrameKind.UNKNOWN, b"\xE5"))
            elif first == 0xAA:
                if len(self._buffer) < self.AA_LENGTH:
                    break
                raw = bytes(self._buffer[: self.AA_LENGTH])
                del self._buffer[: self.AA_LENGTH]
                result.append(ParsedFrame(FrameKind.AA, raw, validate_aa_frame(raw)))
            else:
                del self._buffer[0]
                result.append(ParsedFrame(FrameKind.UNKNOWN, bytes((first,))))
        return result
