"""Isolated-display TDS mapping response sequencer.

This deliberately changes only E5 B2 Data[3] and the two observed latch
flags.  It is intended for a display PCB disconnected from the real Main MCU.
"""

from __future__ import annotations

from dataclasses import dataclass

from .checksum import sum16_bytes


TDS_DISPLAY_UPPER_BOUNDS = (
    0x1F, 0x2A, 0x36, 0x41, 0x4C, 0x58, 0x6E, 0x79, 0x84, 0x8F,
    0x9A, 0xA4, 0xAF, 0xC4, 0xCF, 0xDA, 0xE4, 0xEF, 0xF9, 0xFF,
)
TDS_MAPPING_NAME = "CAPTURED_DISPLAY_THRESHOLDS_V1"


def tds_display_value(raw_value: int) -> int:
    """Map E5 B2 D3 to the value rendered by the original display."""
    value = int(raw_value)
    if not 0 <= value <= 0xFF:
        raise ValueError("TDS raw value must be in the range 0..255")
    return next(
        displayed for displayed, upper in enumerate(TDS_DISPLAY_UPPER_BOUNDS)
        if value <= upper
    )


@dataclass
class TdsMappingSequencer:
    enabled: bool = False
    raw_value: int = 0
    phase: str = "disabled"

    def queue(self, raw_value: int) -> None:
        if not 0 <= int(raw_value) <= 0xFF:
            raise ValueError("TDS raw value must be in the range 0..255")
        self.raw_value = int(raw_value)
        self.enabled = True
        self.phase = "measurement"

    def disable(self) -> None:
        self.enabled = False
        self.phase = "disabled"

    def transform_b2(self, normal_response: bytes) -> bytes:
        """Return measurement once, then commit/hold for later B2 polls."""
        if not self.enabled:
            return bytes(normal_response)
        if len(normal_response) != 18:
            raise ValueError("E5 B2 response must contain 16 Data bytes plus SUM16")

        data = bytearray(normal_response[:16])
        data[3] = self.raw_value
        data[13] |= 0x01  # observed valid-measurement marker
        if self.phase == "measurement":
            data[11] |= 0x10
            self.phase = "commit"
        else:
            data[11] &= ~0x10
            self.phase = "hold"
        return bytes(data) + bytes(sum16_bytes(data))

    def snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "raw_value": self.raw_value,
            "phase": self.phase,
        }
