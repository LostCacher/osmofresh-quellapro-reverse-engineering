"""Host-side reference codec for the independent Mega↔ESP QG v1 link."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct

from .tds_mapping import TDS_MAPPING_NAME, tds_display_value

MAGIC = b"QG"
VERSION = 1
MAX_PAYLOAD = 192


class GatewayMessage(IntEnum):
    PING = 0x01
    GET_STATUS = 0x02
    DISPENSE = 0x03
    STOP_DISPENSE = 0x04
    SET_PRESET = 0x05
    SET_TANK_DELAY = 0x06
    GET_CONFIG = 0x07
    GET_OVERRIDES = 0x08
    SET_OVERRIDE = 0x09
    DELETE_OVERRIDE = 0x0A
    SAVE_CONFIG = 0x0B
    HELLO = 0x80
    PONG = 0x81
    STATUS_SNAPSHOT = 0x82
    EVENT = 0x83
    COMMAND_RESULT = 0x84
    CONFIG_RESULT = 0x85
    ERROR = 0x86
    RAW_BATCH = 0x87
    OVERRIDES_RESULT = 0x88
    # Compatibility alias for old capture files/tools. On the wire 0x87 now
    # carries the documented QG_RAW_BATCH payload, not a single frame.
    PROTOCOL_FRAME = RAW_BATCH


@dataclass(frozen=True)
class GatewayPacket:
    message_type: int
    sequence: int
    payload: bytes
    flags: int = 0


def crc16(data: bytes) -> int:
    value = 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ 0x1021) & 0xFFFF if value & 0x8000 else (value << 1) & 0xFFFF
    return value


def encode(packet: GatewayPacket) -> bytes:
    if len(packet.payload) > MAX_PAYLOAD:
        raise ValueError(f"QG payload exceeds {MAX_PAYLOAD} bytes")
    body = struct.pack("<BBBHH", VERSION, packet.message_type, packet.flags, packet.sequence, len(packet.payload)) + packet.payload
    return MAGIC + body + struct.pack("<H", crc16(body))


class GatewayDecoder:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[GatewayPacket]:
        self.buffer.extend(data)
        packets: list[GatewayPacket] = []
        while True:
            start = self.buffer.find(MAGIC)
            if start < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer.endswith(MAGIC[:1]) else b""
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < 11:
                break
            version, message, flags, sequence, length = struct.unpack_from("<BBBHH", self.buffer, 2)
            if version != VERSION or length > MAX_PAYLOAD:
                del self.buffer[0]
                continue
            total = 11 + length
            if len(self.buffer) < total:
                break
            body = bytes(self.buffer[2 : 9 + length])
            expected = struct.unpack_from("<H", self.buffer, 9 + length)[0]
            if crc16(body) != expected:
                del self.buffer[0]
                continue
            packets.append(GatewayPacket(message, sequence, bytes(self.buffer[9 : 9 + length]), flags))
            del self.buffer[:total]
        return packets


def decode_status(payload: bytes) -> dict:
    if len(payload) != 46 or payload[0] != 1:
        raise ValueError("unsupported QG status snapshot")
    known = struct.unpack_from("<I", payload, 5)[0]
    u16 = lambda offset: struct.unpack_from("<H", payload, offset)[0]
    u32 = lambda offset: struct.unpack_from("<I", payload, offset)[0]
    value = lambda bit, item: item if known & (1 << bit) else None
    flags, tank, fill = payload[10], payload[28], payload[29]
    return {
        "version": 1,
        "mega_uptime_ms": u32(1),
        "machine": {
            "mode": value(27, payload[9]), "a5_d11_bit4_cyclic": value(0, bool(flags & 1)),
            "dispensing": value(1, bool(flags & 2)), "filling": value(2, bool(flags & 4)),
            "feed_pump": value(1, bool(flags & 2)),
            "flushing": value(3, bool(flags & 8)), "heating": value(4, bool(flags & 16)),
            "pump": value(5, bool(flags & 32)), "valve": value(6, bool(flags & 64)),
            "error": value(7, bool(flags & 128)), "error_code_raw": value(28, u16(13)),
        },
        "dispense": {
            "target_ml": value(8, u16(15)), "current_ml": value(9, u16(17)),
            "remaining_ml": value(10, u16(19)), "flow_ml_s": value(11, u16(21) / 100),
            "progress_percent": value(12, u16(23) / 10), "requested_temp_c": value(13, payload[25]),
            "inlet_temp_c": value(14, payload[26]), "outlet_temp_c": value(15, payload[27]),
        },
        "tanks": {
            "internal_min": value(16, bool(tank & 1)), "internal_max": value(17, bool(tank & 2)),
            "external_low": value(18, bool(tank & 4)), "external_raw_available": value(19, bool(tank & 8)),
            "external_stable_available": value(20, bool(tank & 16)), "fill_allowed": value(21, bool(fill & 1)),
            "fill_active": value(22, bool(fill & 2)),
            "external_empty_stop_latched": bool(fill & 8),
            "external_empty_reset_armed": bool(fill & 16),
            "fill_delay_remaining_ms": u32(30),
        },
        "water": {
            "tds_raw": value(29, payload[45]),
            "tds_calculated": value(29, tds_display_value(payload[45])),
            "tds_mapping": TDS_MAPPING_NAME,
            "tds": value(29, payload[45]),  # backward-compatible raw alias
        },
        "electrical": {"ac_mains_v": value(23, payload[34])},
        "statistics": {"total_dispensed_ml": value(24, u32(35)), "current_counter": value(25, u16(39)),
                       "feed_pump_parameter": value(26, u16(41))},
    }


_FRAME_KINDS = {
    0: "UNKNOWN", 1: "A5_REQUEST", 2: "E5_B2_REQUEST", 3: "E5_E1_REQUEST",
    4: "AA00", 5: "AA_A3", 6: "AA_E1", 7: "AA_F1", 8: "A5_RESPONSE",
    9: "E5_B2_RESPONSE", 10: "E5_E1_RESPONSE", 11: "CC", 12: "AA_OTHER",
}


def decode_protocol_frame(payload: bytes) -> dict:
    """Decode a Mega raw-frame stream record without assigning field meanings."""
    if len(payload) < 13 or payload[0] != 1 or len(payload) != 13 + payload[12]:
        raise ValueError("unsupported QG protocol-frame record")
    direction, kind, flags, raw_length = payload[1], payload[2], payload[3], payload[12]
    raw = payload[13:]
    aa = raw_length == 19 and raw[:1] == b"\xAA"
    response = raw_length == 18 and 8 <= kind <= 10
    data_start = 1 if aa else 0
    data = raw[data_start : data_start + 16] if aa or response else b""
    has_checksum = bool(flags & 0x02)
    return {
        "stream_version": 1,
        "direction": "MAIN_TO_DISPLAY" if direction else "DISPLAY_TO_MAIN",
        "frame_type": _FRAME_KINDS.get(kind, "UNKNOWN"),
        "recognized": bool(flags & 0x01),
        "mega_timestamp_us": struct.unpack_from("<I", payload, 4)[0],
        "sequence": struct.unpack_from("<I", payload, 8)[0],
        "raw_hex": raw.hex(" ").upper(),
        "data_offset_base": data_start if data else None,
        "data_dec": list(data),
        "data_hex": [f"{value:02X}" for value in data],
        "data_bin": [f"{value:08b}" for value in data],
        "checksum": {
            "sum16_h": raw[-2] if has_checksum else None,
            "sum16_l": raw[-1] if has_checksum else None,
            "valid": bool(flags & 0x04) if has_checksum else None,
        },
    }
