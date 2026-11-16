"""Expose the evaluated UDP state in Prometheus format for vmagent."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time

import common_service as cs
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily


STATE_PATH = Path(os.getenv("QUELLA_STATE_PATH", "data/live_state.json"))
METRICS_HOST = os.getenv("QUELLA_METRICS_BIND", "0.0.0.0")
METRICS_PORT = int(os.getenv("QUELLA_METRICS_PORT", "8000"))
ONLINE_SECONDS = float(os.getenv("QUELLA_ONLINE_SECONDS", "5"))
SERVICE = "osmofresh-quella-pro"


def numeric(value) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


class StateCollector:
    def collect(self):
        now = time.time()
        load_ok = 0.0
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            load_ok = 1.0
        except (OSError, ValueError, TypeError):
            state = {}

        exporter_ok = GaugeMetricFamily(
            "osmofresh_quella_exporter_state_read_success",
            "Whether the latest evaluated state file could be read",
        )
        exporter_ok.add_metric([], load_ok)
        yield exporter_ok

        receiver = state.get("receiver", {})
        last_packet = receiver.get("last_packet_at")
        packet_age = None if last_packet is None else max(0.0, now - float(last_packet))
        up = GaugeMetricFamily(
            "osmofresh_quella_up",
            "1 when a valid QT UDP packet was received recently",
        )
        up.add_metric([], 1.0 if packet_age is not None and packet_age <= ONLINE_SECONDS else 0.0)
        yield up
        if packet_age is not None:
            age_metric = GaugeMetricFamily(
                "osmofresh_quella_last_packet_age_seconds",
                "Age of the most recently decoded QT UDP packet",
            )
            age_metric.add_metric([], packet_age)
            yield age_metric

        counters = {
            "packets": "packets_total",
            "frames": "frames_total",
            "lost_packets": "lost_packets_total",
            "duplicate_packets": "duplicate_packets_total",
            "out_of_order_packets": "out_of_order_packets_total",
            "malformed_packets": "malformed_packets_total",
            "invalid_protocol_frames": "invalid_protocol_frames_total",
            "rejected_source_packets": "rejected_source_packets_total",
        }
        for metric_name, state_name in counters.items():
            metric = CounterMetricFamily(
                f"osmofresh_quella_{metric_name}",
                f"Quella UDP receiver {metric_name.replace('_', ' ')}",
            )
            metric.add_metric([], float(receiver.get(state_name, 0)))
            yield metric
        for metric_name, state_name in (
            ("mega_dropped_raw_frames", "mega_dropped_raw_frames"),
            ("esp_uptime_seconds", "esp_uptime_ms"),
        ):
            metric = GaugeMetricFamily(
                f"osmofresh_quella_{metric_name}", metric_name.replace("_", " ")
            )
            value = float(receiver.get(state_name, 0))
            metric.add_metric([], value / 1000.0 if state_name.endswith("_ms") else value)
            yield metric

        field_value = GaugeMetricFamily(
            "osmofresh_quella_field_value",
            "Decoded numeric and boolean UART field values",
            labels=["field", "unit", "source"],
        )
        field_available = GaugeMetricFamily(
            "osmofresh_quella_field_available",
            "Whether a decoded UART field currently has a value",
            labels=["field"],
        )
        field_age = GaugeMetricFamily(
            "osmofresh_quella_field_age_seconds",
            "Age of a decoded UART field",
            labels=["field"],
        )
        for name, item in state.get("fields", {}).items():
            available = bool(item.get("available"))
            field_available.add_metric([name], float(available))
            age = numeric(item.get("age_seconds"))
            if age is not None:
                field_age.add_metric([name], age)
            value = numeric(item.get("value"))
            if available and value is not None:
                field_value.add_metric(
                    [name, str(item.get("unit", "")), str(item.get("source", ""))],
                    value,
                )
        yield field_value
        yield field_available
        yield field_age

        # Explicit operational metrics in addition to the generic field
        # series, so dashboards and alerts do not need label matching.
        wifi_rssi = numeric(receiver.get("wifi_rssi_dbm"))
        if wifi_rssi is not None:
            metric = GaugeMetricFamily(
                "osmofresh_quella_wifi_rssi_dbm",
                "ESP8266 WiFi received signal strength in dBm",
            )
            metric.add_metric([], wifi_rssi)
            yield metric
        pump_power = numeric(
            state.get("fields", {}).get("booster_pump_power_w", {}).get("value")
        )
        if pump_power is not None:
            metric = GaugeMetricFamily(
                "osmofresh_quella_booster_pump_power_watt",
                "Booster-pump power from E5 B2 Data[14]; approximately 1 digit per watt",
            )
            metric.add_metric([], pump_power)
            yield metric
            # Deprecated compatibility series. New dashboards should use
            # osmofresh_quella_booster_pump_power_watt.
            metric = GaugeMetricFamily(
                "osmofresh_quella_b2_d14_candidate_raw",
                "Deprecated alias of osmofresh_quella_booster_pump_power_watt",
            )
            metric.add_metric([], pump_power)
            yield metric
        for metric_name, field_name, description in (
            ("external_empty_stop_latched", "external_empty_stop_latched",
             "Whether the external-empty operator-reset stop is latched"),
            ("external_empty_reset_armed", "external_empty_reset_armed",
             "Whether the START acknowledgement was seen and IDLE confirmation is pending"),
        ):
            value = numeric(field_map_item.get("value")) if (
                field_map_item := state.get("fields", {}).get(field_name, {})
            ) else None
            metric = GaugeMetricFamily(f"osmofresh_quella_{metric_name}", description)
            metric.add_metric([], value if value is not None else 0.0)
            yield metric

        # Derived categorical mode for history only. A5 D11.bit4 is
        # intentionally excluded: captures proved it cycles while idle.
        field_map = state.get("fields", {})
        def active(name: str) -> bool:
            item = field_map.get(name, {})
            return bool(item.get("available") and item.get("value"))
        mode_code = 2 if active("dispensing_active") else (
            3 if active("booster_fill_active") else (
                4 if active("flush_active") else (
                    5 if active("heater_900w_active") or active("heater_1200w_active") else 1
                )
            )
        )
        mode = GaugeMetricFamily(
            "osmofresh_quella_operating_mode_code",
            "Derived mode: 1 IDLE, 2 DISPENSING, 3 FILLING, 4 FLUSHING, 5 HEATING",
        )
        mode.add_metric([], mode_code)
        yield mode

        frame_age = GaugeMetricFamily(
            "osmofresh_quella_frame_age_seconds",
            "Age of the latest frame in each direction/type group",
            labels=["direction", "frame"],
        )
        checksum = GaugeMetricFamily(
            "osmofresh_quella_frame_checksum_valid",
            "SUM16/ACK validity of the latest complete frame",
            labels=["direction", "frame"],
        )
        payload_byte = GaugeMetricFamily(
            "osmofresh_quella_payload_byte_value",
            "Raw logical payload byte value for long-term reverse engineering",
            labels=["direction", "frame", "offset"],
        )
        payload_bit = GaugeMetricFamily(
            "osmofresh_quella_payload_bit_value",
            "Raw logical payload bit value for long-term reverse engineering",
            labels=["direction", "frame", "offset", "bit"],
        )
        for item in state.get("frames", {}).values():
            direction = str(item.get("direction", "UNKNOWN"))
            frame = str(item.get("frame_type", "UNKNOWN"))
            age = numeric(item.get("age_seconds"))
            if age is not None:
                frame_age.add_metric([direction, frame], age)
            valid = item.get("checksum_valid")
            if valid is not None:
                checksum.add_metric([direction, frame], float(bool(valid)))
            for offset, value in enumerate(item.get("payload_bytes", [])):
                offset_label = str(offset)
                payload_byte.add_metric([direction, frame, offset_label], float(value))
                for bit in range(8):
                    payload_bit.add_metric(
                        [direction, frame, offset_label, str(bit)],
                        float(bool(value & (1 << bit))),
                    )
        yield frame_age
        yield checksum
        yield payload_byte
        yield payload_bit


def state_pipeline_healthy() -> bool:
    """Service health: receiver keeps producing a readable state snapshot."""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return time.time() - float(state.get("written_at", 0)) <= 5.0
    except (OSError, ValueError, TypeError):
        return False


def main() -> None:
    REGISTRY.register(StateCollector())
    start_http_server(METRICS_PORT, addr=METRICS_HOST)
    cs.start_sync_heartbeat(
        SERVICE,
        interval_s=10.0,
        ok_message="UDP receiver and metrics exporter running",
        ok_http_status=200,
        is_ok=state_pipeline_healthy,
    )
    print(
        f"Quella VictoriaMetrics exporter listening on {METRICS_HOST}:{METRICS_PORT}; "
        f"state={STATE_PATH}",
        flush=True,
    )
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
