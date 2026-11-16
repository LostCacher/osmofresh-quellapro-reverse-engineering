"""Compact periodically rendered view of QuellaLiveState."""

from __future__ import annotations

import json
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QFormLayout, QGridLayout, QGroupBox, QHeaderView, QLabel, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from protocol.live_state import QuellaLiveState


GROUP_LABELS = {
    "machine": "Machine state", "tanks": "Water / tanks", "dispense": "Dispense live values",
    "temperature": "Temperatures", "water_quality": "TDS / water quality",
    "voltage": "Electrical", "statistics": "Counters / statistics",
    "unknown": "Unknown / not yet mapped",
}

FIELD_LABELS = {
    "overall_mode": "Mode / process", "requested_process": "Requested process", "a5_d11_bit4_cyclic": "A5 D11.bit4 (cyclic)", "dispensing_active": "Dispensing",
    "filling_active": "Filling", "flushing_active": "Flushing", "heating_active": "Heating",
    "heater_900w": "900 W control", "heater_1200w": "1200 W control",
    "booster_pump_active": "Booster pump", "feed_pump_active": "Feed pump active",
    "wastewater_flush_valve": "Wastewater valve", "other_valve_states": "Other valves",
    "error_state": "Error", "error_codes": "Error codes", "communication_state": "Communication",
    "session_origin": "Session origin", "override_target_ml": "Override target",
    "remote_target_ml": "Remote target", "internal_min": "Internal tank MIN",
    "internal_max": "Internal tank MAX", "external_low": "External tank LOW",
    "external_water_raw": "External water raw", "external_water_stable": "External water stable",
    "external_delay_pending": "External delay pending", "external_delay_remaining_ms": "Delay remaining",
    "fill_allowed": "Fill allowed", "fill_active": "Fill active", "last_b2_age_ms": "Last valid B2 age",
    "requested_volume_ml": "Requested volume", "current_volume_ml": "Current volume",
    "target_volume_ml": "Target volume", "remaining_volume_ml": "Remaining volume",
    "flow_rate_ml_min": "Derived flow rate", "flow_pulse_counter": "Flow/pulse counter",
    "progress_percent": "Progress", "elapsed_seconds": "Elapsed", "aa_control_d1": "AA control D1",
    "a5_dispensing": "Main A5 dispensing", "feed_pump_parameter_ml_min": "Feed-pump parameter",
    "inlet_c": "Inlet", "outlet_c": "Outlet", "requested_c": "Requested/setpoint",
    "heating_level_raw": "Heating level raw", "secondary_temperature_raw": "Secondary temperature",
    "tds": "TDS raw", "tds_calculated": "TDS calculated / display",
    "water_quality_state": "Water-quality state", "filter_counter": "Filter counter",
    "ac_mains_v": "AC mains voltage", "total_dispensed_ml": "Total dispensed amount",
    "current_amount_counter_ml": "Current amount counter", "fill_counter": "Fill counter",
    "flush_counter": "Flush counter", "pump_runtime": "Pump runtime", "valve_runtime": "Valve runtime",
    "dispense_count": "Dispense count", "lifetime_counter": "Lifetime counter",
    "error_counter": "Error counter",
}


class LiveStatusTab(QWidget):
    def __init__(self, state: QuellaLiveState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.value_labels: dict[str, QLabel] = {}
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        grid = QGridLayout(content)
        for index, (group, names) in enumerate(QuellaLiveState.GROUP_FIELDS.items()):
            box = QGroupBox(GROUP_LABELS[group])
            form = QFormLayout(box)
            for name in names:
                label = QLabel("UNKNOWN")
                label.setTextInteractionFlags(label.textInteractionFlags())
                self.value_labels[name] = label
                form.addRow(FIELD_LABELS.get(name, name), label)
            grid.addWidget(box, index // 2, index % 2)
        raw_box = QGroupBox("Raw protocol states")
        raw_layout = QVBoxLayout(raw_box)
        self.raw_table = QTableWidget(6, 5)
        self.raw_table.setHorizontalHeaderLabels(["Frame", "Age", "Checksum", "Raw frame", "Decoded fields"])
        self.raw_table.verticalHeader().setVisible(False)
        header = self.raw_table.horizontalHeader()
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.raw_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        raw_layout.addWidget(self.raw_table)
        grid.addWidget(raw_box, 4, 0, 1, 2)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    @staticmethod
    def _display(name: str, field: dict) -> str:
        if not field["known"]:
            return "UNKNOWN" if field.get("confidence") == "UNKNOWN" else "—"
        value = field["value"]
        if isinstance(value, bool):
            if name == "internal_min":
                value_text = "BELOW LOW" if value else "ABOVE LOW"
            elif name == "internal_max":
                # Presentation polarity only: the controller keeps its existing
                # internal sensor logic, while the UI describes the tank level.
                value_text = "BELOW MAX" if value else "FULL / MAX REACHED"
            elif name == "external_low":
                value_text = "EMPTY" if value else "AVAILABLE"
            elif name in ("external_water_raw", "external_water_stable"):
                value_text = "AVAILABLE" if value else "NOT AVAILABLE / BLOCKED"
            elif name in ("fill_allowed",):
                value_text = "YES" if value else "NO"
            elif name == "external_delay_pending":
                value_text = "PENDING" if value else "NO"
            else:
                value_text = "ACTIVE" if value else "INACTIVE"
        elif isinstance(value, float):
            value_text = f"{value:.2f}"
        elif isinstance(value, list):
            value_text = ", ".join(map(str, value)) or "NONE"
        elif name == "tds":
            value_text = f"0x{int(value):02X} ({int(value)} dec)"
        else:
            value_text = str(value)
        unit = f" {field['unit']}" if field.get("unit") else ""
        age = "—" if field["age_ms"] is None else f"{field['age_ms']} ms"
        raw = "" if field.get("raw") is None else f" | raw={field['raw']}"
        return f"{value_text}{unit}  [{field['freshness'].upper()}, {age}, {field['source_frame']}]{raw}"

    @staticmethod
    def _style(name: str, field: dict) -> str:
        if not field["known"] or field["freshness"] == "expired":
            return "color:#888888"
        if field["freshness"] == "stale":
            return "color:#d7b84b"
        value = field["value"]
        if name in ("external_low", "error_state") and value is True:
            return "color:#d65c5c;font-weight:bold"
        if name in ("external_water_stable", "fill_allowed") and value is False:
            return "color:#d65c5c"
        if name == "external_delay_pending" and value is True:
            return "color:#d7b84b;font-weight:bold"
        if value is True:
            return "color:#55c57a"
        if value in ("ERROR", "CHECKSUM ERROR"):
            return "color:#d65c5c;font-weight:bold"
        return ""

    def refresh(self) -> None:
        snapshot = self.state.to_dict(time.time())
        for group in QuellaLiveState.GROUP_FIELDS:
            for name, field in snapshot[group].items():
                label = self.value_labels[name]
                label.setText(self._display(name, field))
                label.setStyleSheet(self._style(name, field))
                label.setToolTip(field.get("note", "") + f"\nConfidence: {field['confidence']}")
        for row, (name, frame) in enumerate(snapshot["raw_protocol"].items()):
            values = (
                name,
                "—" if frame["age_ms"] is None else f"{frame['age_ms']} ms ({frame['freshness']})",
                "—" if frame["checksum_valid"] is None else ("VALID" if frame["checksum_valid"] else "INVALID"),
                frame["raw"] or "—",
                json.dumps(frame["decoded"], ensure_ascii=False, sort_keys=True),
            )
            for column, value in enumerate(values):
                self.raw_table.setItem(row, column, QTableWidgetItem(str(value)))
