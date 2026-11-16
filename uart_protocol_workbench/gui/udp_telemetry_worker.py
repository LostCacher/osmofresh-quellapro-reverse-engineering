"""Passive Qt worker for binary QT UDP raw telemetry."""

from __future__ import annotations

import socket
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QThread, Signal

from protocol.checksum import validate_aa_frame, validate_response
from protocol.telemetry import (
    TelemetryDecodeError,
    decode_telemetry,
    direction_name,
)


_KIND_BY_ID = {
    1: "A5",
    2: "E5 B2",
    3: "E5 E1",
    4: "AA",
    5: "AA",
    6: "AA",
    7: "AA",
    8: "A5",
    9: "E5 B2",
    10: "E5 E1",
    11: "CC",
    12: "AA",
}


def parse_udp_endpoint(endpoint: str) -> tuple[str, int]:
    text = endpoint.strip()
    if not text:
        raise ValueError("Enter a UDP bind address, for example 0.0.0.0:42042")
    target = urlsplit(text if "://" in text else f"udp://{text}")
    if target.scheme != "udp":
        raise ValueError("UDP telemetry endpoint must use udp://")
    host = target.hostname or "0.0.0.0"
    try:
        port = target.port
    except ValueError as exc:
        raise ValueError("UDP telemetry port must be an integer") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("UDP telemetry port must be between 1 and 65535")
    return host, port


def frame_checksum_valid(kind: str, raw: bytes) -> bool | None:
    if kind in ("A5", "E5 B2", "E5 E1") and len(raw) == 18:
        return validate_response(raw)
    if kind == "AA" and len(raw) == 19:
        return validate_aa_frame(raw)
    if kind == "CC":
        return raw == b"\xCC"
    return None


class UdpTelemetryWorker(QThread):
    """Receive QT packets without exposing any transmit/control operation."""

    connected = Signal()
    disconnected = Signal()
    failure = Signal(str)
    raw_received = Signal(bytes, float)
    protocol_frame = Signal(str, str, bytes, float)
    network_frame = Signal(str, str, bytes, object, float)
    telemetry_statistics = Signal(dict)

    def __init__(self, endpoint: str) -> None:
        super().__init__()
        self.endpoint = endpoint
        self._stop_requested = False
        self._socket: socket.socket | None = None

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        packets = frames = malformed = 0
        last_sequence: int | None = None
        lost = 0
        try:
            host, port = parse_udp_endpoint(self.endpoint)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket = sock
            sock.bind((host, port))
            sock.settimeout(0.25)
            self.connected.emit()
            while not self._stop_requested:
                try:
                    datagram, ignored_address = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                received_at = time.time()
                try:
                    packet = decode_telemetry(datagram)
                except TelemetryDecodeError:
                    malformed += 1
                    self.telemetry_statistics.emit({
                        "packets": packets, "frames": frames, "lost": lost,
                        "malformed": malformed, "last_sequence": last_sequence,
                    })
                    continue
                packets += 1
                if last_sequence is not None:
                    delta = (packet.sequence - last_sequence) & 0xFFFFFFFF
                    if 1 < delta < 0x80000000:
                        lost += delta - 1
                if (
                    last_sequence is None
                    or 0
                    < ((packet.sequence - last_sequence) & 0xFFFFFFFF)
                    < 0x80000000
                ):
                    last_sequence = packet.sequence
                for frame in packet.frames:
                    kind = _KIND_BY_ID.get(frame.frame_type, "UNKNOWN")
                    direction = direction_name(frame.direction).replace("->", "_TO_")
                    raw = bytes(frame.raw)
                    valid = frame_checksum_valid(kind, raw)
                    self.raw_received.emit(raw, received_at)
                    self.network_frame.emit(direction, kind, raw, valid, received_at)
                    self.protocol_frame.emit(direction, kind, raw, received_at)
                    frames += 1
                self.telemetry_statistics.emit({
                    "packets": packets, "frames": frames, "lost": lost,
                    "malformed": malformed, "last_sequence": last_sequence,
                    "mega_dropped": packet.mega_dropped_raw_frames,
                    "esp_uptime_ms": packet.esp_uptime_ms,
                })
        except Exception as exc:
            if not self._stop_requested:
                self.failure.emit(f"UDP telemetry connection lost: {exc}")
        finally:
            if self._socket is not None:
                try:
                    self._socket.close()
                except OSError:
                    pass
                self._socket = None
            self.disconnected.emit()
