"""Decoder for the ESP8266 gateway's binary QT v1/v2 UDP stream."""

from __future__ import annotations

from dataclasses import dataclass
import struct


MAGIC = b"QT"
VERSIONS = (1, 2)
FLAG_CRC16 = 0x01
HEADER_SIZE_V1 = 21
HEADER_SIZE_V2 = 22
MAX_FRAME_SIZE = 19

FRAME_TYPE_NAMES = {
    0: "UNKNOWN", 1: "A5_REQUEST", 2: "E5_B2_REQUEST",
    3: "E5_E1_REQUEST", 4: "AA00", 5: "AA_A3", 6: "AA_E1",
    7: "AA_F1", 8: "A5", 9: "E5_B2", 10: "E5_E1", 11: "CC",
    12: "AA_OTHER",
}
DIRECTION_NAMES = {0: "DISPLAY_TO_MAIN", 1: "MAIN_TO_DISPLAY"}


class TelemetryDecodeError(ValueError):
    pass


@dataclass(frozen=True)
class TelemetryFrame:
    direction: int
    frame_type: int
    timestamp_us: int
    raw: bytes

    @property
    def direction_name(self) -> str:
        return DIRECTION_NAMES[self.direction]

    @property
    def type_name(self) -> str:
        return FRAME_TYPE_NAMES.get(self.frame_type, f"UNKNOWN_{self.frame_type}")


@dataclass(frozen=True)
class TelemetryPacket:
    sequence: int
    esp_uptime_ms: int
    mega_batch_sequence: int
    mega_dropped_raw_frames: int
    frames: tuple[TelemetryFrame, ...]
    wifi_rssi_dbm: int | None = None


def crc16(data: bytes) -> int:
    value = 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = (((value << 1) ^ 0x1021) & 0xFFFF
                     if value & 0x8000 else (value << 1) & 0xFFFF)
    return value


def decode_telemetry(data: bytes) -> TelemetryPacket:
    if len(data) < HEADER_SIZE_V1 + 2:
        raise TelemetryDecodeError("truncated QT header")
    if data[:2] != MAGIC:
        raise TelemetryDecodeError("invalid QT magic")
    values = struct.unpack_from("<BBIIBHHI", data, 2)
    version, flags, sequence, uptime, frame_count, payload_length, mega_batch, mega_dropped = values
    if version not in VERSIONS:
        raise TelemetryDecodeError(f"unsupported QT version {version}")
    if not flags & FLAG_CRC16:
        raise TelemetryDecodeError(f"QT v{version} packet has no CRC16 flag")
    header_size = HEADER_SIZE_V2 if version == 2 else HEADER_SIZE_V1
    expected_length = header_size + payload_length + 2
    if len(data) != expected_length:
        raise TelemetryDecodeError(
            f"QT length mismatch: expected {expected_length}, received {len(data)}"
        )
    stored_crc = struct.unpack_from("<H", data, len(data) - 2)[0]
    calculated_crc = crc16(data[2:-2])
    if stored_crc != calculated_crc:
        raise TelemetryDecodeError(
            f"QT CRC mismatch: 0x{stored_crc:04X} != 0x{calculated_crc:04X}"
        )

    wifi_rssi_dbm = struct.unpack_from("<b", data, 21)[0] if version == 2 else None
    offset, payload_end = header_size, header_size + payload_length
    frames: list[TelemetryFrame] = []
    for _ in range(frame_count):
        if offset + 7 > payload_end:
            raise TelemetryDecodeError("truncated QT frame header")
        direction, frame_type, timestamp_us, raw_length = struct.unpack_from(
            "<BBIB", data, offset
        )
        offset += 7
        if direction not in DIRECTION_NAMES:
            raise TelemetryDecodeError(f"invalid direction {direction}")
        if not 0 < raw_length <= MAX_FRAME_SIZE or offset + raw_length > payload_end:
            raise TelemetryDecodeError("invalid QT raw-frame length")
        frames.append(TelemetryFrame(
            direction, frame_type, timestamp_us,
            bytes(data[offset:offset + raw_length]),
        ))
        offset += raw_length
    if offset != payload_end:
        raise TelemetryDecodeError("unexpected bytes after QT frame entries")
    return TelemetryPacket(sequence, uptime, mega_batch, mega_dropped,
                           tuple(frames), wifi_rssi_dbm)


def sum16_valid(frame: TelemetryFrame) -> bool | None:
    raw = frame.raw
    if frame.type_name in ("A5", "E5_B2", "E5_E1") and len(raw) == 18:
        payload, stored = raw[:16], raw[16:18]
    elif frame.type_name.startswith("AA") and len(raw) == 19 and raw[0] == 0xAA:
        payload, stored = raw[1:17], raw[17:19]
    elif frame.type_name == "CC":
        return raw == b"\xCC"
    else:
        return None
    checksum = sum(payload)
    return stored == bytes(((checksum >> 8) & 0xFF, checksum & 0xFF))


def payload_bytes(frame: TelemetryFrame) -> bytes:
    if frame.type_name in ("A5", "E5_B2", "E5_E1") and len(frame.raw) == 18:
        return frame.raw[:16]
    if frame.type_name.startswith("AA") and len(frame.raw) == 19:
        return frame.raw[1:17]
    return frame.raw
