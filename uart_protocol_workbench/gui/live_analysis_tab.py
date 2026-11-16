"""Live recurring-frame changes correlated with physical-button markers."""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
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

from protocol.frames import frame_payload_class
from protocol.framing import FrameByteRole
from protocol.knowledge import CONFIDENCE_LEVELS, FIELD_CATEGORIES, ProtocolKnowledge
from protocol.live_analysis import ByteChange, LiveChangeAnalyzer, LiveFrameDifference


CHANGED_BITS_COLUMN = 16
CATEGORY_COLUMN = 17
KNOWN_MEANING_COLUMN = 18
CONFIDENCE_COLUMN = 19


def highlighted_frame_html(
    raw: bytes, difference: LiveFrameDifference | None
) -> tuple[str, str]:
    """Render only changed byte tokens in orange and describe their changed bits."""
    changes = {
        change.raw_offset: change
        for change in difference.changes
    } if difference else {}
    tokens: list[str] = []
    details: list[str] = []
    for offset, value in enumerate(raw):
        token = f"{value:02X}"
        change = changes.get(offset)
        if change is None:
            tokens.append(token)
            continue
        tokens.append(
            '<span style="background-color:#b85f32;color:white;">'
            f"{token}</span>"
        )
        bits = ", ".join(
            f"bit{bit.bit} {bit.before}→{bit.after}" for bit in change.bits
        )
        details.append(
            f"{change.logical_offset} (raw offset {offset}): "
            f"{change.before:02X} → {change.after:02X}; {bits}"
        )
    return "&nbsp;".join(tokens), "\n".join(details)


class LiveAnalysisTab(QWidget):
    """Show exact byte and bit transitions while UART traffic is arriving."""

    def __init__(self, knowledge: ProtocolKnowledge | None, parent=None) -> None:
        super().__init__(parent)
        self.knowledge = knowledge
        self.analyzer = LiveChangeAnalyzer()
        self.records: list[dict] = []
        self.record_signatures: set[tuple[str, str, str | None, int, int, int]] = set()
        self.summary_rows: dict[tuple[str, str, str | None, int], int] = {}
        self._build_ui()
        self.marker_shortcut = QShortcut(QKeySequence("F8"), self)
        self.marker_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.marker_shortcut.activated.connect(self.mark_action)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        controls = QGroupBox("Live change detection")
        grid = QGridLayout(controls)
        self.reference_mode = QComboBox()
        self.reference_mode.addItem("Previous frame of same type", "previous")
        self.reference_mode.addItem("Baseline variants + previous state", "baseline")
        self.reference_mode.currentIndexChanged.connect(
            lambda: self.analyzer.set_reference_mode(self.reference_mode.currentData())
        )
        self.marker_label = QComboBox()
        self.marker_label.setEditable(True)
        self.marker_label.addItems(
            ["Power touch", "Dispense touch", "Hot-water touch", "Lock touch", "Other action"]
        )
        if self.marker_label.lineEdit():
            self.marker_label.lineEdit().returnPressed.connect(self.mark_action)
        self.marker_window = QSpinBox()
        self.marker_window.setRange(100, 30000)
        self.marker_window.setValue(3000)
        self.marker_window.setSuffix(" ms")
        mark = QPushButton("Mark Button Press (F8)")
        mark.clicked.connect(self.mark_action)
        label_selected = QPushButton("Label Selected Changes")
        label_selected.clicked.connect(self.label_selected)
        self.baseline_button = QPushButton("Capture Baseline Variants")
        self.baseline_button.setToolTip(
            "Collect every recurring frame variant during the selected time window"
        )
        self.baseline_button.clicked.connect(self.set_baseline)
        self.baseline_window = QSpinBox()
        self.baseline_window.setRange(500, 30000)
        self.baseline_window.setValue(3000)
        self.baseline_window.setSingleStep(500)
        self.baseline_window.setSuffix(" ms")
        self.baseline_window.setToolTip(
            "How long normal alternating frame states are collected"
        )
        self.baseline_timer = QTimer(self)
        self.baseline_timer.setInterval(100)
        self.baseline_timer.timeout.connect(self._baseline_capture_tick)
        self._baseline_deadline = 0.0
        reset = QPushButton("Reset Live Analysis")
        reset.clicked.connect(self.reset_analysis)
        clear = QPushButton("Clear Change List")
        clear.clicked.connect(self.clear_changes)
        export_json = QPushButton("Export JSON")
        export_json.clicked.connect(lambda: self.export_changes("json"))
        export_csv = QPushButton("Export CSV")
        export_csv.clicked.connect(lambda: self.export_changes("csv"))
        grid.addWidget(QLabel("Compare against"), 0, 0)
        grid.addWidget(self.reference_mode, 0, 1)
        grid.addWidget(QLabel("Physical action / key"), 0, 2)
        grid.addWidget(self.marker_label, 0, 3)
        grid.addWidget(QLabel("Correlation window"), 0, 4)
        grid.addWidget(self.marker_window, 0, 5)
        grid.addWidget(mark, 0, 6)
        grid.addWidget(label_selected, 0, 7)
        grid.addWidget(self.baseline_button, 1, 0, 1, 2)
        grid.addWidget(QLabel("Baseline capture"), 1, 2)
        grid.addWidget(self.baseline_window, 1, 3)
        grid.addWidget(reset, 1, 4)
        grid.addWidget(clear, 1, 5)
        grid.addWidget(export_json, 1, 6)
        grid.addWidget(export_csv, 1, 7)
        outer.addWidget(controls)

        filters = QHBoxLayout()
        self.direction_filter = QComboBox()
        self.direction_filter.addItems(
            [
                "All directions",
                "DISPLAY -> EMU",
                "EMU -> DISPLAY",
                "EMU(DISPLAY) -> MAIN",
                "MAIN -> EMU(DISPLAY)",
            ]
        )
        self.direction_filter.currentIndexChanged.connect(self.rebuild_table)
        self.type_filter = QComboBox()
        self.type_filter.addItems(["All frame types", "AA", "A5", "E5 B2", "E5 E1", "AA ACK", "MANUAL"])
        self.type_filter.currentIndexChanged.connect(self.rebuild_table)
        self.hide_checksum = QCheckBox("Hide SUM16 changes")
        self.hide_checksum.setToolTip("Hide changes to SUM16 High and SUM16 Low")
        self.hide_checksum.toggled.connect(self.rebuild_table)
        self.pause_view = QCheckBox("Pause table view (capture continues)")
        self.pause_view.toggled.connect(lambda paused: None if paused else self.rebuild_table())
        self.auto_scroll = QCheckBox("Auto-scroll")
        self.auto_scroll.setChecked(True)
        self.marker_status = QLabel("No active action marker")
        self.marker_status.setStyleSheet("color:#aeb8c4")
        filters.addWidget(QLabel("Direction"))
        filters.addWidget(self.direction_filter)
        filters.addWidget(QLabel("Frame type"))
        filters.addWidget(self.type_filter)
        filters.addWidget(self.hide_checksum)
        filters.addWidget(self.pause_view)
        filters.addWidget(self.auto_scroll)
        filters.addStretch()
        filters.addWidget(self.marker_status)
        outer.addLayout(filters)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.changes = QTableWidget(0, 20)
        self.changes.setHorizontalHeaderLabels(
            [
                "Time", "Δ Marker", "Action", "Direction", "Frame", "Payload class",
                "Raw / logical offset",
                "Before HEX", "Before DEC", "Before BIN",
                "After HEX", "After DEC", "After BIN",
                "XOR HEX", "XOR DEC", "XOR BIN", "Changed bits", "Category",
                "Known meaning", "Confidence",
            ]
        )
        self.changes.setAlternatingRowColors(True)
        self.changes.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.changes.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.changes.setFont(QFont("monospace"))
        self.changes.itemSelectionChanged.connect(self.show_selected_detail)
        self.changes.itemChanged.connect(self._known_meaning_edited)
        self.changes.horizontalHeader().setStretchLastSection(True)
        self.changes.setColumnWidth(0, 105)
        self.changes.setColumnWidth(1, 90)
        self.changes.setColumnWidth(2, 130)
        self.changes.setColumnWidth(5, 95)
        self.changes.setColumnWidth(8, 95)
        self.changes.setColumnWidth(11, 95)
        self.changes.setColumnWidth(14, 95)
        self.changes.setColumnWidth(CATEGORY_COLUMN, 120)
        self.changes.setColumnWidth(KNOWN_MEANING_COLUMN, 300)
        self.changes.setColumnWidth(CONFIDENCE_COLUMN, 120)

        lower = QWidget()
        lower_layout = QHBoxLayout(lower)
        self.last_frames = QTableWidget(0, 6)
        self.last_frames.setHorizontalHeaderLabels(
            ["Direction", "Frame", "Payload class", "Last time", "Changed bytes", "Current raw frame"]
        )
        self.last_frames.setFont(QFont("monospace"))
        self.last_frames.horizontalHeader().setStretchLastSection(True)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont("monospace"))
        self.detail.setPlaceholderText(
            "Select a changed row to inspect binary values, individual bit transitions, and protocol knowledge."
        )
        lower_layout.addWidget(self.last_frames, 3)
        lower_layout.addWidget(self.detail, 2)
        splitter.addWidget(self.changes)
        splitter.addWidget(lower)
        splitter.setSizes([560, 250])
        outer.addWidget(splitter, 1)
        self.statistics = QLabel()
        outer.addWidget(self.statistics)
        self._update_statistics()

    def observe_frame(self, direction: str, frame_type: str, raw: bytes, timestamp: float) -> None:
        self.analyzer.marker_window_s = self.marker_window.value() / 1000.0
        difference = self.analyzer.observe(direction, frame_type, raw, timestamp)
        self._update_summary(direction, frame_type, raw, timestamp, difference)
        if difference is not None and difference.changes:
            added: list[int] = []
            for change in difference.changes:
                record = self._record(difference, change)
                signature = self._record_signature(record)
                if signature in self.record_signatures:
                    continue
                self.record_signatures.add(signature)
                self.records.append(record)
                added.append(len(self.records) - 1)
            if not self.pause_view.isChecked():
                for record_index in added:
                    if self._visible(self.records[record_index]):
                        self._append_record_row(record_index, self.records[record_index])
                if added and self.auto_scroll.isChecked():
                    self.changes.scrollToBottom()
        self._update_statistics()

    def mark_action(self) -> None:
        label = self.marker_label.currentText().strip()
        try:
            self.analyzer.marker_window_s = self.marker_window.value() / 1000.0
            self.analyzer.mark(label)
        except ValueError as exc:
            QMessageBox.warning(self, "Action marker", str(exc))
            return
        self.marker_status.setText(
            f"ACTIVE: {label} — next {self.marker_window.value()} ms"
        )
        self.marker_status.setStyleSheet("color:#73d795;font-weight:bold")

    def label_selected(self) -> None:
        label = self.marker_label.currentText().strip()
        if not label:
            QMessageBox.warning(self, "Action label", "Enter an action label first.")
            return
        indexes = {
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.changes.selectedItems()
            if isinstance(item.data(Qt.ItemDataRole.UserRole), int)
        }
        if not indexes:
            QMessageBox.information(self, "Label changes", "Select one or more change rows first.")
            return
        for index in indexes:
            self.records[index]["marker"] = label
            self.records[index]["marker_delta_ms"] = None
        self.rebuild_table()

    def set_baseline(self) -> None:
        if self.analyzer.baseline_capture_active:
            self._finish_baseline_capture()
            return
        self.analyzer.begin_baseline_capture()
        self._baseline_deadline = (
            time.monotonic() + self.baseline_window.value() / 1000.0
        )
        self.baseline_window.setEnabled(False)
        self.baseline_button.setText("Finish Baseline Capture Now")
        self.baseline_timer.start()
        self._show_baseline_capture_status()

    def _baseline_capture_tick(self) -> None:
        if time.monotonic() >= self._baseline_deadline:
            self._finish_baseline_capture()
        else:
            self._show_baseline_capture_status()

    def _show_baseline_capture_status(self) -> None:
        remaining = max(0.0, self._baseline_deadline - time.monotonic())
        captured = sum(
            len(variants)
            for variants in self.analyzer.baseline_capture_variants.values()
        )
        self.marker_status.setText(
            f"Capturing baseline: {remaining:.1f}s remaining — {captured} variant(s)"
        )
        self.marker_status.setStyleSheet("color:#f1c56b;font-weight:bold")

    def _finish_baseline_capture(self) -> None:
        self.baseline_timer.stop()
        self.analyzer.finish_baseline_capture()
        self.reference_mode.setCurrentIndex(self.reference_mode.findData("baseline"))
        self._discard_baseline_records()
        self.baseline_window.setEnabled(True)
        self.baseline_button.setText("Capture Baseline Variants")
        self.marker_status.setText(
            f"Baseline captured: {self.analyzer.baseline_variant_count} variant(s) across "
            f"{len(self.analyzer.baselines)} recurring frame group(s)"
        )
        self.marker_status.setStyleSheet("color:#72b7ff")
        self._update_statistics()

    def _discard_baseline_records(self) -> None:
        self.analyzer.discard_baseline_transitions()
        self.records = [
            record
            for record in self.records
            if not self.analyzer.is_baseline_transition(
                record["direction"],
                record["frame_type"],
                record["payload_class"],
                bytes.fromhex(record["reference_frame"]),
                bytes.fromhex(record["current_frame"]),
            )
        ]
        self.record_signatures = {
            self._record_signature(record) for record in self.records
        }
        self.rebuild_table()

    def reset_analysis(self) -> None:
        self.baseline_timer.stop()
        self.analyzer.reset()
        self.baseline_window.setEnabled(True)
        self.baseline_button.setText("Capture Baseline Variants")
        self.records.clear()
        self.record_signatures.clear()
        self.summary_rows.clear()
        self.changes.setRowCount(0)
        self.last_frames.setRowCount(0)
        self.detail.clear()
        self.marker_status.setText("No active action marker")
        self.marker_status.setStyleSheet("color:#aeb8c4")
        self._update_statistics()

    def clear_changes(self) -> None:
        self.analyzer.clear_events()
        self.records.clear()
        self.record_signatures.clear()
        self.changes.setRowCount(0)
        self.detail.clear()
        self._update_statistics()

    def _record(self, difference: LiveFrameDifference, change: ByteChange) -> dict:
        result = change.to_dict()
        result.update(
            timestamp=difference.timestamp,
            timestamp_iso=datetime.fromtimestamp(difference.timestamp).astimezone().isoformat(timespec="milliseconds"),
            direction=difference.direction,
            frame_type=difference.frame_type,
            payload_class=difference.payload_class,
            marker=difference.marker,
            marker_delta_ms=difference.marker_delta_ms,
            reference_frame=difference.reference.hex(" ").upper(),
            current_frame=difference.current.hex(" ").upper(),
        )
        self._refresh_record_knowledge(result)
        return result

    @staticmethod
    def _record_signature(record: dict) -> tuple[str, str, str | None, int, int, int]:
        """Identify one unique byte transition while retaining its reverse separately."""
        return (
            record["direction"],
            record["frame_type"],
            record["payload_class"],
            record["raw_offset"],
            record["before_decimal"],
            record["after_decimal"],
        )

    def _refresh_record_knowledge(self, record: dict) -> None:
        record.update(
            knowledge_dataset=None,
            knowledge_row=None,
            knowledge_bit=None,
            knowledge_bits=[],
            knowledge_editable=False,
            knowledge_target=record["logical_offset"],
            category="UNKNOWN",
            known_meaning="UNKNOWN",
            knowledge_confidence="UNKNOWN",
        )
        byte_role = record.get("byte_role", FrameByteRole.UNKNOWN.value)
        if byte_role == FrameByteRole.CHECKSUM_HIGH.value:
            record.update(
                knowledge_target="SUM16 High",
                category="CHECKSUM",
                known_meaning="SUM16_H",
                knowledge_confidence="CONFIRMED",
            )
            return
        if byte_role == FrameByteRole.CHECKSUM_LOW.value:
            record.update(
                knowledge_target="SUM16 Low",
                category="CHECKSUM",
                known_meaning="SUM16_L",
                knowledge_confidence="CONFIRMED",
            )
            return
        if byte_role == FrameByteRole.HEADER.value:
            record.update(
                knowledge_target="AA Header",
                category="PROTOCOL",
                known_meaning="AA frame header (excluded from SUM16)",
                knowledge_confidence="CONFIRMED",
            )
            return
        if self.knowledge is None:
            return
        if byte_role != FrameByteRole.PAYLOAD.value:
            return
        if record["frame_type"] == "AA":
            dataset = "AA"
        elif record["frame_type"] in ("A5", "E5 B2", "E5 E1"):
            dataset = record["frame_type"]
        else:
            return
        logical_offset = record["logical_offset"]
        if logical_offset.startswith("Data["):
            row = int(logical_offset[5:-1])
        elif logical_offset == "SUM16 High":
            row = 16
        elif logical_offset == "SUM16 Low":
            row = 17
        else:
            return
        try:
            state_id = self._knowledge_state_id(record)
            field = self.knowledge.field(dataset, row, state_id)
            category = field.get("category", "PROTOCOL") if row < 16 else "PROTOCOL"
            bit = None
            description = field
            bit_details = []
            if category == "BITFIELD" and record["bit_changes"]:
                for transition in record["bit_changes"]:
                    changed_bit = int(transition["bit"])
                    bit_description = self.knowledge.bit(
                        dataset, row, changed_bit, state_id
                    )
                    bit_details.append(
                        {
                            "bit": changed_bit,
                            "before": int(transition["before"]),
                            "after": int(transition["after"]),
                            "meaning": bit_description["meaning"],
                            "confidence": bit_description["confidence"],
                        }
                    )
                if len(bit_details) == 1:
                    bit = bit_details[0]["bit"]
                    description = bit_details[0]
                else:
                    description = {
                        "meaning": "; ".join(
                            f"bit{entry['bit']} {entry['before']}→{entry['after']}: "
                            f"{entry['meaning']}"
                            for entry in bit_details
                        ),
                        "confidence": (
                            bit_details[0]["confidence"]
                            if len({entry["confidence"] for entry in bit_details}) == 1
                            else "MIXED"
                        ),
                    }
            record.update(
                knowledge_dataset=dataset,
                knowledge_row=row,
                knowledge_bit=bit,
                knowledge_bits=bit_details,
                knowledge_editable=category != "BITFIELD" or len(bit_details) == 1,
                knowledge_target=(
                    f"{logical_offset}.bit{bit}"
                    if bit is not None
                    else (
                        f"{logical_offset}.bits"
                        + ",".join(str(entry["bit"]) for entry in bit_details)
                        if bit_details
                        else logical_offset
                    )
                ),
                category=category,
                known_meaning=description["meaning"],
                knowledge_confidence=description["confidence"],
            )
        except (ValueError, KeyError, IndexError):
            return

    @staticmethod
    def _knowledge_state_id(record: dict) -> str | None:
        if record.get("frame_type") != "AA":
            return None
        payload_class = record.get("payload_class") or ""
        if payload_class.startswith("CONTROL / SETPOINT"):
            return "control_setpoint_00"
        if "Data[15]=A3" in payload_class:
            return "system_process_a3"
        if "Data[15]=E1" in payload_class:
            return "system_process_e1"
        return "unknown"

    def _known_meaning_edited(self, item: QTableWidgetItem) -> None:
        if item.column() != KNOWN_MEANING_COLUMN or self.knowledge is None:
            return
        record_index = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(record_index, int) or not 0 <= record_index < len(self.records):
            return
        record = self.records[record_index]
        if not record.get("knowledge_editable", False):
            self._restore_known_meaning(item, record["known_meaning"])
            return
        if item.text() == record["known_meaning"]:
            return
        dataset = record["knowledge_dataset"]
        row = record["knowledge_row"]
        bit = record["knowledge_bit"]
        if dataset is None or row is None:
            self._restore_known_meaning(item, record["known_meaning"])
            return
        try:
            if bit is None:
                self.knowledge.set_field(
                    dataset,
                    row,
                    item.text(),
                    record["knowledge_confidence"],
                    self._knowledge_state_id(record),
                )
            else:
                self.knowledge.set_bit(
                    dataset,
                    row,
                    bit,
                    item.text(),
                    record["knowledge_confidence"],
                    self._knowledge_state_id(record),
                )
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self._restore_known_meaning(item, record["known_meaning"])
            return
        saved = item.text().strip() or "UNKNOWN"
        record["known_meaning"] = saved
        self._restore_known_meaning(item, saved)
        self.marker_status.setText(
            f"Knowledge saved: {record['frame_type']} {record['knowledge_target']}"
        )
        self.marker_status.setStyleSheet("color:#73d795;font-weight:bold")

    def _category_changed(self, record_index: int, category: str) -> None:
        if self.knowledge is None or not 0 <= record_index < len(self.records):
            return
        record = self.records[record_index]
        dataset = record["knowledge_dataset"]
        row = record["knowledge_row"]
        if dataset is None or row is None or not 0 <= row < 16:
            return
        try:
            self.knowledge.set_category(dataset, row, category, self._knowledge_state_id(record))
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self.rebuild_table()
            return
        self._refresh_matching_knowledge(dataset, row, self._knowledge_state_id(record))
        self.rebuild_table()
        self.marker_status.setText(
            f"Category saved: {record['frame_type']} {record['logical_offset']} = {category}"
        )
        self.marker_status.setStyleSheet("color:#73d795;font-weight:bold")

    def _confidence_changed(self, record_index: int, confidence: str) -> None:
        if self.knowledge is None or not 0 <= record_index < len(self.records):
            return
        record = self.records[record_index]
        if not record.get("knowledge_editable", False):
            return
        dataset = record["knowledge_dataset"]
        row = record["knowledge_row"]
        bit = record["knowledge_bit"]
        if dataset is None or row is None or confidence == record["knowledge_confidence"]:
            return
        try:
            if bit is None:
                self.knowledge.set_field(
                    dataset,
                    row,
                    record["known_meaning"],
                    confidence,
                    self._knowledge_state_id(record),
                )
            else:
                self.knowledge.set_bit(
                    dataset,
                    row,
                    bit,
                    record["known_meaning"],
                    confidence,
                    self._knowledge_state_id(record),
                )
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self.rebuild_table()
            return
        record["knowledge_confidence"] = confidence
        self.marker_status.setText(
            f"Confidence saved: {record['frame_type']} {record['knowledge_target']} = {confidence}"
        )
        self.marker_status.setStyleSheet("color:#73d795;font-weight:bold")

    def _refresh_matching_knowledge(
        self, dataset: str, row: int, state_id: str | None
    ) -> None:
        for record in self.records:
            if (
                record["knowledge_dataset"] == dataset
                and record["knowledge_row"] == row
                and self._knowledge_state_id(record) == state_id
            ):
                self._refresh_record_knowledge(record)

    def _restore_known_meaning(self, item: QTableWidgetItem, value: str) -> None:
        self.changes.blockSignals(True)
        item.setText(value)
        self.changes.blockSignals(False)

    def rebuild_table(self) -> None:
        visible = [
            (index, record)
            for index, record in enumerate(self.records)
            if self._visible(record)
        ]
        self.changes.setRowCount(0)
        for record_index, record in visible:
            self._append_record_row(record_index, record)
        if visible and self.auto_scroll.isChecked():
            self.changes.scrollToBottom()

    def _append_record_row(self, record_index: int, record: dict) -> None:
        row = self.changes.rowCount()
        self.changes.insertRow(row)
        bit_text = ", ".join(
            f"bit{bit['bit']} {bit['before']}→{bit['after']}"
            for bit in record["bit_changes"]
        )
        marker_delta = (
            "—"
            if record["marker_delta_ms"] is None
            else f"{record['marker_delta_ms']:+.1f} ms"
        )
        values = (
            datetime.fromtimestamp(record["timestamp"]).strftime("%H:%M:%S.%f")[:-3],
            marker_delta,
            record["marker"] or "—",
            record["direction"],
            record["frame_type"],
            record["payload_class"] or "—",
            f"Raw[{record['raw_offset']}] / {record['knowledge_target']}",
            record["before_hex"],
            str(record["before_decimal"]),
            record["before_binary"],
            record["after_hex"],
            str(record["after_decimal"]),
            record["after_binary"],
            record["xor_hex"],
            str(record["xor_decimal"]),
            record["xor_binary"],
            bit_text,
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setData(Qt.ItemDataRole.UserRole, record_index)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if record["marker"]:
                item.setBackground(QColor("#284c39"))
            elif record["direction"] == "DISPLAY -> EMU":
                item.setBackground(QColor("#253d52"))
            self.changes.setItem(row, column, item)

        category = QComboBox()
        if record["knowledge_dataset"] is not None and isinstance(record["knowledge_row"], int):
            if record["knowledge_row"] < 16:
                category.addItems(FIELD_CATEGORIES)
                category.setCurrentText(record["category"])
                category.currentTextChanged.connect(
                    lambda value, index=record_index: self._category_changed(index, value)
                )
            else:
                category.addItem("PROTOCOL")
                category.setEnabled(False)
        else:
            category.addItem(record["category"])
            category.setEnabled(False)
        self.changes.setCellWidget(row, CATEGORY_COLUMN, category)

        meaning = QTableWidgetItem(record["known_meaning"])
        meaning.setData(Qt.ItemDataRole.UserRole, record_index)
        if (
            record["knowledge_dataset"] is None
            or not record.get("knowledge_editable", False)
        ):
            meaning.setFlags(meaning.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if record.get("knowledge_bits") and len(record["knowledge_bits"]) > 1:
            meaning.setToolTip(
                "Combined view of all changed bits. Edit individual bit meanings in the bit editor."
            )
        if record["marker"]:
            meaning.setBackground(QColor("#284c39"))
        elif record["direction"] == "DISPLAY -> EMU":
            meaning.setBackground(QColor("#253d52"))
        self.changes.setItem(row, KNOWN_MEANING_COLUMN, meaning)

        confidence = QComboBox()
        confidence.addItems(CONFIDENCE_LEVELS)
        if record["knowledge_confidence"] not in CONFIDENCE_LEVELS:
            confidence.addItem(record["knowledge_confidence"])
        confidence.setCurrentText(record["knowledge_confidence"])
        confidence.setEnabled(
            record["knowledge_dataset"] is not None
            and record.get("knowledge_editable", False)
        )
        confidence.currentTextChanged.connect(
            lambda value, index=record_index: self._confidence_changed(index, value)
        )
        self.changes.setCellWidget(row, CONFIDENCE_COLUMN, confidence)

    def _visible(self, record: dict) -> bool:
        direction = self.direction_filter.currentText()
        frame_type = self.type_filter.currentText()
        return (
            (direction == "All directions" or record["direction"] == direction)
            and (frame_type == "All frame types" or record["frame_type"] == frame_type)
            and (
                not self.hide_checksum.isChecked()
                or record.get("byte_role")
                not in (
                    FrameByteRole.CHECKSUM_HIGH.value,
                    FrameByteRole.CHECKSUM_LOW.value,
                )
            )
        )

    def _update_summary(
        self,
        direction: str,
        frame_type: str,
        raw: bytes,
        timestamp: float,
        difference: LiveFrameDifference | None,
    ) -> None:
        payload_class = (
            difference.payload_class
            if difference is not None
            else frame_payload_class(frame_type, raw)
        )
        key = (direction, frame_type, payload_class, len(raw))
        row = self.summary_rows.get(key)
        if row is None:
            row = self.last_frames.rowCount()
            self.last_frames.insertRow(row)
            self.summary_rows[key] = row
        values = (
            direction,
            frame_type,
            payload_class or "—",
            datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")[:-3],
            str(len(difference.changes) if difference else 0),
            raw.hex(" ").upper(),
        )
        for column, value in enumerate(values[:5]):
            item = QTableWidgetItem(value)
            self.last_frames.setItem(row, column, item)
        # Column 4 is rendered exclusively by the rich-text label below. Keeping
        # a QTableWidgetItem there would paint the raw frame a second time behind it.
        self.last_frames.takeItem(row, 5)
        frame_html, bit_details = highlighted_frame_html(raw, difference)
        raw_frame = QLabel(frame_html)
        raw_frame.setTextFormat(Qt.TextFormat.RichText)
        raw_frame.setFont(QFont("monospace"))
        raw_frame.setStyleSheet("background:transparent;padding:0 4px")
        raw_frame.setToolTip(bit_details or "No byte or bit changes against the selected reference")
        self.last_frames.setCellWidget(row, 5, raw_frame)

    def show_selected_detail(self) -> None:
        rows = self.changes.selectionModel().selectedRows()
        if not rows:
            return
        item = self.changes.item(rows[0].row(), 0)
        record_index = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(record_index, int) or not 0 <= record_index < len(self.records):
            return
        record = self.records[record_index]
        bit_lines = [
            f"  bit{bit['bit']}: {bit['before']} → {bit['after']}"
            for bit in record["bit_changes"]
        ] or ["  none"]
        self.detail.setPlainText(
            "\n".join(
                [
                    f"{record['direction']}  {record['frame_type']}  {record['knowledge_target']}",
                    f"Action: {record['marker'] or '—'}",
                    f"Before: {record['before_hex']} / {record['before_decimal']} / {record['before_binary']}",
                    f"After:  {record['after_hex']} / {record['after_decimal']} / {record['after_binary']}",
                    f"XOR:    {record['xor_hex']}",
                    "Bit transitions:",
                    *bit_lines,
                    "",
                    f"Category: {record['category']}",
                    f"Known meaning: {record['known_meaning']}",
                    f"Confidence: {record['knowledge_confidence']}",
                    "",
                    f"Reference frame: {record['reference_frame']}",
                    f"Current frame:   {record['current_frame']}",
                ]
            )
        )

    def _update_statistics(self) -> None:
        self.statistics.setText(
            f"Frames observed: {self.analyzer.frames_observed}   |   "
            f"Changed frames: {self.analyzer.frames_changed}   |   "
            f"Changed bytes: {self.analyzer.bytes_changed}   |   "
            f"Unique change rows: {len(self.records)}"
        )

    def export_changes(self, output_type: str) -> None:
        extension = "json" if output_type == "json" else "csv"
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export live changes", f"live-uart-changes.{extension}"
        )
        if not filename:
            return
        try:
            if output_type == "json":
                payload = {
                    "exported_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    "reference_mode": self.analyzer.reference_mode,
                    "records": self.records,
                }
                Path(filename).write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
            else:
                headers = [
                    "timestamp_iso", "marker", "marker_delta_ms", "direction", "frame_type",
                    "logical_offset", "raw_offset", "before_hex", "before_decimal",
                    "before_binary", "after_hex", "after_decimal", "after_binary",
                    "xor_hex", "xor_decimal", "xor_binary", "bit_changes", "known_meaning",
                    "knowledge_target", "category", "knowledge_confidence",
                    "reference_frame", "current_frame",
                ]
                with Path(filename).open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
                    writer.writeheader()
                    for record in self.records:
                        row = dict(record)
                        row["bit_changes"] = json.dumps(row["bit_changes"])
                        writer.writerow(row)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
