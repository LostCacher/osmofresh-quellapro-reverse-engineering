"""Passive WebSocket capture transport for the ESP8266 gateway."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QThread, Signal


_KIND_MAP = {
    "A5_REQUEST": "A5",
    "A5_RESPONSE": "A5",
    "E5_B2_REQUEST": "E5 B2",
    "E5_B2_RESPONSE": "E5 B2",
    "E5_E1_REQUEST": "E5 E1",
    "E5_E1_RESPONSE": "E5 E1",
    "AA00": "AA",
    "AA_A3": "AA",
    "AA_E1": "AA",
    "AA_F1": "AA",
    "AA_OTHER": "AA",
    "CC": "CC",
}


def _decode_gateway_frame(data: dict) -> tuple[str, str, bytes, bool | None]:
    direction = data.get("direction")
    if direction not in ("DISPLAY_TO_MAIN", "MAIN_TO_DISPLAY"):
        raise ValueError("protocol_frame has an invalid direction")
    raw_text = data.get("raw_hex")
    if not isinstance(raw_text, str):
        raise ValueError("protocol_frame has no raw_hex field")
    raw = bytes.fromhex(raw_text)
    kind = _KIND_MAP.get(str(data.get("frame_type", "UNKNOWN")), "UNKNOWN")
    checksum = data.get("checksum")
    valid = checksum.get("valid") if isinstance(checksum, dict) else None
    if valid not in (True, False):
        valid = None
    return direction, kind, raw, valid


def decode_gateway_ws_messages(text: str) -> list[tuple[str, str, bytes, bool | None]]:
    """Return every exact frame carried by one gateway WebSocket message."""
    message = json.loads(text)
    data = message.get("data")
    if message.get("type") == "protocol_frame" and isinstance(data, dict):
        return [_decode_gateway_frame(data)]
    if message.get("type") == "protocol_frame_batch" and isinstance(data, dict):
        frames = data.get("frames")
        if not isinstance(frames, list):
            raise ValueError("protocol_frame_batch has no frames array")
        return [_decode_gateway_frame(frame) for frame in frames if isinstance(frame, dict)]
    return []


def decode_gateway_ws_message(text: str) -> tuple[str, str, bytes, bool | None] | None:
    """Backward-compatible decoder for a message containing exactly one frame."""
    frames = decode_gateway_ws_messages(text)
    return frames[0] if len(frames) == 1 else None


class NetworkCaptureWorker(QThread):
    """Minimal dependency-free RFC6455 client; receives gateway text frames only."""

    connected = Signal()
    disconnected = Signal()
    failure = Signal(str)
    raw_received = Signal(bytes, float)
    protocol_frame = Signal(str, str, bytes, float)
    network_frame = Signal(str, str, bytes, object, float)

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url if "://" in url else f"ws://{url}"
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True


    @staticmethod
    def _masked_control_frame(opcode: int, payload: bytes = b"") -> bytes:
        mask = os.urandom(4)
        return bytes((0x80 | opcode, 0x80 | len(payload))) + mask + bytes(
            value ^ mask[index & 3] for index, value in enumerate(payload)
        )

    def run(self) -> None:
        connection: socket.socket | None = None
        try:
            target = urlsplit(self.url)
            if target.scheme != "ws" or not target.hostname:
                raise ValueError("Use a ws://host:port URL (wss:// is not supported by the ESP gateway)")
            port = target.port or 81
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            connection = socket.create_connection((target.hostname, port), timeout=4.0)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\nHost: {target.hostname}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            connection.sendall(request.encode("ascii"))
            received = bytearray()
            while b"\r\n\r\n" not in received:
                chunk = connection.recv(2048)
                if not chunk or len(received) > 16384:
                    raise ConnectionError("incomplete WebSocket handshake")
                received.extend(chunk)
            header, buffered = bytes(received).split(b"\r\n\r\n", 1)
            status_line = header.split(b"\r\n", 1)[0]
            if b" 101 " not in status_line:
                raise ConnectionError(status_line.decode("latin1", "replace"))
            expected_accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
            )
            if expected_accept.lower() not in header.lower():
                raise ConnectionError("WebSocket handshake accept key mismatch")
            connection.settimeout(0.25)
            pending = bytearray(buffered)
            self.connected.emit()

            def read_exact(count: int) -> bytes:
                while len(pending) < count:
                    try:
                        chunk = connection.recv(max(2048, count - len(pending)))
                    except socket.timeout:
                        # A timeout in the middle of a WebSocket frame must not
                        # discard the already consumed header. Keep the current
                        # parse state until the remaining bytes arrive.
                        if self._stop_requested:
                            raise InterruptedError
                        continue
                    if not chunk:
                        raise ConnectionError("gateway closed the WebSocket")
                    pending.extend(chunk)
                value = bytes(pending[:count])
                del pending[:count]
                return value

            fragmented = bytearray()
            fragmented_opcode: int | None = None
            while not self._stop_requested:
                first, second = read_exact(2)
                final = bool(first & 0x80)
                opcode, masked = first & 0x0F, bool(second & 0x80)
                if first & 0x70:
                    raise ValueError("unsupported WebSocket RSV flags")
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack(">H", read_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", read_exact(8))[0]
                if length > 65536:
                    raise ValueError("WebSocket message exceeds 64 KiB")
                mask = read_exact(4) if masked else b""
                payload = read_exact(length)
                if masked:
                    payload = bytes(value ^ mask[index & 3] for index, value in enumerate(payload))
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    connection.sendall(self._masked_control_frame(0xA, payload[:125]))
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2) and not final:
                    fragmented_opcode = opcode
                    fragmented = bytearray(payload)
                    continue
                if opcode == 0x0:
                    if fragmented_opcode is None:
                        raise ValueError("unexpected WebSocket continuation frame")
                    fragmented.extend(payload)
                    if len(fragmented) > 65536:
                        raise ValueError("fragmented WebSocket message exceeds 64 KiB")
                    if not final:
                        continue
                    opcode, payload = fragmented_opcode, bytes(fragmented)
                    fragmented_opcode, fragmented = None, bytearray()
                if opcode != 0x1:
                    continue
                decoded_frames = decode_gateway_ws_messages(payload.decode("utf-8"))
                if not decoded_frames:
                    continue
                received_at = time.time()
                for direction, kind, raw, valid in decoded_frames:
                    self.raw_received.emit(raw, received_at)
                    self.network_frame.emit(direction, kind, raw, valid, received_at)
                    self.protocol_frame.emit(direction, kind, raw, received_at)
        except Exception as exc:
            if not self._stop_requested:
                self.failure.emit(f"Network capture connection lost: {exc}")
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
            self.disconnected.emit()
