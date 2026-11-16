"""Binary QT v1 UDP telemetry codec shared by receiver tests and tools."""

from __future__ import annotations

from dataclasses import dataclass
import struct

from .gateway_protocol import crc16


MAGIC = b"QT"
VERSION = 1
FLAG_CRC16 = 0x01
HEADER_SIZE = 21
MAX_FRAME_SIZE = 19
QG_BATCH_VERSION = 1
QG_BATCH_HEADER_SIZE = 8


class TelemetryDecodeError(ValueError):
    pass


class TelemetryCrcError(TelemetryDecodeError):
    pass


@dataclass(frozen=True)
class TelemetryFrame:
    direction: int
    frame_type: int
    timestamp_us: int
    raw: bytes


@dataclass(frozen=True)
class TelemetryPacket:
    sequence: int
    esp_uptime_ms: int
    mega_batch_sequence: int
    mega_dropped_raw_frames: int
    frames: tuple[TelemetryFrame, ...]
    flags: int = FLAG_CRC16


@dataclass(frozen=True)
class QgRawBatch:
    sequence: int
    dropped_raw_frames: int
    frames: tuple[TelemetryFrame, ...]


FRAME_TYPE_NAMES = {
    0: "UNKNOWN", 1: "A5 request", 2: "E5 B2 request", 3: "E5 E1 request",
    4: "AA00", 5: "AA A3", 6: "AA E1", 7: "AA F1",
    8: "A5 response", 9: "E5 B2 response", 10: "E5 E1 response",
    11: "CC", 12: "AA other",
}


def direction_name(direction: int) -> str:
    return {0: "DISPLAY->MAIN", 1: "MAIN->DISPLAY"}.get(direction, f"UNKNOWN({direction})")


def frame_type_name(frame_type: int) -> str:
    return FRAME_TYPE_NAMES.get(frame_type, f"UNKNOWN({frame_type})")


def _entries(frames: tuple[TelemetryFrame, ...]) -> bytes:
    payload = bytearray()
    for frame in frames:
        if frame.direction not in (0, 1):
            raise ValueError("direction must be 0 or 1")
        if not 0 < len(frame.raw) <= MAX_FRAME_SIZE:
            raise ValueError("raw telemetry frames must contain 1..19 bytes")
        payload.extend(struct.pack("<BBIB", frame.direction, frame.frame_type,
                                   frame.timestamp_us & 0xFFFFFFFF, len(frame.raw)))
        payload.extend(frame.raw)
    return bytes(payload)


def encode_qg_raw_batch(batch: QgRawBatch) -> bytes:
    entries = _entries(batch.frames)
    if len(batch.frames) > 255 or QG_BATCH_HEADER_SIZE + len(entries) > 192:
        raise ValueError("QG raw batch exceeds the 192-byte QG payload")
    return struct.pack(
        "<BHBI", QG_BATCH_VERSION, batch.sequence & 0xFFFF,
        len(batch.frames), batch.dropped_raw_frames & 0xFFFFFFFF,
    ) + entries


def decode_qg_raw_batch(payload: bytes) -> QgRawBatch:
    if len(payload) < QG_BATCH_HEADER_SIZE:
        raise TelemetryDecodeError("truncated QG raw batch header")
    version, sequence, frame_count, dropped = struct.unpack_from("<BHBI", payload)
    if version != QG_BATCH_VERSION:
        raise TelemetryDecodeError(f"unsupported QG raw batch version {version}")
    offset = QG_BATCH_HEADER_SIZE
    frames: list[TelemetryFrame] = []
    for _ in range(frame_count):
        if offset + 7 > len(payload):
            raise TelemetryDecodeError("truncated QG raw frame header")
        direction, frame_type, timestamp_us, raw_length = struct.unpack_from(
            "<BBIB", payload, offset
        )
        offset += 7
        if direction not in (0, 1):
            raise TelemetryDecodeError(f"invalid frame direction {direction}")
        if not 0 < raw_length <= MAX_FRAME_SIZE or offset + raw_length > len(payload):
            raise TelemetryDecodeError("invalid or truncated QG raw frame")
        frames.append(TelemetryFrame(
            direction, frame_type, timestamp_us,
            bytes(payload[offset:offset + raw_length]),
        ))
        offset += raw_length
    if offset != len(payload):
        raise TelemetryDecodeError("unexpected bytes after QG raw frame entries")
    return QgRawBatch(sequence, dropped, tuple(frames))


def encode_telemetry(packet: TelemetryPacket) -> bytes:
    entries = _entries(packet.frames)
    if len(packet.frames) > 255 or len(entries) > 0xFFFF:
        raise ValueError("telemetry packet is too large")
    header = MAGIC + struct.pack(
        "<BBIIBHHI", VERSION, packet.flags, packet.sequence & 0xFFFFFFFF,
        packet.esp_uptime_ms & 0xFFFFFFFF, len(packet.frames), len(entries),
        packet.mega_batch_sequence & 0xFFFF,
        packet.mega_dropped_raw_frames & 0xFFFFFFFF,
    )
    body = header + entries
    return body + struct.pack("<H", crc16(body[2:]))


def decode_telemetry(data: bytes) -> TelemetryPacket:
    if len(data) < HEADER_SIZE + 2:
        raise TelemetryDecodeError("truncated QT header")
    if data[:2] != MAGIC:
        raise TelemetryDecodeError("invalid QT magic")
    version, flags, sequence, uptime, frame_count, payload_length, mega_batch, mega_dropped = struct.unpack_from(
        "<BBIIBHHI", data, 2
    )
    if version != VERSION:
        raise TelemetryDecodeError(f"unsupported QT version {version}")
    if not flags & FLAG_CRC16:
        raise TelemetryDecodeError("QT v1 requires CRC16")
    expected_length = HEADER_SIZE + payload_length + 2
    if len(data) != expected_length:
        raise TelemetryDecodeError(
            f"QT length mismatch: header says {expected_length}, received {len(data)}"
        )
    stored_crc = struct.unpack_from("<H", data, len(data) - 2)[0]
    calculated_crc = crc16(data[2:-2])
    if stored_crc != calculated_crc:
        raise TelemetryCrcError(
            f"QT CRC mismatch: stored 0x{stored_crc:04X}, calculated 0x{calculated_crc:04X}"
        )
    offset, payload_end = HEADER_SIZE, HEADER_SIZE + payload_length
    frames: list[TelemetryFrame] = []
    for _ in range(frame_count):
        if offset + 7 > payload_end:
            raise TelemetryDecodeError("truncated QT frame header")
        direction, frame_type, timestamp_us, raw_length = struct.unpack_from("<BBIB", data, offset)
        offset += 7
        if direction not in (0, 1):
            raise TelemetryDecodeError(f"invalid frame direction {direction}")
        if not 0 < raw_length <= MAX_FRAME_SIZE or offset + raw_length > payload_end:
            raise TelemetryDecodeError("invalid or truncated QT raw frame")
        frames.append(TelemetryFrame(direction, frame_type, timestamp_us,
                                     bytes(data[offset:offset + raw_length])))
        offset += raw_length
    if offset != payload_end:
        raise TelemetryDecodeError("unexpected bytes after QT frame entries")
    return TelemetryPacket(sequence, uptime, mega_batch, mega_dropped,
                           tuple(frames), flags)


@dataclass
class SequenceTracker:
    last_sequence: int | None = None
    packets: int = 0
    lost: int = 0
    duplicates: int = 0
    out_of_order: int = 0

    def observe(self, sequence: int) -> int:
        sequence &= 0xFFFFFFFF
        self.packets += 1
        if self.last_sequence is None:
            self.last_sequence = sequence
            return 0
        delta = (sequence - self.last_sequence) & 0xFFFFFFFF
        if delta == 0:
            self.duplicates += 1
            return 0
        if delta < 0x80000000:
            missing = delta - 1
            self.lost += missing
            self.last_sequence = sequence
            return missing
        self.out_of_order += 1
        return 0


def live_state_frame_type(frame_type: int) -> str | None:
    return {
        4: "AA", 5: "AA", 6: "AA", 7: "AA", 8: "A5",
        9: "E5 B2", 10: "E5 E1", 11: "CC", 12: "AA",
    }.get(frame_type)
