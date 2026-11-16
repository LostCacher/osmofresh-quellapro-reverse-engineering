"""Technical live terminal receiver for QT UDP raw telemetry."""

from __future__ import annotations

import argparse
import json
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import socket
import time
from typing import TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from protocol.live_state import QuellaLiveState
from protocol.telemetry import (
    SequenceTracker, TelemetryCrcError, TelemetryDecodeError, TelemetryFrame,
    decode_telemetry, direction_name, frame_type_name, live_state_frame_type,
)


@dataclass
class ReceiverCounters:
    bytes_received: int = 0
    frames_received: int = 0
    malformed_packets: int = 0
    crc_errors: int = 0


class TelemetryReceiver:
    def __init__(self, bind: str = "0.0.0.0", port: int = 42042,
                 *, raw: bool = False, record: Path | None = None) -> None:
        self.bind, self.port, self.raw = bind, port, raw
        self.sequence = SequenceTracker()
        self.counters = ReceiverCounters()
        self.state = QuellaLiveState()
        self.latest: deque[dict] = deque(maxlen=10)
        self.packet_samples: deque[tuple[float, int, int]] = deque()
        self.started = time.monotonic()
        self.last_packet_monotonic: float | None = None
        self.last_esp_uptime_ms: int | None = None
        self.last_mega_batch: int | None = None
        self.last_mega_dropped = 0
        self.batch_frame_total = 0
        if record is not None:
            record.parent.mkdir(parents=True, exist_ok=True)
        self.record_handle: TextIO | None = (
            record.open("a", encoding="utf-8") if record is not None else None
        )
        self.console = Console()

    def close(self) -> None:
        if self.record_handle is not None:
            self.record_handle.close()

    def _observe_frame(self, frame: TelemetryFrame, received_at: float) -> None:
        item = {
            "server_timestamp": datetime.fromtimestamp(received_at).isoformat(timespec="milliseconds"),
            "mega_timestamp_us": frame.timestamp_us,
            "direction": direction_name(frame.direction),
            "frame_type": frame_type_name(frame.frame_type),
            "length": len(frame.raw),
            "raw": frame.raw.hex(" ").upper(),
        }
        self.latest.append(item)
        state_kind = live_state_frame_type(frame.frame_type)
        if state_kind is not None:
            direction = "MAIN_TO_DISPLAY" if frame.direction else "DISPLAY_TO_MAIN"
            self.state.observe_frame(direction, state_kind, frame.raw, received_at)
        if self.raw:
            self.console.print(
                f"{item['server_timestamp'][11:]} mega={frame.timestamp_us:10d} "
                f"{item['direction']:14} {item['frame_type']:15} len={len(frame.raw):2d} {item['raw']}"
            )

    def process_datagram(self, data: bytes, received_at: float | None = None) -> bool:
        received_at = time.time() if received_at is None else received_at
        try:
            packet = decode_telemetry(data)
        except TelemetryCrcError as exc:
            self.counters.crc_errors += 1
            if self.raw:
                self.console.print(f"[red]DROP CRC[/] bytes={len(data)} source=UDP error={exc}")
            return False
        except TelemetryDecodeError as exc:
            self.counters.malformed_packets += 1
            if self.raw:
                preview = data[:32].hex(" ").upper()
                self.console.print(
                    f"[red]DROP MALFORMED[/] bytes={len(data)} error={exc} data={preview}"
                )
            return False
        now = time.monotonic()
        missing = self.sequence.observe(packet.sequence)
        self.counters.bytes_received += len(data)
        self.counters.frames_received += len(packet.frames)
        self.batch_frame_total += len(packet.frames)
        self.last_packet_monotonic = now
        self.last_esp_uptime_ms = packet.esp_uptime_ms
        self.last_mega_batch = packet.mega_batch_sequence
        self.last_mega_dropped = packet.mega_dropped_raw_frames
        self.packet_samples.append((now, len(data), len(packet.frames)))
        for frame in packet.frames:
            self._observe_frame(frame, received_at)
        if self.record_handle is not None:
            record = {
                "record_type": "qt_packet", "server_timestamp": received_at,
                "packet": asdict(packet), "udp_bytes": len(data), "sequence_gap": missing,
            }
            for frame in record["packet"]["frames"]:
                frame["raw"] = frame["raw"].hex(" ").upper()
            self.record_handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            self.record_handle.flush()
        return True

    def rates(self, now: float | None = None) -> tuple[float, float, float]:
        now = time.monotonic() if now is None else now
        while self.packet_samples and now - self.packet_samples[0][0] > 1.0:
            self.packet_samples.popleft()
        return (
            float(len(self.packet_samples)),
            float(sum(item[1] for item in self.packet_samples)),
            float(sum(item[2] for item in self.packet_samples)),
        )

    @staticmethod
    def _value(snapshot: dict, group: str, name: str) -> str:
        field = snapshot[group][name]
        if not field["known"]:
            return "—"
        value = field["value"]
        if isinstance(value, bool):
            value = "YES" if value else "NO"
        elif isinstance(value, float):
            value = f"{value:.2f}"
        elif isinstance(value, list):
            value = ", ".join(map(str, value)) or "NONE"
        elif group == "water_quality" and name == "tds":
            value = f"0x{int(value):02X} ({int(value)} dec)"
        unit = f" {field['unit']}" if field.get("unit") else ""
        return f"{value}{unit} [{field['freshness'].upper()}]"

    def render(self):
        now_mono, now_wall = time.monotonic(), time.time()
        packets_s, bytes_s, frames_s = self.rates(now_mono)
        age = None if self.last_packet_monotonic is None else now_mono - self.last_packet_monotonic
        online = age is not None and age < 2.0
        network = Table.grid(padding=(0, 2))
        network.add_column();network.add_column();network.add_column();network.add_column()
        network.add_row("State", "[green]ONLINE[/]" if online else "[red]STALE/OFFLINE[/]",
                        "Last packet age", "—" if age is None else f"{age:.3f} s")
        network.add_row("Packets RX", str(self.sequence.packets), "Packet loss", str(self.sequence.lost))
        network.add_row("Last sequence", str(self.sequence.last_sequence or "—"), "Packets/s", f"{packets_s:.1f}")
        network.add_row("Frames/s", f"{frames_s:.1f}", "Bytes/s", f"{bytes_s:.0f}")
        network.add_row("Malformed", str(self.counters.malformed_packets), "CRC errors", str(self.counters.crc_errors))
        average = self.batch_frame_total / self.sequence.packets if self.sequence.packets else 0.0
        network.add_row("Average batch", f"{average:.2f}", "Mega raw drops", str(self.last_mega_dropped))
        network.add_row("ESP uptime", "—" if self.last_esp_uptime_ms is None else f"{self.last_esp_uptime_ms} ms",
                        "Transport delay", "not derivable (independent clocks)")

        snapshot = self.state.to_dict(now_wall)
        sections = Table.grid(expand=True)
        sections.add_column(ratio=1);sections.add_column(ratio=1)
        def fields(title: str, group: str, names: tuple[str, ...]) -> Panel:
            table = Table.grid(padding=(0, 1));table.add_column(style="cyan");table.add_column()
            for label, name in names:
                table.add_row(label, self._value(snapshot, group, name))
            return Panel(table, title=title)
        sections.add_row(
            fields("Machine", "machine", (("Mode", "overall_mode"), ("Dispensing", "dispensing_active"),
                   ("Filling", "filling_active"), ("Flushing", "flushing_active"),
                   ("Pump", "booster_pump_active"), ("Errors", "error_codes"))),
            fields("Dispense", "dispense", (("Target", "target_volume_ml"), ("Current", "current_volume_ml"),
                   ("Flow", "flow_rate_ml_min"), ("Progress", "progress_percent"),
                   ("Feed parameter", "feed_pump_parameter_ml_min"))),
        )
        sections.add_row(
            fields("Tanks", "tanks", (("Internal MIN", "internal_min"), ("Internal MAX", "internal_max"),
                   ("External raw", "external_water_raw"), ("External stable", "external_water_stable"),
                   ("Fill active", "fill_active"))),
            fields("Temperatures", "temperature", (("Inlet", "inlet_c"), ("Outlet", "outlet_c"),
                   ("Setpoint", "requested_c"))),
        )
        sections.add_row(
            fields("Water", "water_quality", (("TDS raw", "tds"), ("TDS display", "tds_calculated"))),
            fields("Electrical / Counters", "voltage", (("AC mains", "ac_mains_v"),))
        )
        counters = Table.grid(padding=(0, 1));counters.add_column(style="cyan");counters.add_column()
        for label, name in (("Total dispensed", "total_dispensed_ml"), ("Current counter", "current_amount_counter_ml")):
            counters.add_row(label, self._value(snapshot, "statistics", name))
        counters.add_row("External voltage", "— (no audited protocol field)")
        counters.add_row("Other water quality", self._value(snapshot, "unknown", "water_quality_state"))
        latest = Table("Server time", "Mega µs", "Direction", "Type", "Raw", expand=True)
        for item in reversed(self.latest):
            latest.add_row(item["server_timestamp"][11:], str(item["mega_timestamp_us"]),
                           item["direction"], item["frame_type"], item["raw"])
        return Group(Panel(network, title="Network"), sections,
                     Panel(counters, title="Counters"), Panel(latest, title="Latest Frames"))

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.bind, self.port));sock.settimeout(0.2)
        self.console.print(f"Listening for QT UDP telemetry on {self.bind}:{self.port}")
        try:
            if self.raw:
                last_wait_message = time.monotonic()
                while True:
                    try:
                        data, ignored = sock.recvfrom(2048)
                    except socket.timeout:
                        now = time.monotonic()
                        if now - last_wait_message >= 2.0:
                            age = "never" if self.last_packet_monotonic is None else f"{now-self.last_packet_monotonic:.1f}s"
                            self.console.print(
                                f"WAITING UDP {self.bind}:{self.port} | last_valid={age} "
                                f"malformed={self.counters.malformed_packets} crc={self.counters.crc_errors}"
                            )
                            last_wait_message = now
                        continue
                    self.process_datagram(data)
            else:
                with Live(self.render(), console=self.console, refresh_per_second=5, screen=True) as live:
                    while True:
                        try:
                            data, ignored = sock.recvfrom(2048)
                            self.process_datagram(data)
                        except socket.timeout:
                            pass
                        live.update(self.render())
        except KeyboardInterrupt:
            pass
        finally:
            sock.close();self.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Quella QT UDP telemetry receiver")
    parser.add_argument("--bind", default="0.0.0.0", help="UDP bind address")
    parser.add_argument("--port", type=int, default=42042, help="QT UDP port")
    parser.add_argument("--raw", "--raw-log", action="store_true", help="print one line per decoded frame")
    parser.add_argument("--record", type=Path, help="append packets and frames as JSONL")
    args = parser.parse_args()
    TelemetryReceiver(args.bind, args.port, raw=args.raw, record=args.record).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
