"""Controls and trace view for the display-emulator role."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from protocol.display_role import (
    AAPayloadPreset,
    AutoAAMode,
    DEFAULT_AA_COMMAND_PRESETS_PATH,
    DisplayRequest,
    DisplayRoleConfig,
    load_aa_command_presets,
    save_aa_command_presets,
)


LIVE_VALUE_FORMATS = ("Decimal", "Signed decimal", "Hex", "Binary", "Boolean")
LIVE_VALUE_CATALOG = {
    "a5_outlet_temperature": {
        "label": "Outlet temperature",
        "request": "A5",
        "offset": 2,
        "length": 1,
        "unit": "°C",
    },
    "a5_inlet_temperature": {
        "label": "Inlet temperature",
        "request": "A5",
        "offset": 12,
        "length": 1,
        "unit": "°C",
    },
    "a5_ac_mains_voltage": {
        "label": "AC mains voltage",
        "request": "A5",
        "offset": 14,
        "length": 1,
        "unit": "V",
    },
    "a5_current_dispensed_volume": {
        "label": "Current dispensed volume",
        "request": "A5",
        "offset": 3,
        "length": 2,
        "unit": "ml",
    },
    "a5_internal_below_low": {
        "label": "Internal tank BELOW LOW (A5)",
        "request": "A5",
        "offset": 11,
        "length": 1,
        "bit": 0,
        "unit": "",
    },
    "a5_total_dispensed_volume": {
        "label": "Total dispensed volume counter",
        "request": "A5",
        "offset": 5,
        "length": 4,
        "unit": "ml",
    },
    "e1_flow_parameter": {
        "label": "E1 feed-pump parameter",
        "request": "E5 E1",
        "offset": 1,
        "length": 2,
        "unit": "ml/min",
    },
    "b2_external_available": {
        "label": "External tank water available",
        "request": "E5 B2",
        "offset": 4,
        "length": 1,
        "bit": 0,
        "unit": "",
    },
    "b2_internal_low_sensor": {
        "label": "Internal tank BELOW LOW",
        "request": "E5 B2",
        "offset": 4,
        "length": 1,
        "bit": 3,
        "unit": "",
    },
    "b2_internal_high_sensor": {
        "label": "Internal HIGH sensor state",
        "request": "E5 B2",
        "offset": 4,
        "length": 1,
        "bit": 5,
        "unit": "",
    },
}


class DisplayEmulatorTab(QWidget):
    config_changed = Signal(object)
    start_requested = Signal()
    stop_requested = Signal()
    step_requested = Signal()
    send_requested = Signal(str)
    send_aa_preset_requested = Signal(int)
    abort_requested = Signal()

    def __init__(
        self,
        preset_path: Path | str = DEFAULT_AA_COMMAND_PRESETS_PATH,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.preset_path = Path(preset_path)
        self._updating = False
        self._last_display_state: dict = {}
        self.trace_events: list[dict] = []
        self.request_enabled: dict[DisplayRequest, QCheckBox] = {}
        self.request_delay: dict[DisplayRequest, QSpinBox] = {}
        self._build_ui()
        initial_config = DisplayRoleConfig()
        initial_config.aa_presets = load_aa_command_presets(self.preset_path)
        self.load_config(initial_config)
        self.set_connected(False)

    def _build_live_values_group(self) -> QGroupBox:
        group = QGroupBox("Selected decoded response values")
        layout = QVBoxLayout(group)
        help_label = QLabel(
            "Choose a known field, display format and unit. Values update from the latest checksum-valid response."
        )
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self.live_value_table = QTableWidget(0, 6)
        self.live_value_table.setHorizontalHeaderLabels(
            ["Field", "Format", "Unit", "Live value", "Source bytes", "Age"]
        )
        self.live_value_table.verticalHeader().hide()
        self.live_value_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.live_value_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.live_value_table.setColumnWidth(1, 135)
        self.live_value_table.setColumnWidth(2, 100)
        self.live_value_table.setColumnWidth(3, 150)
        self.live_value_table.setColumnWidth(4, 150)
        self.live_value_table.setColumnWidth(5, 100)
        buttons = QHBoxLayout()
        add_button = QPushButton("Add live field")
        add_button.clicked.connect(self._add_live_value_field)
        remove_button = QPushButton("Remove selected field")
        remove_button.clicked.connect(self._remove_live_value_field)
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addStretch()
        layout.addWidget(self.live_value_table)
        layout.addLayout(buttons)
        return group

    def _append_live_value_field(self, definition: dict) -> None:
        row = self.live_value_table.rowCount()
        self.live_value_table.insertRow(row)
        field_box = QComboBox()
        for key, catalog_entry in LIVE_VALUE_CATALOG.items():
            field_box.addItem(
                f"{catalog_entry['label']} — {catalog_entry['request']} D{catalog_entry['offset']}",
                key,
            )
        field_index = field_box.findData(definition.get("field"))
        field_box.setCurrentIndex(max(0, field_index))
        field_box.currentIndexChanged.connect(self._live_value_definition_changed)
        format_box = QComboBox()
        format_box.addItems(LIVE_VALUE_FORMATS)
        format_box.setCurrentText(str(definition.get("format", "Decimal")))
        format_box.currentTextChanged.connect(self._live_value_definition_changed)
        selected_key = field_box.currentData()
        default_unit = LIVE_VALUE_CATALOG[selected_key]["unit"]
        unit_edit = QLineEdit(str(definition.get("unit", default_unit)))
        unit_edit.editingFinished.connect(self._live_value_definition_changed)
        self.live_value_table.setCellWidget(row, 0, field_box)
        self.live_value_table.setCellWidget(row, 1, format_box)
        self.live_value_table.setCellWidget(row, 2, unit_edit)
        for column in (3, 4, 5):
            item = QTableWidgetItem("—")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.live_value_table.setItem(row, column, item)

    def _load_live_value_fields(self, definitions: list[dict]) -> None:
        self.live_value_table.setRowCount(0)
        for definition in definitions:
            if definition.get("field") in LIVE_VALUE_CATALOG:
                self._append_live_value_field(definition)
        self._refresh_live_values()

    def _add_live_value_field(self) -> None:
        self._append_live_value_field(
            {"field": "a5_outlet_temperature", "format": "Decimal", "unit": "°C"}
        )
        self.live_value_table.selectRow(self.live_value_table.rowCount() - 1)
        self._configuration_changed()
        self._refresh_live_values()

    def _remove_live_value_field(self) -> None:
        row = self.live_value_table.currentRow()
        if row < 0:
            return
        self.live_value_table.removeRow(row)
        self._configuration_changed()

    def _live_value_definition_changed(self, ignored=None) -> None:
        if self._updating:
            return
        sender = self.sender()
        for row in range(self.live_value_table.rowCount()):
            field_box = self.live_value_table.cellWidget(row, 0)
            if sender is field_box:
                unit_edit = self.live_value_table.cellWidget(row, 2)
                unit_edit.setText(LIVE_VALUE_CATALOG[field_box.currentData()]["unit"])
                break
        self._configuration_changed()
        self._refresh_live_values()

    def _live_value_definitions(self) -> list[dict]:
        result = []
        for row in range(self.live_value_table.rowCount()):
            result.append(
                {
                    "field": self.live_value_table.cellWidget(row, 0).currentData(),
                    "format": self.live_value_table.cellWidget(row, 1).currentText(),
                    "unit": self.live_value_table.cellWidget(row, 2).text(),
                }
            )
        return result

    def _refresh_live_values(self) -> None:
        main_state = self._last_display_state.get("main_state", {})
        for row in range(self.live_value_table.rowCount()):
            field_key = self.live_value_table.cellWidget(row, 0).currentData()
            definition = LIVE_VALUE_CATALOG[field_key]
            response = main_state.get(definition["request"], {})
            raw_text = str(response.get("raw_payload", ""))
            try:
                payload = bytes.fromhex(raw_text)
            except ValueError:
                payload = b""
            start = int(definition["offset"])
            length = int(definition["length"])
            raw_value = payload[start : start + length]
            if not response.get("valid") or len(raw_value) != length:
                rendered, raw_rendered, age_rendered = "—", "—", "—"
            else:
                unsigned = int.from_bytes(raw_value, "big")
                bit = definition.get("bit")
                if bit is not None:
                    unsigned = int(bool(unsigned & (1 << int(bit))))
                display_format = self.live_value_table.cellWidget(row, 1).currentText()
                if display_format == "Signed decimal":
                    rendered = str(int.from_bytes(raw_value, "big", signed=True))
                elif display_format == "Hex":
                    rendered = f"0x{unsigned:0{length * 2}X}"
                elif display_format == "Binary":
                    rendered = f"0b{unsigned:0{length * 8}b}"
                elif display_format == "Boolean":
                    rendered = "TRUE" if unsigned else "FALSE"
                else:
                    rendered = str(unsigned)
                raw_rendered = raw_value.hex(" ").upper()
                age = response.get("age_ms")
                age_rendered = "—" if age is None else f"{float(age):.0f} ms"
            self.live_value_table.item(row, 3).setText(rendered)
            self.live_value_table.item(row, 4).setText(raw_rendered)
            self.live_value_table.item(row, 5).setText(age_rendered)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        safety = QGroupBox("Display Emulator safety")
        safety_layout = QHBoxLayout(safety)
        self.allow_aa = QCheckBox("Allow AA control frames")
        self.allow_aa.setStyleSheet("font-weight:bold;color:#ef9a9a")
        self.allow_aa.setToolTip(
            "AA may request real actuators. Disabled by default and after profile loading."
        )
        self.allow_aa.toggled.connect(self._configuration_changed)
        self.retry_aa = QCheckBox("Retry AA on missing CC")
        self.retry_aa.setChecked(False)
        self.retry_aa.toggled.connect(self._configuration_changed)
        self.ack_timeout = QSpinBox()
        self.ack_timeout.setRange(1, 10000)
        self.ack_timeout.setSuffix(" ms")
        self.ack_timeout.valueChanged.connect(self._configuration_changed)
        self.response_timeout = QSpinBox()
        self.response_timeout.setRange(1, 10000)
        self.response_timeout.setSuffix(" ms")
        self.response_timeout.valueChanged.connect(self._configuration_changed)
        self.abort_process = QPushButton("ABORT WATER PROCESS")
        self.abort_process.setStyleSheet(
            "background:#a82222;color:white;font-weight:bold;padding:8px"
        )
        self.abort_process.setToolTip(
            "Queue one confirmed A3 BASE/abort frame. Requires Allow AA control frames."
        )
        self.abort_process.clicked.connect(self.abort_requested)
        safety_layout.addWidget(self.allow_aa)
        safety_layout.addWidget(self.retry_aa)
        safety_layout.addWidget(QLabel("ACK timeout"))
        safety_layout.addWidget(self.ack_timeout)
        safety_layout.addWidget(QLabel("Response timeout"))
        safety_layout.addWidget(self.response_timeout)
        safety_layout.addWidget(self.abort_process)
        safety_layout.addStretch()
        outer.addWidget(safety)

        self.content_tabs = QTabWidget()
        outer.addWidget(self.content_tabs, 1)
        control_page = QWidget()
        control_layout = QVBoxLayout(control_page)
        control_layout.setContentsMargins(6, 6, 6, 6)
        self.content_tabs.addTab(control_page, "Control")

        auto_group = QGroupBox("Display State / Automatic AA")
        auto_layout = QGridLayout(auto_group)
        self.enable_auto_aa = QCheckBox("Enable Automatic AA")
        self.enable_auto_aa.toggled.connect(self._auto_enabled_changed)
        self.auto_aa_mode = QComboBox()
        self.auto_aa_mode.addItems([mode.value for mode in AutoAAMode])
        self.auto_aa_mode.currentTextChanged.connect(self._auto_mode_changed)
        self.unsafe_process_safety = QCheckBox(
            "Unsafe: bypass automatic process safety"
        )
        self.unsafe_process_safety.setStyleSheet("color:#ff7b72;font-weight:bold")
        self.unsafe_process_safety.toggled.connect(self._configuration_changed)
        self.auto_temperature = QSpinBox()
        self.auto_temperature.setRange(0, 255)
        self.auto_temperature.setSuffix(" °C")
        self.auto_volume = QSpinBox()
        self.auto_volume.setRange(0, 65535)
        self.auto_volume.setSuffix(" ml")
        self.auto_dispense = QComboBox()
        self.auto_dispense.addItem("IDLE / CONFIG (0x80)", 0x80)
        self.auto_dispense.addItem("DISPENSE START (0x01)", 0x01)
        self.auto_dispense.addItem("DISPENSE STOP (0x00)", 0x00)
        self.auto_a3_mode = QComboBox()
        self.auto_a3_mode.addItem("BASE / NORMAL (0x68)", 0x68)
        self.auto_a3_mode.addItem("FILTER / REFILL (0x78)", 0x78)
        self.auto_a3_mode.addItem("FLUSH (0x7C)", 0x7C)
        self.auto_a3_phase = QSpinBox()
        self.auto_a3_phase.setRange(0, 255)
        self.auto_a3_phase.setDisplayIntegerBase(16)
        self.auto_a3_phase.setPrefix("0x")
        self.auto_e1_flow = QSpinBox()
        self.auto_e1_flow.setRange(0, 65535)
        for widget in (
            self.auto_temperature,
            self.auto_volume,
            self.auto_dispense,
            self.auto_a3_mode,
            self.auto_a3_phase,
            self.auto_e1_flow,
        ):
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._configuration_changed)
            else:
                widget.valueChanged.connect(self._configuration_changed)
        auto_layout.addWidget(self.enable_auto_aa, 0, 0)
        auto_layout.addWidget(QLabel("Auto AA mode"), 0, 1)
        auto_layout.addWidget(self.auto_aa_mode, 0, 2)
        auto_layout.addWidget(self.unsafe_process_safety, 0, 3)
        auto_layout.addWidget(QLabel("AA00 temperature"), 1, 0)
        auto_layout.addWidget(self.auto_temperature, 1, 1)
        auto_layout.addWidget(QLabel("AA00 volume"), 1, 2)
        auto_layout.addWidget(self.auto_volume, 1, 3)
        auto_layout.addWidget(QLabel("AA00 command"), 2, 0)
        auto_layout.addWidget(self.auto_dispense, 2, 1)
        auto_layout.addWidget(QLabel("A3 mode"), 2, 2)
        auto_layout.addWidget(self.auto_a3_mode, 2, 3)
        auto_layout.addWidget(QLabel("A3 phase/event"), 3, 0)
        auto_layout.addWidget(self.auto_a3_phase, 3, 1)
        auto_layout.addWidget(QLabel("E1 flow parameter"), 3, 2)
        auto_layout.addWidget(self.auto_e1_flow, 3, 3)
        self.process_mode_status = QLabel("Process Mode: BASE")
        self.process_mode_status.setStyleSheet("font-weight:bold")
        self.process_safety_status = QLabel(
            "Safety: Internal HIGH: — | External Available: —"
        )
        self.process_start_status = QLabel(
            "Filter start allowed: NO | Flush start allowed: NO | Dispense allowed: YES"
        )
        self.last_safety_status = QLabel("Last safety decision: —")
        self.last_safety_status.setWordWrap(True)
        auto_layout.addWidget(self.process_mode_status, 4, 0, 1, 2)
        auto_layout.addWidget(self.process_safety_status, 4, 2, 1, 2)
        auto_layout.addWidget(self.process_start_status, 5, 0, 1, 2)
        auto_layout.addWidget(self.last_safety_status, 5, 2, 1, 2)
        control_layout.addWidget(auto_group)

        scheduler = QGroupBox("Display Request Scheduler — delay after request")
        scheduler_layout = QGridLayout(scheduler)
        scheduler_layout.addWidget(QLabel("Enabled"), 0, 0)
        scheduler_layout.addWidget(QLabel("Request"), 0, 1)
        scheduler_layout.addWidget(QLabel("Delay"), 0, 2)
        for row, request in enumerate(DisplayRequest, 1):
            enabled = QCheckBox()
            enabled.toggled.connect(self._configuration_changed)
            delay = QSpinBox()
            delay.setRange(0, 10000)
            delay.setSuffix(" ms")
            delay.valueChanged.connect(self._configuration_changed)
            self.request_enabled[request] = enabled
            self.request_delay[request] = delay
            scheduler_layout.addWidget(enabled, row, 0)
            scheduler_layout.addWidget(QLabel(request.value), row, 1)
            scheduler_layout.addWidget(delay, row, 2)
        self.start_polling = QPushButton("Start polling")
        self.stop_polling = QPushButton("Stop polling")
        self.step_next = QPushButton("Step next frame")
        self.start_polling.clicked.connect(self._start)
        self.stop_polling.clicked.connect(self.stop_requested)
        self.step_next.clicked.connect(self._step)
        self.send_kind = QComboBox()
        self.send_kind.addItems([request.value for request in DisplayRequest])
        self.send_once = QPushButton("Send once")
        self.send_once.clicked.connect(self._send_once)
        scheduler_layout.addWidget(self.start_polling, 1, 4)
        scheduler_layout.addWidget(self.stop_polling, 2, 4)
        scheduler_layout.addWidget(self.step_next, 3, 4)
        scheduler_layout.addWidget(self.send_kind, 4, 4)
        scheduler_layout.addWidget(self.send_once, 4, 5)
        self.scheduler_status = QLabel("Polling OFF — manual send only")
        self.scheduler_status.setStyleSheet("color:#f1c56b;font-weight:bold")
        scheduler_layout.addWidget(self.scheduler_status, 5, 0, 1, 6)
        self.scheduler_diagnostics = QLabel(
            "Scheduler: STOPPED | Cycle: 0 | Next TX: — | Gap: — | Late: —"
        )
        scheduler_layout.addWidget(self.scheduler_diagnostics, 6, 0, 1, 6)
        scheduler_layout.setColumnStretch(3, 1)
        control_layout.addWidget(scheduler)
        control_layout.addStretch()

        aa_group = QGroupBox("AA payload sequence — generic captured payloads, semantics UNKNOWN")
        aa_layout = QVBoxLayout(aa_group)
        self.aa_table = QTableWidget(0, 4)
        self.aa_table.setHorizontalHeaderLabels(
            ["Enabled", "Name", "Description", "Payload[0..15]"]
        )
        self.aa_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.aa_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.aa_table.itemChanged.connect(self._aa_item_changed)
        aa_buttons = QHBoxLayout()
        for label, callback in (
            ("Add payload", self._add_payload),
            ("Duplicate payload", self._duplicate_payload),
            ("Remove payload", self._remove_payload),
            ("Move up", lambda: self._move_payload(-1)),
            ("Move down", lambda: self._move_payload(1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            aa_buttons.addWidget(button)
        self.aa_round_robin = QCheckBox("Round robin enabled payloads")
        self.aa_round_robin.setChecked(True)
        self.aa_round_robin.toggled.connect(self._configuration_changed)
        hold = QPushButton("Hold selected payload")
        hold.clicked.connect(self._hold_selected)
        self.send_selected_aa = QPushButton("Send selected AA once")
        self.send_selected_aa.clicked.connect(self._send_selected_aa)
        aa_buttons.addWidget(self.aa_round_robin)
        aa_buttons.addWidget(hold)
        aa_buttons.addWidget(self.send_selected_aa)
        aa_buttons.addStretch()
        aa_layout.addWidget(self.aa_table)
        aa_layout.addLayout(aa_buttons)
        self.preset_save_status = QLabel(
            f"AA presets auto-save to {self.preset_path}"
        )
        self.preset_save_status.setStyleSheet("color:#73d795")
        aa_layout.addWidget(self.preset_save_status)
        self.content_tabs.addTab(aa_group, "AA Presets")

        diagnostic = QGroupBox("MainState cache / Auto-AA diagnostics")
        diagnostic_layout = QVBoxLayout(diagnostic)
        self.main_state_table = QTableWidget(3, 4)
        self.main_state_table.setHorizontalHeaderLabels(
            ["Response", "Valid", "Age", "Raw payload Data[0..15]"]
        )
        self.main_state_table.verticalHeader().hide()
        self.main_state_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        for row, request_name in enumerate(("A5", "E5 B2", "E5 E1")):
            self.main_state_table.setItem(row, 0, QTableWidgetItem(request_name))
        self.auto_state_summary = QLabel(
            "Auto AA disabled — no cached Main responses yet"
        )
        self.auto_state_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.auto_state_summary.setWordWrap(True)
        diagnostic_layout.addWidget(self.main_state_table)
        diagnostic_layout.addWidget(self.auto_state_summary)
        live_page = QWidget()
        live_layout = QVBoxLayout(live_page)
        live_layout.setContentsMargins(6, 6, 6, 6)
        live_layout.addWidget(self._build_live_values_group(), 2)
        live_layout.addWidget(diagnostic, 1)
        self.content_tabs.insertTab(1, live_page, "Live Values")

        trace_group = QGroupBox("Display-role request/response trace")
        trace_layout = QVBoxLayout(trace_group)
        self.trace = QTableWidget(0, 8)
        self.trace.setHorizontalHeaderLabels(
            ["Time", "Direction", "Request", "Raw", "Status", "Latency", "Source", "Reason"]
        )
        self.trace.horizontalHeader().setStretchLastSection(False)
        self.trace.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        trace_buttons = QHBoxLayout()
        clear = QPushButton("Clear trace")
        clear.clicked.connect(self.clear_trace)
        export_json = QPushButton("Export JSON")
        export_json.clicked.connect(lambda: self.export_trace("json"))
        export_csv = QPushButton("Export CSV")
        export_csv.clicked.connect(lambda: self.export_trace("csv"))
        trace_buttons.addWidget(clear)
        trace_buttons.addWidget(export_json)
        trace_buttons.addWidget(export_csv)
        trace_buttons.addStretch()
        trace_layout.addWidget(self.trace)
        trace_layout.addLayout(trace_buttons)
        trace_page = QWidget()
        trace_page_layout = QVBoxLayout(trace_page)
        trace_page_layout.setContentsMargins(6, 6, 6, 6)
        trace_page_layout.addWidget(trace_group, 1)

        self.latency_table = QTableWidget(5, 7)
        self.latency_table.setHorizontalHeaderLabels(
            ["Request", "Count", "Timeouts / >750 ms", "Min ms", "Max ms", "Average ms", "Median / jitter ms"]
        )
        self.latency_table.verticalHeader().hide()
        for row, request in enumerate(DisplayRequest):
            self.latency_table.setItem(row, 0, QTableWidgetItem(request.value))
        self.latency_table.setItem(4, 0, QTableWidgetItem("TX-to-TX"))
        trace_page_layout.addWidget(self.latency_table)
        self.content_tabs.addTab(trace_page, "Trace & Statistics")

    def config(self) -> DisplayRoleConfig:
        requests = {
            request: {
                "enabled": widget.isChecked(),
                "delay_ms": self.request_delay[request].value(),
            }
            for request, widget in self.request_enabled.items()
        }
        presets = []
        for row in range(self.aa_table.rowCount()):
            enabled = self.aa_table.cellWidget(row, 0)
            metadata = self.aa_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            if not isinstance(metadata, dict):
                metadata = {"knowledge_class": metadata}
            presets.append(
                AAPayloadPreset(
                    self.aa_table.item(row, 1).text().strip() or f"AA Payload {row + 1}",
                    bytes.fromhex(self.aa_table.item(row, 3).text()),
                    enabled.isChecked(),
                    metadata.get("knowledge_class"),
                    bool(metadata.get("auto_checksum", True)),
                    metadata.get("checksum_high"),
                    metadata.get("checksum_low"),
                    self.aa_table.item(row, 2).text().strip(),
                )
            )
        return DisplayRoleConfig(
            requests=requests,
            aa_presets=presets,
            aa_round_robin=self.aa_round_robin.isChecked(),
            aa_hold_index=max(0, self.aa_table.currentRow()),
            allow_aa_control=self.allow_aa.isChecked(),
            retry_aa=self.retry_aa.isChecked(),
            ack_timeout_ms=self.ack_timeout.value(),
            response_timeout_ms=self.response_timeout.value(),
            auto_aa_enabled=self.enable_auto_aa.isChecked(),
            auto_aa_mode=AutoAAMode(self.auto_aa_mode.currentText()),
            auto_temperature=self.auto_temperature.value(),
            auto_volume_ml=self.auto_volume.value(),
            auto_dispense_command=int(self.auto_dispense.currentData()),
            auto_a3_mode=int(self.auto_a3_mode.currentData()),
            auto_a3_phase=self.auto_a3_phase.value(),
            auto_e1_flow_parameter=self.auto_e1_flow.value(),
            live_value_fields=self._live_value_definitions(),
            unsafe_process_safety_bypass=self.unsafe_process_safety.isChecked(),
        )

    def load_config(self, config: DisplayRoleConfig) -> None:
        self._updating = True
        for request in DisplayRequest:
            settings = config.requests[request]
            self.request_enabled[request].setChecked(bool(settings["enabled"]))
            self.request_delay[request].setValue(int(settings["delay_ms"]))
        self.allow_aa.setChecked(config.allow_aa_control)
        self.retry_aa.setChecked(config.retry_aa)
        self.ack_timeout.setValue(config.ack_timeout_ms)
        self.response_timeout.setValue(config.response_timeout_ms)
        self.aa_round_robin.setChecked(config.aa_round_robin)
        self.enable_auto_aa.setChecked(config.auto_aa_enabled)
        self.auto_aa_mode.setCurrentText(config.auto_aa_mode.value)
        self.auto_temperature.setValue(config.auto_temperature)
        self.auto_volume.setValue(config.auto_volume_ml)
        self._set_combo_data(self.auto_dispense, config.auto_dispense_command)
        self._set_combo_data(self.auto_a3_mode, config.auto_a3_mode)
        self.auto_a3_phase.setValue(config.auto_a3_phase)
        self.auto_e1_flow.setValue(config.auto_e1_flow_parameter)
        self.unsafe_process_safety.setChecked(config.unsafe_process_safety_bypass)
        self._load_live_value_fields(config.live_value_fields)
        self.aa_table.setRowCount(0)
        for preset in config.aa_presets:
            self._append_preset(preset)
        if self.aa_table.rowCount():
            self.aa_table.selectRow(min(config.aa_hold_index, self.aa_table.rowCount() - 1))
        self._updating = False
        self._persist_presets(config.aa_presets)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: int) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _auto_enabled_changed(self, enabled: bool) -> None:
        if self._updating:
            return
        if enabled and self.auto_aa_mode.currentText() == AutoAAMode.OFF.value:
            self._updating = True
            self.auto_aa_mode.setCurrentText(AutoAAMode.AUTO.value)
            self._updating = False
        self._configuration_changed()

    def _auto_mode_changed(self, mode_text: str) -> None:
        if self._updating:
            return
        self._updating = True
        self.enable_auto_aa.setChecked(mode_text != AutoAAMode.OFF.value)
        self._updating = False
        self._configuration_changed()

    def _append_preset(self, preset: AAPayloadPreset) -> None:
        row = self.aa_table.rowCount()
        self.aa_table.insertRow(row)
        enabled = QCheckBox()
        enabled.setChecked(preset.enabled)
        enabled.toggled.connect(self._configuration_changed)
        self.aa_table.setCellWidget(row, 0, enabled)
        name_item = QTableWidgetItem(preset.name)
        name_item.setData(
            Qt.ItemDataRole.UserRole,
            {
                "knowledge_class": preset.knowledge_class,
                "auto_checksum": preset.auto_checksum,
                "checksum_high": preset.checksum_high,
                "checksum_low": preset.checksum_low,
            },
        )
        self.aa_table.setItem(row, 1, name_item)
        self.aa_table.setItem(row, 2, QTableWidgetItem(preset.description))
        payload_item = QTableWidgetItem(preset.payload.hex(" ").upper())
        payload_item.setData(Qt.ItemDataRole.UserRole, preset.payload)
        self.aa_table.setItem(row, 3, payload_item)

    def _configuration_changed(self, ignored=None) -> None:
        if self._updating:
            return
        try:
            config = self.config()
            self._persist_presets(config.aa_presets)
            self.config_changed.emit(config)
        except ValueError:
            return

    def _persist_presets(self, presets: list[AAPayloadPreset]) -> None:
        try:
            save_aa_command_presets(presets, self.preset_path)
            if hasattr(self, "preset_save_status"):
                self.preset_save_status.setText(
                    f"AA presets saved automatically: {self.preset_path}"
                )
                self.preset_save_status.setStyleSheet("color:#73d795")
        except OSError as exc:
            if hasattr(self, "preset_save_status"):
                self.preset_save_status.setText(f"AA preset save failed: {exc}")
                self.preset_save_status.setStyleSheet(
                    "color:#ef6c6c;font-weight:bold"
                )

    def _aa_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        if item.column() == 3:
            try:
                payload = bytes.fromhex(item.text())
                if len(payload) != 16:
                    raise ValueError
                self._updating = True
                item.setText(payload.hex(" ").upper())
                item.setData(Qt.ItemDataRole.UserRole, payload)
                self._updating = False
            except ValueError:
                QMessageBox.warning(self, "Invalid AA payload", "Enter exactly 16 hexadecimal payload bytes.")
                self._updating = True
                previous = item.data(Qt.ItemDataRole.UserRole) or bytes(16)
                item.setText(bytes(previous).hex(" ").upper())
                self._updating = False
                return
        self._configuration_changed()

    def _add_payload(self) -> None:
        self._append_preset(AAPayloadPreset("New AA Payload", bytes(16)))
        self.aa_table.selectRow(self.aa_table.rowCount() - 1)
        self._configuration_changed()

    def _duplicate_payload(self) -> None:
        row = self.aa_table.currentRow()
        if row < 0:
            return
        source = self.config().aa_presets[row]
        self._append_preset(
            AAPayloadPreset(
                source.name + " copy",
                source.payload,
                True,
                source.knowledge_class,
                source.auto_checksum,
                source.checksum_high,
                source.checksum_low,
                source.description,
            )
        )
        self.aa_table.selectRow(self.aa_table.rowCount() - 1)
        self._configuration_changed()

    def _remove_payload(self) -> None:
        if self.aa_table.rowCount() <= 1:
            QMessageBox.warning(self, "AA payload", "At least one AA payload must remain.")
            return
        row = self.aa_table.currentRow()
        if row >= 0:
            self.aa_table.removeRow(row)
            self._configuration_changed()

    def _move_payload(self, delta: int) -> None:
        row = self.aa_table.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.aa_table.rowCount():
            return
        config = self.config()
        config.aa_presets[row], config.aa_presets[target] = config.aa_presets[target], config.aa_presets[row]
        config.aa_hold_index = target
        self.load_config(config)
        self._configuration_changed()

    def _hold_selected(self) -> None:
        if self.aa_table.currentRow() < 0:
            return
        self.aa_round_robin.setChecked(False)
        self._configuration_changed()

    def _start(self) -> None:
        self._configuration_changed()
        self.start_requested.emit()

    def _step(self) -> None:
        self._configuration_changed()
        self.step_requested.emit()

    def _send_once(self) -> None:
        self._send_request(DisplayRequest(self.send_kind.currentText()))

    def _send_request(self, request: DisplayRequest) -> None:
        self._configuration_changed()
        self.send_requested.emit(request.value)

    def _send_selected_aa(self) -> None:
        row = self.aa_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "AA payload", "Select an AA payload first.")
            return
        self._configuration_changed()
        self.send_aa_preset_requested.emit(row)

    def apply_payload_to_auto_state(self, payload: bytes) -> None:
        if len(payload) != 16:
            raise ValueError("AA auto-state payload must contain 16 bytes")
        discriminator = payload[15]
        if discriminator == 0x00:
            self.auto_temperature.setValue(payload[2])
            self.auto_volume.setValue(int.from_bytes(payload[3:5], "big"))
            self._set_combo_data(self.auto_dispense, payload[1])
        elif discriminator == 0xA3:
            self._set_combo_data(self.auto_a3_mode, payload[4])
            self.auto_a3_phase.setValue(payload[5])
        elif discriminator == 0xE1:
            self.auto_e1_flow.setValue(int.from_bytes(payload[2:4], "big"))
        else:
            raise ValueError(f"Unsupported AA class 0x{discriminator:02X}")
        self._configuration_changed()

    def set_connected(self, connected: bool) -> None:
        for widget in (
            self.start_polling,
            self.stop_polling,
            self.step_next,
            self.send_once,
            self.send_selected_aa,
            self.abort_process,
        ):
            widget.setEnabled(connected)

    def force_safe_idle(self) -> None:
        self.allow_aa.setChecked(False)
        self.request_enabled[DisplayRequest.AA].setChecked(False)
        self.enable_auto_aa.setChecked(False)
        self.auto_aa_mode.setCurrentText(AutoAAMode.OFF.value)
        self.unsafe_process_safety.setChecked(False)
        self.scheduler_status.setText("Polling OFF — manual send only; AA safety locked")
        self.scheduler_status.setStyleSheet("color:#f1c56b;font-weight:bold")

    def append_event(self, event: dict) -> None:
        self.trace_events.append(dict(event))
        state = event.get("state")
        if isinstance(state, dict):
            self.update_display_state(state)
        status = str(event.get("status", ""))
        if status == "POLLING STARTED":
            self.scheduler_status.setText(status)
            self.scheduler_status.setStyleSheet("color:#73d795;font-weight:bold")
        elif status in ("POLLING STOPPED", "NO TRAFFIC OBSERVED") or "BLOCKED" in status:
            self.scheduler_status.setText(status)
            self.scheduler_status.setStyleSheet("color:#f1c56b;font-weight:bold")
        row = self.trace.rowCount()
        self.trace.insertRow(row)
        timestamp = event.get("timestamp")
        values = (
            datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")[:-3] if timestamp else "—",
            event.get("direction", "—"),
            event.get("request", "—"),
            event.get("raw", ""),
            status,
            "—" if event.get("latency_ms") is None else f"{float(event['latency_ms']):.3f} ms",
            event.get("source", ""),
            event.get("reason", ""),
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            gap = (
                state.get("auto_aa", {}).get("actual_inter_frame_gap_ms")
                if isinstance(state, dict)
                else None
            )
            if event.get("event") == "tx" and gap is not None and float(gap) > 750.0:
                item.setBackground(QColor("#6b3a17"))
            self.trace.setItem(row, column, item)
        self.trace.scrollToBottom()

    def update_display_state(self, snapshot: dict) -> None:
        self._last_display_state = dict(snapshot)
        self._refresh_live_values()
        main_state = snapshot.get("main_state", {})
        for row, request_name in enumerate(("A5", "E5 B2", "E5 E1")):
            entry = main_state.get(request_name, {})
            age = entry.get("age_ms")
            values = (
                request_name,
                "YES" if entry.get("valid") else "NO",
                "—" if age is None else f"{float(age):.0f} ms",
                entry.get("raw_payload", ""),
            )
            for column, value in enumerate(values):
                self.main_state_table.setItem(
                    row, column, QTableWidgetItem(str(value))
                )
        auto = snapshot.get("auto_aa", {})
        requested_mode = auto.get("a3_mode")
        if isinstance(requested_mode, str) and requested_mode.startswith("0x"):
            self._updating = True
            self._set_combo_data(self.auto_a3_mode, int(requested_mode, 16))
            self._updating = False
        aa00 = auto.get("aa00_state", {})
        decoded = main_state.get("decoded", {})
        mode_hex = auto.get("process_mode", "0x68")
        mode_name = {"0x68": "BASE", "0x78": "FILTER", "0x7C": "FLUSH"}.get(
            mode_hex, mode_hex
        )
        self.process_mode_status.setText(f"Process Mode: {mode_name}")
        self.process_safety_status.setText(
            "Safety: Internal HIGH: "
            f"{int(bool(decoded.get('internal_high_sensor')))} | External Available: "
            f"{int(bool(decoded.get('external_tank_available')))}"
        )
        self.process_start_status.setText(
            "Filter start allowed: "
            f"{'YES' if auto.get('filter_start_allowed') else 'NO'} | "
            "Flush start allowed: "
            f"{'YES' if auto.get('flush_start_allowed') else 'NO'} | "
            "Dispense allowed: "
            f"{'YES' if auto.get('dispense_allowed', True) else 'NO'} | "
            "Initial fill: "
            f"{'ACTIVE' if auto.get('initial_fill_active') else 'OFF'}"
        )
        self.last_safety_status.setText(
            f"Last safety decision: {auto.get('last_safety_decision') or '—'}"
        )
        next_tx = auto.get("expected_next_tx_ms")
        gap = auto.get("actual_inter_frame_gap_ms")
        self.scheduler_diagnostics.setText(
            f"Scheduler: {auto.get('scheduler_state', 'STOPPED')} | "
            f"Cycle: {auto.get('cycle_number', 0)} | "
            f"Next TX: {'—' if next_tx is None else f'{float(next_tx):.0f} ms'} | "
            f"Gap: {'—' if gap is None else f'{float(gap):.1f} ms'} | "
            f"Late: {float(auto.get('late_by_ms', 0)):.1f} ms"
        )
        self.auto_state_summary.setText(
            " | ".join(
                (
                    f"Enabled: {auto.get('enabled', False)}",
                    f"Mode: {auto.get('mode', 'OFF')}",
                    f"AA00: command={aa00.get('dispense_command', '—')}, "
                    f"temp={aa00.get('temperature', '—')} °C, "
                    f"volume={aa00.get('volume_ml', '—')} ml",
                    f"A3: mode={auto.get('a3_mode', '—')}, phase={auto.get('a3_phase', '—')}",
                    f"E1: {auto.get('e1_flow_parameter', '—')}",
                    f"Sensors: BELOW LOW={decoded.get('internal_below_low', '—')}, "
                    f"Internal HIGH={decoded.get('internal_high_sensor', '—')}, "
                    f"External available={decoded.get('external_tank_available', '—')}",
                    f"Manual queued: {auto.get('pending_manual_aa', 0)}",
                    f"Safety queued: {auto.get('pending_safety_action', 0)}",
                    f"Last decision: {auto.get('last_decision_reason') or '—'}",
                )
            )
        )

    def update_statistics(self, statistics: dict) -> None:
        for row, request_name in enumerate(
            [request.value for request in DisplayRequest] + ["TX-to-TX"]
        ):
            values = statistics.get(request_name, {})
            cells = (
                request_name,
                str(values.get("count", 0)),
                str(values.get("timeout_count", 0)),
                f"{float(values.get('minimum_ms', 0)):.3f}",
                f"{float(values.get('maximum_ms', 0)):.3f}",
                f"{float(values.get('average_ms', 0)):.3f}",
                f"{float(values.get('median_ms', 0)):.3f} / {float(values.get('jitter_ms', 0)):.3f}",
            )
            for column, value in enumerate(cells):
                self.latency_table.setItem(row, column, QTableWidgetItem(value))

    def clear_trace(self) -> None:
        self.trace_events.clear()
        self.trace.setRowCount(0)

    def export_trace(self, kind: str) -> None:
        filename, ignored = QFileDialog.getSaveFileName(
            self, "Export display-role trace", f"display-role-trace.{kind}"
        )
        if not filename:
            return
        try:
            if kind == "json":
                Path(filename).write_text(json.dumps(self.trace_events, indent=2) + "\n", encoding="utf-8")
            else:
                fields = ["timestamp", "direction", "request", "raw", "status", "latency_ms", "checksum_valid", "source", "reason"]
                with Path(filename).open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(self.trace_events)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
