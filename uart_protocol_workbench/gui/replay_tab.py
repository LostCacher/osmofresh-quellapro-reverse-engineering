"""Startup replay controls, trace, comparisons, and TDS observations."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from protocol.checksum import calculate_sum16, sum16_bytes, validate_response
from protocol.frames import Dataset, ResponseFrame, parse_hex_bytes
from protocol.framing import frame_byte_metadata
from protocol.replay import ReplayMode, ReplayProfile, RequestType


MODE_LABELS = {
    "Static": ReplayMode.STATIC,
    "Startup Replay": ReplayMode.STARTUP_REPLAY,
    "Manual Step": ReplayMode.MANUAL_STEP,
    "Capture/Learn": ReplayMode.CAPTURE_LEARN,
}
MODE_NAMES = {mode.value: label for label, mode in MODE_LABELS.items()}


class ReplayTab(QWidget):
    profile_changed = Signal(object)
    start_requested = Signal(object)
    reset_requested = Signal()
    pause_requested = Signal(bool)
    next_requested = Signal()
    static_requested = Signal()

    def __init__(self, static_frames: dict[Dataset, ResponseFrame], parent=None) -> None:
        super().__init__(parent)
        self.static_frames = static_frames
        self.profiles_dir = Path(__file__).resolve().parent.parent / "config" / "startup_profiles"
        self.logs_dir = Path(__file__).resolve().parent.parent / "logs"
        self.current_profile = ReplayProfile.from_dict({"name": "Empty", "sequences": {}})
        self.last_state: dict = self._empty_state()
        self.trace_events: list[dict] = []
        self._elapsed_base = 0.0
        self._elapsed_updated_at = time.monotonic()
        self._build_ui()
        self.refresh_profiles()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_elapsed_clock)
        self._timer.start(100)

    @staticmethod
    def _empty_state() -> dict:
        return {
            "mode": ReplayMode.STATIC.value,
            "paused": False,
            "phase": 0,
            "indices": {request.value: 0 for request in RequestType},
            "aa_frames_received": 0,
            "started_epoch": None,
            "elapsed_s": 0.0,
            "current_responses": {},
            "last_aa": None,
            "capture_trace": [],
        }

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        controls = QGroupBox("Request-driven replay control")
        grid = QGridLayout(controls)
        self.mode = QComboBox()
        self.mode.addItems(MODE_LABELS)
        self.mode.currentTextChanged.connect(self._mode_selection_changed)
        self.profile = QComboBox()
        self.profile.currentIndexChanged.connect(self._selected_profile_changed)
        load = QPushButton("Load Profile")
        load.clicked.connect(self.load_profile_dialog)
        save = QPushButton("Save Profile")
        save.clicked.connect(self.save_profile_dialog)
        recalculate = QPushButton("Recalculate checksums…")
        recalculate.clicked.connect(self.recalculate_checksums)
        reset = QPushButton("Reset Startup Sequence")
        reset.setToolTip("Atomically reset A5, E5 B2, E5 E1, and AA cursors to their first state")
        reset.clicked.connect(self.reset_requested)
        start = QPushButton("Start Replay")
        start.clicked.connect(lambda: self.start_requested.emit(MODE_LABELS[self.mode.currentText()]))
        self.pause = QPushButton("Pause")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self._pause_toggled)
        self.next_step = QPushButton("Next Step")
        self.next_step.clicked.connect(self.next_requested)
        switch_static = QPushButton("Switch to Static")
        switch_static.clicked.connect(self.static_requested)
        export_capture = QPushButton("Export Capture Trace")
        export_capture.clicked.connect(self.export_capture_trace)
        grid.addWidget(QLabel("Mode"), 0, 0)
        grid.addWidget(self.mode, 0, 1)
        grid.addWidget(QLabel("Profile"), 0, 2)
        grid.addWidget(self.profile, 0, 3, 1, 2)
        grid.addWidget(load, 0, 5)
        grid.addWidget(save, 0, 6)
        grid.addWidget(recalculate, 0, 7)
        grid.addWidget(reset, 1, 0)
        grid.addWidget(start, 1, 1)
        grid.addWidget(self.pause, 1, 2)
        grid.addWidget(self.next_step, 1, 3)
        grid.addWidget(switch_static, 1, 4)
        grid.addWidget(export_capture, 1, 5)
        grid.setColumnStretch(4, 1)
        outer.addWidget(controls)

        self.profile_warnings = QLabel()
        self.profile_warnings.setWordWrap(True)
        self.profile_warnings.setStyleSheet("color:#efb35f")
        outer.addWidget(self.profile_warnings)

        status = QGroupBox("Replay state")
        status_grid = QGridLayout(status)
        self.state_labels: dict[str, QLabel] = {}
        fields = (
            ("mode", "Mode"),
            ("phase", "Current startup phase"),
            ("A5", "A5 sequence"),
            ("E5_B2", "E5 B2 sequence"),
            ("E5_E1", "E5 E1 sequence"),
            ("AA", "AA trigger sequence"),
            ("aa_count", "AA frames received"),
            ("start", "Replay start time"),
            ("elapsed", "Elapsed time"),
        )
        for index, (key, caption) in enumerate(fields):
            label = QLabel("—")
            self.state_labels[key] = label
            status_grid.addWidget(QLabel(caption), index // 3 * 2, index % 3)
            status_grid.addWidget(label, index // 3 * 2 + 1, index % 3)
        outer.addWidget(status)

        middle = QSplitter(Qt.Orientation.Horizontal)
        trace_box = QGroupBox("Live replay trace")
        trace_layout = QVBoxLayout(trace_box)
        self.trace = QTextEdit()
        self.trace.setReadOnly(True)
        self.trace.setFont(QFont("monospace"))
        clear_trace = QPushButton("Clear trace")
        clear_trace.clicked.connect(self.clear_trace)
        save_trace = QPushButton("Save replay trace")
        save_trace.clicked.connect(self.save_replay_trace)
        trace_buttons = QHBoxLayout()
        trace_buttons.addWidget(clear_trace)
        trace_buttons.addWidget(save_trace)
        trace_buttons.addStretch()
        trace_layout.addWidget(self.trace)
        trace_layout.addLayout(trace_buttons)
        middle.addWidget(trace_box)

        compare_box = QGroupBox("Captured Startup State vs Current Static State")
        compare_layout = QVBoxLayout(compare_box)
        self.compare_dataset = QComboBox()
        self.compare_dataset.addItems([dataset.value for dataset in Dataset])
        self.compare_dataset.currentTextChanged.connect(self.refresh_comparison)
        self.comparison = QTableWidget(18, 5)
        self.comparison.setHorizontalHeaderLabels(
            ["Offset", "Startup", "Static", "XOR", "Changed bits"]
        )
        self.comparison.horizontalHeader().setStretchLastSection(True)
        self.compare_checksum = QLabel()
        compare_layout.addWidget(self.compare_dataset)
        compare_layout.addWidget(self.comparison)
        compare_layout.addWidget(self.compare_checksum)
        middle.addWidget(compare_box)
        middle.setSizes([650, 650])
        outer.addWidget(middle, 1)

        experiment = QGroupBox("TDS investigation and protocol snapshot")
        experiment_layout = QHBoxLayout(experiment)
        self.tds_value = QSpinBox()
        self.tds_value.setRange(0, 9999)
        self.observation_notes = QLineEdit()
        self.observation_notes.setPlaceholderText("Visible effect / notes")
        record = QPushButton("Record Observation")
        record.clicked.connect(self.record_observation)
        snapshot = QPushButton("Save Current Protocol Snapshot")
        snapshot.clicked.connect(self.save_snapshot)
        experiment_layout.addWidget(QLabel("Display TDS"))
        experiment_layout.addWidget(self.tds_value)
        experiment_layout.addWidget(QLabel("Notes"))
        experiment_layout.addWidget(self.observation_notes, 1)
        experiment_layout.addWidget(record)
        experiment_layout.addWidget(snapshot)
        outer.addWidget(experiment)
        self._mode_selection_changed(self.mode.currentText())

    def refresh_profiles(self) -> None:
        current_path = self.profile.currentData()
        self.profile.blockSignals(True)
        self.profile.clear()
        for path in sorted(self.profiles_dir.glob("*.json")):
            self.profile.addItem(path.stem, str(path))
        self.profile.blockSignals(False)
        if current_path:
            index = self.profile.findData(current_path)
            if index >= 0:
                self.profile.setCurrentIndex(index)
        if self.profile.count():
            self._selected_profile_changed(self.profile.currentIndex())

    def _selected_profile_changed(self, index: int) -> None:
        if index < 0:
            return
        path = self.profile.itemData(index)
        if path:
            self._load_profile(path)

    def _load_profile(self, path: Path | str) -> None:
        try:
            self.current_profile = ReplayProfile.load(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "Invalid replay profile", str(exc))
            return
        self._show_profile_warnings()
        self.profile_changed.emit(self.current_profile)

    def load_profile_dialog(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Load startup replay profile", str(self.profiles_dir), "JSON (*.json)"
        )
        if filename:
            self._load_profile(filename)

    def save_profile_dialog(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save startup replay profile", str(self.profiles_dir / "startup-profile.json"), "JSON (*.json)"
        )
        if not filename:
            return
        try:
            self.current_profile.save(filename)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.refresh_profiles()

    def recalculate_checksums(self) -> None:
        invalid = [
            step
            for request in (RequestType.A5, RequestType.E5_B2, RequestType.E5_E1)
            for step in self.current_profile.sequences.get(request, [])
            if step.response is not None and len(step.response) == 18 and not validate_response(step.response)
        ]
        if not invalid:
            QMessageBox.information(self, "Checksums", "All 18-byte replay responses are already valid.")
            return
        answer = QMessageBox.question(
            self,
            "Recalculate captured checksums?",
            f"This explicitly changes the stored checksum in {len(invalid)} captured response(s). Continue?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for step in invalid:
            response = bytearray(step.response)
            response[16], response[17] = sum16_bytes(response[:16])
            step.response = bytes(response)
        self.current_profile = ReplayProfile.from_dict(self.current_profile.to_dict())
        self._show_profile_warnings()
        self.profile_changed.emit(self.current_profile)

    def _show_profile_warnings(self) -> None:
        if self.current_profile.warnings:
            self.profile_warnings.setText("Profile warnings:\n" + "\n".join(self.current_profile.warnings))
        else:
            self.profile_warnings.setText("Profile validation: all configured response checksums are valid.")

    def _mode_selection_changed(self, label: str) -> None:
        self.next_step.setEnabled(MODE_LABELS[label] is ReplayMode.MANUAL_STEP)

    def _pause_toggled(self, paused: bool) -> None:
        self.pause.setText("Resume" if paused else "Pause")
        self.pause_requested.emit(paused)

    def update_state(self, state: dict) -> None:
        self.last_state = state
        self._elapsed_base = float(state.get("elapsed_s", 0.0))
        self._elapsed_updated_at = time.monotonic()
        mode = state.get("mode", ReplayMode.STATIC.value)
        self.state_labels["mode"].setText(MODE_NAMES.get(mode, mode) + (" / PAUSED" if state.get("paused") else ""))
        self.state_labels["phase"].setText(str(state.get("phase", 0)))
        indices = state.get("indices", {})
        sequence_status = state.get("sequence_status", {})
        for key in ("A5", "E5_B2", "E5_E1", "AA"):
            details = sequence_status.get(key)
            if details:
                if details.get("hold"):
                    text = f"{details['name']}   HOLD"
                elif details.get("repeat", 0):
                    text = (
                        f"{details['name']}   repetition "
                        f"{details.get('repetition', 0)}/{details['repeat']}"
                    )
                else:
                    text = details.get("name", "STATIC")
                self.state_labels[key].setText(text)
            else:
                self.state_labels[key].setText(str(indices.get(key, 0)))
        self.state_labels["aa_count"].setText(str(state.get("aa_frames_received", 0)))
        started = state.get("started_epoch")
        self.state_labels["start"].setText(
            datetime.fromtimestamp(started).isoformat(timespec="milliseconds") if started else "—"
        )
        self.refresh_comparison()

    def _update_elapsed_clock(self) -> None:
        running = self.last_state.get("started_epoch") and not self.last_state.get("paused")
        elapsed = self._elapsed_base + (time.monotonic() - self._elapsed_updated_at if running else 0.0)
        self.state_labels["elapsed"].setText(f"{elapsed:.3f} s")

    def append_event(self, event: dict) -> None:
        self.trace_events.append(dict(event))
        elapsed = float(event.get("elapsed_s", 0.0))
        prefix = f"{elapsed:08.3f}"
        event_type = event.get("event")
        if event_type == "rx":
            request = event.get("request_type", "UNKNOWN")
            self.trace.append(f"{prefix}  RX {request}   {event.get('raw', '')}")
            if request == "AA":
                stored = event.get("checksum_stored")
                calculated = event.get("checksum_calculated")
                valid = event.get("aa_checksum_valid")
                self.trace.append(
                    f"          AA checksum {'VALID' if valid else 'INVALID'}  "
                    f"stored={self._hex_or_dash(stored)} calculated={self._hex_or_dash(calculated)}"
                )
                if event.get("aa_matched") is not None:
                    self.trace.append(f"          AA masked trigger match: {event['aa_matched']}")
            if event.get("timeout_exceeded"):
                self.trace.append("          WARNING: configured step timeout exceeded")
        elif event_type == "tx":
            state = f" state {event['state']}" if event.get("state") else ""
            self.trace.append(
                f"{prefix}  TX {event.get('request_type', '')}{state}   {event.get('raw', '')}"
            )
            if event.get("checksum_stored") is not None:
                self.trace.append(
                    f"          checksum {'VALID' if event.get('checksum_valid') else 'INVALID'}  "
                    f"stored={self._hex_or_dash(event.get('checksum_stored'))} "
                    f"calculated={self._hex_or_dash(event.get('checksum_calculated'))}"
                )
        else:
            self.trace.append(f"{prefix}  {event.get('text', event_type or 'EVENT')}")

    def clear_trace(self) -> None:
        self.trace_events.clear()
        self.trace.clear()

    def save_replay_trace(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save replay trace", "startup-replay-trace.json", "JSON (*.json)"
        )
        if not filename:
            return
        payload = {
            "profile": self.current_profile.name,
            "saved_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "events": self.trace_events,
        }
        try:
            Path(filename).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Trace save failed", str(exc))

    @staticmethod
    def _hex_or_dash(value) -> str:
        return "—" if value is None else f"{int(value):02X}"

    def refresh_comparison(self, ignored=None) -> None:
        dataset = Dataset(self.compare_dataset.currentText())
        static = self.static_frames[dataset].to_bytes()
        startup_hex = self.last_state.get("current_responses", {}).get(dataset.value)
        try:
            startup = parse_hex_bytes(startup_hex, 18) if startup_hex else static
        except ValueError:
            startup = static
        for row, (startup_value, static_value) in enumerate(zip(startup, static)):
            xor = startup_value ^ static_value
            changed_bits = ", ".join(f"bit{bit}" for bit in range(7, -1, -1) if xor & (1 << bit)) or "—"
            offset = frame_byte_metadata(dataset.value, len(startup), row).logical_offset
            for column, value in enumerate(
                (offset, f"{startup_value:02X}", f"{static_value:02X}", f"{xor:02X}", changed_bits)
            ):
                self.comparison.setItem(row, column, QTableWidgetItem(value))
        startup_status = self._checksum_text(startup)
        static_status = self._checksum_text(static)
        self.compare_checksum.setText(f"Startup: {startup_status}    |    Static: {static_status}")

    @staticmethod
    def _checksum_text(response: bytes) -> str:
        if len(response) != 18:
            return f"INVALID LENGTH ({len(response)})"
        calculated = calculate_sum16(response[:16])
        stored = (response[16] << 8) | response[17]
        return (
            f"stored {stored:04X}, calculated {calculated:04X}, "
            f"{'VALID' if validate_response(response) else 'INVALID'}"
        )

    def _observation(self) -> dict:
        current = self.last_state.get("current_responses", {})
        return {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "profile": self.current_profile.name,
            "mode": self.last_state.get("mode"),
            "replay_phase": self.last_state.get("phase", 0),
            "display_tds": self.tds_value.value(),
            "responses": {dataset.value: current.get(dataset.value, self.static_frames[dataset].to_hex()) for dataset in Dataset},
            "last_aa_frame": self.last_state.get("last_aa"),
            "notes": self.observation_notes.text(),
        }

    def record_observation(self) -> None:
        path = self.logs_dir / "tds_observations.json"
        try:
            observations = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            if not isinstance(observations, list):
                raise ValueError("existing observation file is not a JSON list")
            observations.append(self._observation())
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(observations, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "Observation save failed", str(exc))
            return
        self.trace.append(f"{self._elapsed_base:08.3f}  TDS OBSERVATION RECORDED: {self.tds_value.value()}")

    def save_snapshot(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save protocol snapshot", "protocol-snapshot.json", "JSON (*.json)"
        )
        if not filename:
            return
        try:
            Path(filename).write_text(json.dumps(self._observation(), indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Snapshot save failed", str(exc))

    def export_capture_trace(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export capture learning trace", "startup-capture-trace.json", "JSON (*.json)"
        )
        if not filename:
            return
        payload = {
            "profile": self.current_profile.name,
            "exported_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "events": self.last_state.get("capture_trace", []),
        }
        try:
            Path(filename).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Capture export failed", str(exc))
