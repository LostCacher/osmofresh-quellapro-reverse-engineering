"""Controls and diagnostics for the ATmega2560 MITM bridge transport."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QFileDialog, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from protocol.frames import parse_hex_bytes
from protocol.dispense_capture import (
    CAPTURE_SCENARIOS, DispenseSequenceCapture, is_display_dispense_start,
)
from protocol.display_role import load_aa_command_presets
from protocol.knowledge import ProtocolKnowledge
from protocol.mega_bridge import (
    BridgeDirection, BridgeMode, FrameAction, MegaConfig, MegaMessageType,
    MegaRule, TankConfig, MAX_MANUAL_DISPENSE_ML,
)


class MegaBridgeTab(QWidget):
    config_requested = Signal(object)
    inject_requested = Signal(object, object)
    set_rule_requested = Signal(object)
    rule_command_requested = Signal(object)
    manual_dispense_requested = Signal(int, int)
    stop_remote_dispense_requested = Signal()
    tank_config_requested = Signal(object)
    tds_test_requested = Signal(int)
    tds_test_disable_requested = Signal()

    def __init__(self, knowledge: ProtocolKnowledge, parent=None) -> None:
        super().__init__(parent)
        self._rule_match_count = 0
        self._rule_replace_count = 0
        self._connected = False
        self.sequence_capture = DispenseSequenceCapture(knowledge)
        self._capture_host_started = 0.0
        self._capture_duration_s = 20
        self._last_capture_path: Path | None = None
        self._pending_replace_original: bytes | None = None
        self.capture_timer = QTimer(self)
        self.capture_timer.setInterval(100)
        self.capture_timer.timeout.connect(self._capture_tick)
        layout = QVBoxLayout(self)

        config_box = QGroupBox("ATmega2560 bridge configuration")
        form = QFormLayout(config_box)
        self.mode = QComboBox()
        self.mode.addItem("DIRECT PASS — Display ↔ Main", BridgeMode.DIRECT_PASS)
        self.mode.addItem("DISPLAY_ONLY_TEST — Main may be absent", BridgeMode.DISPLAY_ONLY_TEST)
        self.quella_baud = QComboBox()
        self.quella_baud.setEditable(True)
        self.quella_baud.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.idle_gap = QSpinBox()
        self.idle_gap.setRange(1000, 100000)
        self.idle_gap.setValue(4000)
        self.idle_gap.setSuffix(" µs")
        self.sniff = QCheckBox("Report captured traffic to PC")
        self.sniff.setChecked(True)
        apply_button = QPushButton("Apply bridge configuration")
        apply_button.clicked.connect(lambda: self.config_requested.emit(self.config()))
        form.addRow("Bridge mode", self.mode)
        form.addRow("Quella UART baud", self.quella_baud)
        form.addRow("Safe idle gap", self.idle_gap)
        form.addRow("Sniffing", self.sniff)
        form.addRow(apply_button)
        layout.addWidget(config_box)

        status_box = QGroupBox("Bridge status")
        status_form = QFormLayout(status_box)
        self.connection = QLabel("Not connected")
        self.firmware = QLabel("—")
        self.status = QLabel("—")
        self.counters = QLabel("Bridge drops: 0 | Capture drops: 0")
        status_form.addRow("Connection", self.connection)
        status_form.addRow("Firmware", self.firmware)
        status_form.addRow("State", self.status)
        status_form.addRow("Buffers", self.counters)
        layout.addWidget(status_box)

        tank_box = QGroupBox("Tank / Fill status")
        tank_form = QFormLayout(tank_box)
        tank_controls = QHBoxLayout()
        self.external_delay_enabled = QCheckBox("External water available delay")
        self.external_delay_enabled.setChecked(True)
        self.external_delay_ms = QSpinBox()
        self.external_delay_ms.setRange(0, 60000)
        self.external_delay_ms.setValue(10000)
        self.external_delay_ms.setSuffix(" ms")
        self.apply_tank_config = QPushButton("Apply tank configuration")
        self.apply_tank_config.clicked.connect(
            lambda: self.tank_config_requested.emit(self.tank_config())
        )
        tank_controls.addWidget(self.external_delay_enabled)
        tank_controls.addWidget(self.external_delay_ms)
        tank_controls.addWidget(self.apply_tank_config)
        tank_controls.addStretch(1)
        self.tank_internal_min = QLabel("UNKNOWN")
        self.tank_internal_max = QLabel("UNKNOWN")
        self.tank_external_low = QLabel("UNKNOWN")
        self.tank_external_raw = QLabel("UNKNOWN")
        self.tank_external_stable = QLabel("UNKNOWN")
        self.tank_delay_status = QLabel("YES | 10000 ms | remaining —")
        self.tank_fill_allowed = QLabel("NO")
        self.tank_fill_active = QLabel("UNKNOWN")
        self.tank_last_frame = QLineEdit()
        self.tank_last_frame.setReadOnly(True)
        self.tank_last_age = QLabel("UNKNOWN")
        tank_form.addRow(tank_controls)
        tank_form.addRow("Internal tank MIN", self.tank_internal_min)
        tank_form.addRow("Internal tank MAX", self.tank_internal_max)
        tank_form.addRow("External tank LOW", self.tank_external_low)
        tank_form.addRow("External water raw", self.tank_external_raw)
        tank_form.addRow("External water stable", self.tank_external_stable)
        tank_form.addRow("Fill delay", self.tank_delay_status)
        tank_form.addRow("Fill allowed", self.tank_fill_allowed)
        tank_form.addRow("Fill active", self.tank_fill_active)
        tank_form.addRow("Last tank status frame", self.tank_last_frame)
        tank_form.addRow("Last status update age", self.tank_last_age)
        layout.addWidget(tank_box)

        tds_box = QGroupBox("Isolated Display TDS mapping — real Main MCU disconnected")
        tds_form = QFormLayout(tds_box)
        tds_row = QHBoxLayout()
        self.tds_raw_value = QSpinBox()
        self.tds_raw_value.setRange(0, 255)
        self.tds_raw_value.setDisplayIntegerBase(16)
        self.tds_raw_value.setPrefix("0x")
        self.tds_raw_value.setValue(0x32)
        self.tds_apply = QPushButton("Queue raw update")
        self.tds_disable = QPushButton("Stop TDS emulation")
        self.tds_apply.clicked.connect(self._queue_tds_test)
        self.tds_disable.clicked.connect(self.tds_test_disable_requested.emit)
        tds_row.addWidget(self.tds_raw_value)
        tds_row.addWidget(self.tds_apply)
        tds_row.addWidget(self.tds_disable)
        tds_row.addStretch(1)
        self.tds_test_status = QLabel("DISABLED")
        self.tds_test_status.setToolTip(
            "On the next two E5 B2 requests Python sends the measurement and commit phases."
        )
        tds_form.addRow("Synthetic E5 B2 Data[3]", tds_row)
        tds_form.addRow("State", self.tds_test_status)
        layout.addWidget(tds_box)

        manual_command = QGroupBox("Manual command / Direct command test")
        manual_form = QFormLayout(manual_command)
        manual_row = QHBoxLayout()
        self.manual_dispense_volume = QSpinBox()
        self.manual_dispense_volume.setRange(1, MAX_MANUAL_DISPENSE_ML)
        self.manual_dispense_volume.setValue(10)
        self.manual_dispense_volume.setSuffix(" ml")
        self.manual_feed_pump_parameter = QSpinBox()
        self.manual_feed_pump_parameter.setRange(100, 1200)
        self.manual_feed_pump_parameter.setValue(1000)
        self.manual_feed_pump_parameter.setSingleStep(50)
        self.manual_feed_pump_parameter.setSuffix(" ml/min")
        self.manual_feed_pump_parameter.setToolTip(
            "AA E1 feed-pump parameter for manual 25 C dispensing. "
            "Observed normal value: 1000; captured maximum: 1200 ml/min."
        )
        self.send_dispense = QPushButton("SEND DISPENSE")
        self.stop_remote_dispense = QPushButton("STOP REMOTE DISPENSE")
        self.send_dispense.setEnabled(False)
        self.stop_remote_dispense.setEnabled(False)
        self.send_dispense.setToolTip(
            "Send the known AA00 dispense command to Main MCU via Serial2. "
            "Mega validates the current live machine and tank state before transmission."
        )
        self.send_dispense.clicked.connect(self._send_manual_dispense)
        self.stop_remote_dispense.clicked.connect(self._stop_remote_dispense)
        manual_row.addWidget(self.manual_dispense_volume)
        manual_row.addWidget(self.manual_feed_pump_parameter)
        manual_row.addWidget(self.send_dispense)
        manual_row.addWidget(self.stop_remote_dispense)
        manual_row.addStretch(1)
        self.manual_command_status = QLabel(
            "Requires DIRECT_PASS, fresh validated state, and no active manipulation rule."
        )
        self.remote_session_status = QLabel("IDLE")
        self.remote_runtime = QLabel(
            "Target 0 ml | A5 0 ml | Main NO | suppressed idle 0 | replaced refresh 0 | 0.000 s"
        )
        manual_form.addRow("25 °C dispense", manual_row)
        manual_form.addRow("Status", self.manual_command_status)
        manual_form.addRow("Remote session", self.remote_session_status)
        manual_form.addRow("Remote runtime", self.remote_runtime)
        layout.addWidget(manual_command)

        sequence_box = QGroupBox("Dispense Sequence Capture — both directions")
        sequence_form = QFormLayout(sequence_box)
        capture_controls = QHBoxLayout()
        self.capture_scenario = QComboBox()
        for key, description in CAPTURE_SCENARIOS.items():
            self.capture_scenario.addItem(f"{key}) {description}", key)
        self.auto_dispense_capture = QCheckBox("Auto-start on valid Display AA00 DISPENSE")
        self.auto_dispense_capture.setChecked(True)
        self.capture_duration = QSpinBox()
        self.capture_duration.setRange(5, 120)
        self.capture_duration.setValue(20)
        self.capture_duration.setSuffix(" s")
        self.capture_duration.setToolTip(
            "Use at least 20 seconds for a complete 150 ml dispense including its stop sequence."
        )
        self.start_capture = QPushButton("START CAPTURE")
        self.stop_capture = QPushButton("STOP CAPTURE")
        self.export_capture_jsonl = QPushButton("Export JSONL")
        self.export_capture_csv = QPushButton("Export CSV")
        self.start_capture.clicked.connect(lambda: self._begin_capture("manual button"))
        self.stop_capture.clicked.connect(lambda: self._finish_capture("operator stopped"))
        self.export_capture_jsonl.clicked.connect(self._export_capture_jsonl)
        self.export_capture_csv.clicked.connect(self._export_capture_csv)
        for widget in (
            self.capture_scenario, self.start_capture, self.stop_capture,
            self.export_capture_jsonl, self.export_capture_csv,
        ):
            capture_controls.addWidget(widget)
        self.capture_status = QLabel("INACTIVE | frames=0 | elapsed=0.000 s")
        self.capture_last_frame = QLineEdit()
        self.capture_last_frame.setReadOnly(True)
        sequence_form.addRow(capture_controls)
        sequence_form.addRow(self.auto_dispense_capture)
        sequence_form.addRow("Capture duration", self.capture_duration)
        sequence_form.addRow("Status", self.capture_status)
        sequence_form.addRow("Last frame", self.capture_last_frame)
        layout.addWidget(sequence_box)

        manipulation = QGroupBox("Fail-safe frame manipulation — exact known frame only")
        manipulation_form = QFormLayout(manipulation)
        self.manipulation_enabled = QCheckBox("Enable manipulation (2 s PC lease)")
        self.manipulation_enabled.toggled.connect(
            lambda enabled: self.rule_command_requested.emit(
                MegaMessageType.ENABLE_RULE if enabled else MegaMessageType.DISABLE_RULE
            )
        )
        self.rule_direction = QComboBox()
        self.rule_direction.addItem("DISPLAY_TO_MAIN", BridgeDirection.DISPLAY_TO_MAIN)
        self.rule_direction.addItem("MAIN_TO_DISPLAY", BridgeDirection.MAIN_TO_DISPLAY)
        self.known_command = QComboBox()
        for preset in load_aa_command_presets():
            if preset.knowledge_class == "control_setpoint_00" and preset.payload[1] == 0x01:
                self.known_command.addItem(
                    f"{preset.name} — {preset.description}", bytes(preset.payload)
                )
        self.rule_action = QComboBox()
        for action in FrameAction:
            self.rule_action.addItem(action.name, action)
        self.rule_action.setCurrentIndex(self.rule_action.findData(FrameAction.REPLACE))
        self.override_value = QSpinBox()
        self.override_value.setRange(0, 65535)
        self.override_value.setValue(150)
        self.override_value.setSuffix(" ml")
        buttons = QHBoxLayout()
        apply_rule = QPushButton("Set rule")
        enable_rule = QPushButton("Enable rule")
        disable_rule = QPushButton("Disable rule")
        clear_rule = QPushButton("Clear rule")
        refresh_rule = QPushButton("Get rules")
        apply_rule.clicked.connect(self._set_rule)
        enable_rule.clicked.connect(lambda: self.rule_command_requested.emit(MegaMessageType.ENABLE_RULE))
        disable_rule.clicked.connect(lambda: self.rule_command_requested.emit(MegaMessageType.DISABLE_RULE))
        clear_rule.clicked.connect(lambda: self.rule_command_requested.emit(MegaMessageType.CLEAR_RULE))
        refresh_rule.clicked.connect(lambda: self.rule_command_requested.emit(MegaMessageType.GET_RULES))
        for button in (apply_rule, enable_rule, disable_rule, clear_rule, refresh_rule):
            buttons.addWidget(button)
        self.rule_status = QLabel("PASS — no rule status received")
        self.rule_counters = QLabel("Matches: 0 | Replacements: 0")
        self.last_original = QLineEdit()
        self.last_original.setReadOnly(True)
        self.last_replacement = QLineEdit()
        self.last_replacement.setReadOnly(True)
        self.override_active = QLabel("NO")
        self.override_origin = QLabel("NONE")
        self.override_target = QLabel("0 ml")
        self.override_original = QLabel("0 ml")
        self.override_refreshes = QLabel("0")
        self.override_blocked_aborts = QLabel("0")
        self.override_progress = QLabel("0 ml")
        self.override_main_dispensing = QLabel("NO")
        self.override_elapsed = QLabel("0.000 s")
        manipulation_form.addRow("Master", self.manipulation_enabled)
        manipulation_form.addRow("Direction", self.rule_direction)
        manipulation_form.addRow("Rule / known command", self.known_command)
        manipulation_form.addRow("Action", self.rule_action)
        manipulation_form.addRow("Override value", self.override_value)
        manipulation_form.addRow(buttons)
        manipulation_form.addRow("Current status", self.rule_status)
        manipulation_form.addRow("Counters", self.rule_counters)
        manipulation_form.addRow("Last original", self.last_original)
        manipulation_form.addRow("Last replacement", self.last_replacement)
        manipulation_form.addRow("Override active", self.override_active)
        manipulation_form.addRow("Session origin", self.override_origin)
        manipulation_form.addRow("Target", self.override_target)
        manipulation_form.addRow("Original display amount", self.override_original)
        manipulation_form.addRow("Refresh replacements", self.override_refreshes)
        manipulation_form.addRow("Blocked aborts", self.override_blocked_aborts)
        manipulation_form.addRow("Current A5 amount", self.override_progress)
        manipulation_form.addRow("Main dispensing", self.override_main_dispensing)
        manipulation_form.addRow("Override elapsed", self.override_elapsed)
        layout.addWidget(manipulation)

        injection = QGroupBox("Safe frame injection — queued until a frame boundary / idle gap")
        injection_layout = QVBoxLayout(injection)
        row = QHBoxLayout()
        self.direction = QComboBox()
        self.direction.addItem("DISPLAY_TO_MAIN", BridgeDirection.DISPLAY_TO_MAIN)
        self.direction.addItem("MAIN_TO_DISPLAY", BridgeDirection.MAIN_TO_DISPLAY)
        self.hex_input = QLineEdit()
        self.hex_input.setPlaceholderText("AA 00 ... or a known complete frame")
        inject_button = QPushButton("Queue injection")
        inject_button.clicked.connect(self._inject)
        row.addWidget(self.direction)
        row.addWidget(self.hex_input, 1)
        row.addWidget(inject_button)
        injection_layout.addLayout(row)
        self.queue = QTableWidget(0, 5)
        self.queue.setHorizontalHeaderLabels(["Time", "Sequence", "Direction", "State", "Frame"])
        self.queue.horizontalHeader().setStretchLastSection(True)
        injection_layout.addWidget(self.queue)
        layout.addWidget(injection, 1)

        events_box = QGroupBox("Bridge event log")
        events_layout = QVBoxLayout(events_box)
        self.events = QTextEdit()
        self.events.setReadOnly(True)
        self.events.setFont(QFont("monospace"))
        events_layout.addWidget(self.events)
        layout.addWidget(events_box, 1)

    def config(self) -> MegaConfig:
        try:
            baud = int(self.quella_baud.currentText())
        except ValueError:
            baud = 9600
        return MegaConfig(
            BridgeMode(self.mode.currentData()), baud, self.idle_gap.value(), self.sniff.isChecked()
        )

    def tank_config(self) -> TankConfig:
        return TankConfig(
            self.external_delay_enabled.isChecked(), self.external_delay_ms.value()
        )

    def load_config(self, config: MegaConfig) -> None:
        index = self.mode.findData(config.mode)
        if index >= 0:
            self.mode.setCurrentIndex(index)
        self.quella_baud.setCurrentText(str(config.quella_baud))
        self.idle_gap.setValue(config.idle_gap_us)
        self.sniff.setChecked(config.sniff_enabled)

    def _inject(self) -> None:
        try:
            raw = parse_hex_bytes(self.hex_input.text())
            if not raw:
                raise ValueError("enter at least one byte")
            if len(raw) > 64:
                raise ValueError("Mega injection frames are limited to 64 bytes")
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid injection", str(exc))
            return
        self.inject_requested.emit(BridgeDirection(self.direction.currentData()), raw)

    def _queue_tds_test(self) -> None:
        if self.mode.currentData() != BridgeMode.DISPLAY_ONLY_TEST:
            QMessageBox.warning(
                self,
                "Unsafe bridge mode",
                "TDS mapping requires DISPLAY_ONLY_TEST and a physically disconnected Main MCU.",
            )
            return
        if not self._connected:
            QMessageBox.warning(self, "Disconnected", "Connect the Mega bridge first.")
            return
        self.tds_test_requested.emit(self.tds_raw_value.value())

    def update_tds_test_state(self, state: dict) -> None:
        if not state.get("enabled"):
            self.tds_test_status.setText("DISABLED")
            self.tds_test_status.setStyleSheet("")
            return
        raw_value = int(state.get("raw_value", 0))
        phase = str(state.get("phase", "unknown")).upper()
        self.tds_test_status.setText(
            f"ACTIVE | raw=0x{raw_value:02X} ({raw_value}) | phase={phase}"
        )
        self.tds_test_status.setStyleSheet("color:#55c57a;font-weight:bold")

    def _set_rule(self) -> None:
        payload = self.known_command.currentData()
        if not isinstance(payload, bytes):
            QMessageBox.warning(self, "No rule", "No known dispense command is available.")
            return
        try:
            rule = MegaRule(
                BridgeDirection(self.rule_direction.currentData()),
                payload,
                self.override_value.value(),
                FrameAction(self.rule_action.currentData()),
                self.manipulation_enabled.isChecked(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid rule", str(exc))
            return
        self.set_rule_requested.emit(rule)

    def _send_manual_dispense(self) -> None:
        if self.mode.currentData() != BridgeMode.DIRECT_PASS:
            QMessageBox.warning(self, "Unsafe bridge mode", "SEND DISPENSE requires DIRECT_PASS.")
            return
        if self.manipulation_enabled.isChecked():
            QMessageBox.warning(
                self, "Rule active", "Disable frame manipulation before the direct command test."
            )
            return
        # This is comparison scenario C by definition. Start before queueing so
        # the first resulting bus traffic is retained.
        index = self.capture_scenario.findData("C")
        if index >= 0:
            self.capture_scenario.setCurrentIndex(index)
        if not self.sequence_capture.active:
            self._begin_capture("manual Mega DISPENSE command")
        self.send_dispense.setEnabled(False)
        self.remote_session_status.setText("STARTING")
        self.manual_dispense_requested.emit(
            self.manual_dispense_volume.value(), self.manual_feed_pump_parameter.value()
        )

    def _stop_remote_dispense(self) -> None:
        self.send_dispense.setEnabled(False)
        self.stop_remote_dispense.setEnabled(False)
        self.remote_session_status.setText("ABORTING")
        self.stop_remote_dispense_requested.emit()

    def _begin_capture(self, trigger: str, timestamp_us: int | None = None) -> None:
        if self.sequence_capture.active:
            self._finish_capture("restarted by operator/trigger")
        scenario = str(self.capture_scenario.currentData())
        self._capture_duration_s = self.capture_duration.value()
        self.sequence_capture.duration_us = self._capture_duration_s * 1_000_000
        self.sequence_capture.start(scenario, trigger, timestamp_us)
        self._capture_host_started = time.monotonic()
        self._last_capture_path = None
        self.capture_timer.start()
        self._update_capture_status()

    def _capture_tick(self) -> None:
        if not self.sequence_capture.active:
            self.capture_timer.stop()
            return
        if time.monotonic() - self._capture_host_started >= self._capture_duration_s:
            self._finish_capture(f"{self._capture_duration_s} second duration complete")
        else:
            self._update_capture_status()

    def _finish_capture(self, reason: str) -> None:
        if not self.sequence_capture.active:
            return
        self.sequence_capture.stop(reason)
        self.capture_timer.stop()
        runtime = Path(__file__).resolve().parent.parent / "evidence" / "runtime"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = runtime / f"dispense_sequence_{self.sequence_capture.scenario}_{stamp}.jsonl"
        self._last_capture_path = self.sequence_capture.export_jsonl(target)
        self._update_capture_status()

    def _update_capture_status(self) -> None:
        elapsed = (
            time.monotonic() - self._capture_host_started
            if self.sequence_capture.active else self.sequence_capture.elapsed_us / 1_000_000.0
        )
        state = "ACTIVE" if self.sequence_capture.active else "INACTIVE"
        suffix = f" | saved={self._last_capture_path}" if self._last_capture_path else ""
        self.capture_status.setText(
            f"{state} | scenario={self.sequence_capture.scenario} | "
            f"frames={self.sequence_capture.frame_count} | elapsed={elapsed:.3f} s{suffix}"
        )
        self.capture_status.setStyleSheet(
            "color:#55c57a;font-weight:bold" if self.sequence_capture.active else ""
        )

    def _capture_bridge_frame(self, event: dict) -> None:
        direction = str(event.get("direction", ""))
        raw_text = str(event.get("raw", ""))
        timestamp_us = event.get("timestamp_us")
        if direction not in ("DISPLAY_TO_MAIN", "MAIN_TO_DISPLAY") or not raw_text:
            return
        try:
            raw = parse_hex_bytes(raw_text)
            timestamp = int(timestamp_us)
        except (TypeError, ValueError):
            return
        traffic_source = str(event.get("traffic_source", "UART_RX"))
        normal_display_trigger = traffic_source == "UART_RX"
        replace_trigger = (
            traffic_source == "MEGA_LOCAL_REPLACE_TX"
            and str(self.capture_scenario.currentData()) == "D"
        )
        if (
            not self.sequence_capture.active
            and self.auto_dispense_capture.isChecked()
            and direction == "DISPLAY_TO_MAIN"
            and event.get("checksum_valid") is True
            and is_display_dispense_start(raw)
            and (normal_display_trigger or replace_trigger)
        ):
            trigger = (
                "automatic REPLACE AA00 DISPENSE"
                if replace_trigger else "automatic valid Display AA00 DISPENSE"
            )
            self._begin_capture(trigger, timestamp)
            if replace_trigger and self._pending_replace_original is not None:
                self.sequence_capture.add_frame(
                    timestamp,
                    direction,
                    "AA",
                    self._pending_replace_original,
                    True,
                    "MEGA_RULE_ORIGINAL",
                )
        record = self.sequence_capture.add_frame(
            timestamp,
            direction,
            str(event.get("decoded_type", "UNKNOWN")),
            raw,
            event.get("checksum_valid"),
            traffic_source,
        )
        if record is not None:
            tags = ",".join(record["semantic_tags"]) or "—"
            self.capture_last_frame.setText(
                f"#{record['frame_index']} Δ{record['delta_us']} µs "
                f"{direction} {record['traffic_source']} {record['decoded_type']} [{tags}] {record['raw']}"
            )
            self._update_capture_status()

    def _export_capture_jsonl(self) -> None:
        default = Path(__file__).resolve().parent.parent / "evidence" / "runtime" / "dispense_sequence.jsonl"
        filename, _ = QFileDialog.getSaveFileName(self, "Export dispense sequence", str(default), "JSON Lines (*.jsonl)")
        if filename:
            self._last_capture_path = self.sequence_capture.export_jsonl(filename)
            self._update_capture_status()

    def _export_capture_csv(self) -> None:
        default = Path(__file__).resolve().parent.parent / "evidence" / "runtime" / "dispense_sequence.csv"
        filename, _ = QFileDialog.getSaveFileName(self, "Export dispense sequence", str(default), "CSV (*.csv)")
        if filename:
            self._last_capture_path = self.sequence_capture.export_csv(filename)
            self._update_capture_status()

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self.connection.setText("Connected" if connected else "Not connected")
        self.connection.setStyleSheet(
            "color:#55c57a;font-weight:bold" if connected else "color:#d65c5c;font-weight:bold"
        )
        remote_busy = self.remote_session_status.text() in ("ACTIVE", "ABORTING", "STARTING")
        self.send_dispense.setEnabled(connected and not remote_busy)
        self.stop_remote_dispense.setEnabled(connected and self.remote_session_status.text() == "ACTIVE")
        self.apply_tank_config.setEnabled(connected)
        self.tds_apply.setEnabled(connected)
        self.tds_disable.setEnabled(connected)
        if not connected:
            self.remote_session_status.setText("IDLE")
            self.update_tds_test_state({"enabled": False})
            self.stop_remote_dispense.setEnabled(False)
            self.manipulation_enabled.blockSignals(True)
            self.manipulation_enabled.setChecked(False)
            self.manipulation_enabled.blockSignals(False)
            self.rule_status.setText("OFFLINE — firmware returns to PASS when lease expires")
            self.manual_command_status.setText("Not connected")
            self.manual_command_status.setStyleSheet("")

    def append_event(self, event: dict) -> None:
        if str(event.get("event", "")) == "REPLACE_BUILD":
            try:
                self._pending_replace_original = parse_hex_bytes(str(event.get("original", "")))
            except ValueError:
                self._pending_replace_original = None
        self._capture_bridge_frame(event)
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        kind = str(event.get("event", "EVENT"))
        sequence = event.get("sequence", "—")
        direction = str(event.get("direction", "—"))
        detail = str(event.get("detail", event.get("status", "")))
        self.events.append(f"{timestamp}  {kind:<18} seq={sequence!s:<5} {direction:<20} {detail}")
        if kind in ("INJECT_REQUESTED", "INJECT_QUEUED", "INJECT_SENT", "ERROR"):
            row = self.queue.rowCount()
            self.queue.insertRow(row)
            values = (timestamp, sequence, direction, kind, event.get("raw", detail))
            for column, value in enumerate(values):
                self.queue.setItem(row, column, QTableWidgetItem(str(value)))
            self.queue.scrollToBottom()
        if kind == "HELLO":
            self.firmware.setText(detail or "ATmega2560")
        elif kind == "STATUS":
            self.status.setText(detail)
            self.counters.setText(
                f"Bridge drops: {event.get('dropped_bridge', 0)} | "
                f"Capture drops: {event.get('dropped_capture', 0)} | "
                f"Injection queue: {event.get('injection_queue', 0)}"
            )
        elif kind == "TANK_STATUS" or kind.startswith(("EXTERNAL_", "INTERNAL_TANK_", "FILL_STATE_")):
            def active_text(known_key: str, active_key: str) -> str:
                if not event.get(known_key, False):
                    return "UNKNOWN"
                return "ACTIVE" if event.get(active_key, False) else "INACTIVE"

            self.tank_internal_min.setText(active_text("internal_min_known", "internal_min_active"))
            if not event.get("internal_max_known", False):
                self.tank_internal_max.setText("UNKNOWN")
            else:
                self.tank_internal_max.setText(
                    "BELOW MAX" if event.get("internal_max_active", False) else "FULL / MAX REACHED"
                )
            raw_known = bool(event.get("external_raw_known", False))
            raw_available = bool(event.get("external_raw_available", False))
            stable_known = bool(event.get("external_stable_known", False))
            stable_available = bool(event.get("external_stable_available", False))
            self.tank_external_low.setText(
                "UNKNOWN" if not raw_known else ("INACTIVE" if raw_available else "ACTIVE")
            )
            self.tank_external_raw.setText(
                "UNKNOWN" if not raw_known else ("AVAILABLE" if raw_available else "NOT AVAILABLE")
            )
            self.tank_external_stable.setText(
                "UNKNOWN" if not stable_known else ("AVAILABLE" if stable_available else "BLOCKED")
            )
            delay_enabled = bool(event.get("delay_enabled", False))
            delay_ms = int(event.get("delay_ms", 0))
            remaining_ms = int(event.get("delay_remaining_ms", 0))
            self.external_delay_enabled.setChecked(delay_enabled)
            self.external_delay_ms.setValue(delay_ms)
            self.tank_delay_status.setText(
                f"{'YES' if delay_enabled else 'NO'} | {delay_ms} ms | remaining {remaining_ms} ms"
            )
            self.tank_fill_allowed.setText("YES" if event.get("fill_allowed", False) else "NO")
            self.tank_fill_active.setText(active_text("fill_active_known", "fill_active"))
            self.tank_last_frame.setText(str(event.get("raw", "")))
            age_ms = int(event.get("last_update_age_ms", 0xFFFFFFFF))
            self.tank_last_age.setText("UNKNOWN" if age_ms == 0xFFFFFFFF else f"{age_ms} ms")
        elif kind == "ERROR" and self.remote_session_status.text() == "STARTING":
            self.remote_session_status.setText("IDLE")
            self.manual_command_status.setText(detail)
            self.manual_command_status.setStyleSheet("color:#d65c5c;font-weight:bold")
            self.send_dispense.setEnabled(self._connected)
        elif kind == "RULE_STATUS":
            enabled = bool(event.get("enabled"))
            self.manipulation_enabled.blockSignals(True)
            self.manipulation_enabled.setChecked(enabled)
            self.manipulation_enabled.blockSignals(False)
            configured = bool(event.get("configured"))
            self.rule_status.setText(
                f"{'ENABLED' if enabled else 'PASS'} | "
                f"{'configured' if configured else 'no rule'} | "
                f"{event.get('direction', '—')} | {event.get('action', '—')} | "
                f"override={event.get('override_value', 0)} ml"
            )
            self.rule_status.setStyleSheet("")
            self._rule_match_count = int(event.get("match_count", 0))
            self._rule_replace_count = int(event.get("replace_count", 0))
            self._update_rule_counters()
            self.last_original.setText(str(event.get("original", "")))
            self.last_replacement.setText(str(event.get("replacement", "")))
        elif kind.startswith(("DISPENSE_", "REMOTE_")):
            active = bool(event.get("override_active", False))
            self.override_active.setText("YES" if active else "NO")
            self.override_active.setStyleSheet(
                "color:#55c57a;font-weight:bold" if active else ""
            )
            self.override_origin.setText(str(event.get("session_origin", "NONE")))
            self.override_target.setText(f"{event.get('target_ml', 0)} ml")
            self.override_original.setText(f"{event.get('original_display_ml', 0)} ml")
            self.override_refreshes.setText(str(event.get("refresh_count", 0)))
            self.override_blocked_aborts.setText(str(event.get("blocked_abort_count", 0)))
            self.override_progress.setText(f"{event.get('current_a5_ml', 0)} ml")
            self.override_main_dispensing.setText(
                "YES" if event.get("main_dispensing", False) else "NO"
            )
            self.override_elapsed.setText(f"{int(event.get('elapsed_us', 0)) / 1_000_000:.3f} s")
            origin = str(event.get("session_origin", "NONE"))
            aborting = bool(event.get("aborting", False))
            if origin == "MANUAL/REMOTE":
                remote_state = "ABORTING" if aborting else ("ACTIVE" if active else "IDLE")
                self.remote_session_status.setText(remote_state)
                self.remote_runtime.setText(
                    f"Target {event.get('target_ml', 0)} ml | "
                    f"A5 {event.get('current_a5_ml', 0)} ml | "
                    f"Main {'YES' if event.get('main_dispensing', False) else 'NO'} | "
                    f"suppressed idle {event.get('blocked_abort_count', 0)} | "
                    f"replaced refresh {event.get('refresh_count', 0)} | "
                    f"{int(event.get('elapsed_us', 0)) / 1_000_000:.3f} s"
                )
                self.send_dispense.setEnabled(self._connected and remote_state == "IDLE")
                self.stop_remote_dispense.setEnabled(self._connected and remote_state == "ACTIVE")
        elif kind == "RULE_MATCH":
            self._rule_match_count = int(event.get("match_count", self._rule_match_count))
            self._update_rule_counters()
            self.last_original.setText(str(event.get("original", "")))
        elif kind == "RULE_REPLACED":
            self._rule_replace_count = int(event.get("replace_count", self._rule_replace_count))
            self._update_rule_counters()
            self.last_original.setText(str(event.get("original", "")))
            self.last_replacement.setText(str(event.get("replacement", "")))
        elif kind == "REPLACE_BUILD":
            self.last_original.setText(str(event.get("original", "")))
            self.last_replacement.setText(str(event.get("replacement", "")))
            self.rule_status.setText(
                f"REPLACE built: {event.get('replacement_len', '—')} bytes | "
                f"SUM16 {event.get('replacement_checksum', '—')}"
            )
        elif kind == "REPLACE_TX_BEGIN":
            self.rule_status.setText(
                f"REPLACE TX begin: {event.get('target_uart', '—')} | "
                f"{event.get('length', '—')} bytes | gap {event.get('gap_us', '—')} µs"
            )
        elif kind == "REPLACE_TX_END":
            written = int(event.get("written", 0))
            length = int(event.get("length", 0))
            self.rule_status.setText(
                f"REPLACE TX {'complete' if written == length else 'INCOMPLETE'}: "
                f"{written}/{length} bytes | {event.get('duration_us', '—')} µs"
            )
            self.rule_status.setStyleSheet(
                "color:#55c57a;font-weight:bold" if written == length
                else "color:#d65c5c;font-weight:bold"
            )
        elif kind == "MANUAL_CMD_QUEUED":
            command = str(event.get("command", "DISPENSE"))
            amount = f"{event.get('volume_ml', '—')} ml"
            self.manual_command_status.setText(
                f"QUEUED: {command} — {amount}; waiting for safe gap"
            )
            self.manual_command_status.setStyleSheet("color:#d7b84b;font-weight:bold")
        elif kind == "MANUAL_CMD_TX_BEGIN":
            self.manual_command_status.setText(
                f"TX BEGIN: {event.get('target_uart', '—')} | {event.get('length', '—')} bytes"
            )
        elif kind == "MANUAL_CMD_TX_END":
            written = int(event.get("written", 0))
            length = int(event.get("length", 0))
            self.manual_command_status.setText(
                f"TX {'complete' if written == length else 'INCOMPLETE'}: "
                f"{written}/{length} bytes | {event.get('duration_us', '—')} µs; "
                "capturing Display follow-up for 1 s"
            )
            self.manual_command_status.setStyleSheet(
                "color:#55c57a;font-weight:bold" if written == length
                else "color:#d65c5c;font-weight:bold"
            )
        elif kind == "MANUAL_FOLLOWUP_DISPLAY":
            self.manual_command_status.setText(str(event.get("detail", "Display follow-up observed")))

    def _update_rule_counters(self) -> None:
        self.rule_counters.setText(
            f"Matches: {self._rule_match_count} | Replacements: {self._rule_replace_count}"
        )
