"""Passive Workbench transport polling the homeserver's read-only state API."""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from PySide6.QtCore import QThread, Signal


_KIND_MAP = {
    "A5_REQUEST": "A5",
    "E5_B2_REQUEST": "E5 B2",
    "E5_E1_REQUEST": "E5 E1",
    "A5": "A5",
    "E5_B2": "E5 B2",
    "E5_E1": "E5 E1",
    "AA00": "AA",
    "AA_A3": "AA",
    "AA_E1": "AA",
    "AA_F1": "AA",
    "AA_OTHER": "AA",
    "CC": "CC",
}


def normalize_state_url(url: str) -> str:
    text = url.strip()
    if not text:
        raise ValueError("Enter the homeserver state URL")
    if "://" not in text:
        text = "https://" + text
    target = urlsplit(text)
    if target.scheme not in ("http", "https") or not target.hostname:
        raise ValueError("Use an http:// or https:// homeserver URL")
    path = target.path.rstrip("/")
    if not path:
        path = "/state"
    return urlunsplit((target.scheme, target.netloc, path, target.query, ""))


def decode_state_frames(snapshot: dict) -> list[tuple[str, str, bytes, bool | None, float, str]]:
    decoded = []
    frames = snapshot.get("frames", {})
    if not isinstance(frames, dict):
        raise ValueError("homeserver snapshot has no frames object")
    for key, item in frames.items():
        if not isinstance(item, dict):
            continue
        direction = item.get("direction")
        if direction not in ("DISPLAY_TO_MAIN", "MAIN_TO_DISPLAY"):
            continue
        kind = _KIND_MAP.get(str(item.get("frame_type", "")))
        if kind is None:
            continue
        raw_values = item.get("raw_bytes")
        if isinstance(raw_values, list):
            try:
                raw = bytes(int(value) for value in raw_values)
            except (TypeError, ValueError):
                continue
        else:
            try:
                raw = bytes.fromhex(str(item.get("raw_hex", "")))
            except ValueError:
                continue
        valid = item.get("checksum_valid")
        if valid not in (True, False):
            valid = None
        received_at = float(item.get("received_at", snapshot.get("written_at", time.time())))
        decoded.append((direction, kind, raw, valid, received_at, str(key)))
    return decoded


def state_statistics(snapshot: dict) -> dict:
    receiver = snapshot.get("receiver", {})
    return {
        "packets": int(receiver.get("packets_total", 0)),
        "frames": int(receiver.get("frames_total", 0)),
        "lost": int(receiver.get("lost_packets_total", 0)),
        "malformed": int(receiver.get("malformed_packets_total", 0)),
        "last_sequence": receiver.get("last_sequence"),
        "mega_dropped": int(receiver.get("mega_dropped_raw_frames", 0)),
        "esp_uptime_ms": int(receiver.get("esp_uptime_ms", 0)),
    }


class HomeServerStateWorker(QThread):
    """Poll snapshots at 5 Hz and emit new latest frames through normal GUI signals."""

    connected = Signal()
    disconnected = Signal()
    failure = Signal(str)
    raw_received = Signal(bytes, float)
    protocol_frame = Signal(str, str, bytes, float)
    network_frame = Signal(str, str, bytes, object, float)
    telemetry_statistics = Signal(dict)

    def __init__(self, url: str, poll_interval: float = 0.2) -> None:
        super().__init__()
        self.url = normalize_state_url(url)
        self.poll_interval = max(0.1, float(poll_interval))
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        seen: dict[str, float] = {}
        connected = False
        last_success = time.monotonic()
        try:
            while not self._stop_requested:
                started = time.monotonic()
                try:
                    request = Request(
                        self.url,
                        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
                    )
                    with urlopen(request, timeout=2.0) as response:
                        snapshot = json.loads(response.read())
                    if not isinstance(snapshot, dict):
                        raise ValueError("homeserver returned no JSON object")
                    last_success = time.monotonic()
                    if not connected:
                        connected = True
                        self.connected.emit()
                    for direction, kind, raw, valid, timestamp, key in decode_state_frames(snapshot):
                        if timestamp <= seen.get(key, -1.0):
                            continue
                        seen[key] = timestamp
                        self.raw_received.emit(raw, timestamp)
                        self.network_frame.emit(direction, kind, raw, valid, timestamp)
                        self.protocol_frame.emit(direction, kind, raw, timestamp)
                    self.telemetry_statistics.emit(state_statistics(snapshot))
                except Exception as exc:
                    if time.monotonic() - last_success >= 5.0:
                        raise ConnectionError(f"homeserver state unavailable: {exc}") from exc
                remaining = self.poll_interval - (time.monotonic() - started)
                if remaining > 0:
                    self.msleep(round(remaining * 1000))
        except Exception as exc:
            if not self._stop_requested:
                self.failure.emit(f"Homeserver connection lost: {exc}")
        finally:
            self.disconnected.emit()
