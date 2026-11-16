"""Receive, validate and evaluate Quella QT UDP telemetry."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import time

from quella_state import QuellaState
from telemetry_protocol import TelemetryDecodeError, decode_telemetry, sum16_valid


BIND_HOST = os.getenv("QUELLA_UDP_BIND", "0.0.0.0")
BIND_PORT = int(os.getenv("QUELLA_UDP_PORT", "42042"))
EXPECTED_SOURCE = os.getenv("QUELLA_EXPECTED_SOURCE", "").strip()
STATE_PATH = Path(os.getenv("QUELLA_STATE_PATH", "data/live_state.json"))
WRITE_INTERVAL = max(0.05, float(os.getenv("QUELLA_STATE_WRITE_INTERVAL", "0.2")))
LOG_INTERVAL = max(1.0, float(os.getenv("QUELLA_LOG_INTERVAL", "10")))


running = True


def stop(_signum, _frame) -> None:
    global running
    running = False


def write_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def observe_sequence(stats: dict, sequence: int) -> None:
    previous = stats.get("last_sequence")
    stats["packets_total"] += 1
    if previous is None:
        stats["last_sequence"] = sequence
        return
    delta = (sequence - previous) & 0xFFFFFFFF
    if delta == 0:
        stats["duplicate_packets_total"] += 1
    elif delta < 0x80000000:
        stats["lost_packets_total"] += delta - 1
        stats["last_sequence"] = sequence
    else:
        stats["out_of_order_packets_total"] += 1


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state = QuellaState()
    stats = {
        "started_at": time.time(),
        "last_packet_at": None,
        "last_sequence": None,
        "packets_total": 0,
        "frames_total": 0,
        "lost_packets_total": 0,
        "duplicate_packets_total": 0,
        "out_of_order_packets_total": 0,
        "malformed_packets_total": 0,
        "invalid_protocol_frames_total": 0,
        "rejected_source_packets_total": 0,
        "mega_dropped_raw_frames": 0,
        "esp_uptime_ms": 0,
        "wifi_rssi_dbm": None,
    }
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    sock.bind((BIND_HOST, BIND_PORT))
    sock.settimeout(0.25)
    print(
        f"Quella QT UDP receiver listening on {BIND_HOST}:{BIND_PORT}; "
        f"state={STATE_PATH}; source={EXPECTED_SOURCE or 'any'}",
        flush=True,
    )
    last_write = last_log = 0.0
    write_snapshot(STATE_PATH, state.snapshot(stats))
    try:
        while running:
            now = time.time()
            try:
                datagram, address = sock.recvfrom(65535)
            except socket.timeout:
                datagram = None
                address = None
            if datagram is not None and address is not None:
                if EXPECTED_SOURCE and address[0] != EXPECTED_SOURCE:
                    stats["rejected_source_packets_total"] += 1
                else:
                    try:
                        packet = decode_telemetry(datagram)
                    except TelemetryDecodeError as exc:
                        stats["malformed_packets_total"] += 1
                        print(f"Malformed QT packet from {address[0]}: {exc}", flush=True)
                    else:
                        observe_sequence(stats, packet.sequence)
                        stats["last_packet_at"] = now
                        stats["frames_total"] += len(packet.frames)
                        stats["mega_dropped_raw_frames"] = packet.mega_dropped_raw_frames
                        stats["esp_uptime_ms"] = packet.esp_uptime_ms
                        stats["wifi_rssi_dbm"] = packet.wifi_rssi_dbm
                        for frame in packet.frames:
                            if sum16_valid(frame) is False:
                                stats["invalid_protocol_frames_total"] += 1
                            state.observe(frame, now)
            now = time.time()
            if now - last_write >= WRITE_INTERVAL:
                write_snapshot(STATE_PATH, state.snapshot(stats, now))
                last_write = now
            if now - last_log >= LOG_INTERVAL:
                age = None if stats["last_packet_at"] is None else now - stats["last_packet_at"]
                print(
                    "QT stats: "
                    f"packets={stats['packets_total']} frames={stats['frames_total']} "
                    f"lost={stats['lost_packets_total']} malformed={stats['malformed_packets_total']} "
                    f"last_age={'never' if age is None else f'{age:.2f}s'}",
                    flush=True,
                )
                last_log = now
    finally:
        write_snapshot(STATE_PATH, state.snapshot(stats))
        sock.close()


if __name__ == "__main__":
    main()
