"""Editable dataset frame, bit editor, presets, and single-bit test preview."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protocol.checksum import calculate_sum16, sum16_bytes, validate_response
from protocol.frames import Dataset, RequestResponseSet, ResponseFrame, ResponsePayloadState
from protocol.framing import frame_byte_metadata
from protocol.knowledge import (
    CONFIDENCE_LEVELS,
    FIELD_CATEGORIES,
    ProtocolKnowledge,
)


class FrameEditor(QWidget):
    frame_changed = Signal(str, bytes)
    response_set_changed = Signal(str, object)

    def __init__(
        self,
        frame: ResponseFrame,
        knowledge: ProtocolKnowledge,
        response_set: RequestResponseSet | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.frame = frame
        self.response_set = response_set or RequestResponseSet(
            frame.dataset,
            [ResponsePayloadState(frame, "observed_state_a", "Observed payload state A")],
        )
        self.knowledge = knowledge
        self._updating = False
        self._selected_global_offset: int | None = None
        self._live_response: bytes | None = None
        self._live_timestamp: float | None = None
        self._synchronize_global_knowledge()
        self.table = QTableWidget(18, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "Offset", "Category", "Hex", "Decimal", "Binary",
                "Known meaning", "Confidence", "Shared across payload states",
            ]
        )
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 80)
        self.table.setColumnWidth(1, 125)
        self.table.setColumnWidth(2, 70)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 100)
        self.table.setColumnWidth(5, 390)
        self.table.setColumnWidth(6, 125)
        self.table.setColumnWidth(7, 145)
        self.table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            7, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.category_boxes: list[QComboBox] = []
        self.confidence_boxes: list[QComboBox] = []
        self.global_toggles: list[QCheckBox] = []
        for row in range(18):
            confidence = QComboBox()
            confidence.setEditable(True)
            confidence.addItems(CONFIDENCE_LEVELS)
            confidence.currentTextChanged.connect(
                lambda ignored, selected_row=row: self._save_field_confidence(selected_row)
            )
            self.table.setCellWidget(row, 6, confidence)
            self.confidence_boxes.append(confidence)
            if row < 16:
                category = QComboBox()
                category.addItems(FIELD_CATEGORIES)
                category.currentTextChanged.connect(
                    lambda ignored, selected_row=row: self._category_changed(selected_row)
                )
                self.table.setCellWidget(row, 1, category)
                self.category_boxes.append(category)
                global_toggle = QCheckBox()
                global_toggle.setToolTip(
                    f"Share Data[{row}] across every {frame.dataset.value} payload state"
                )
                global_toggle.toggled.connect(
                    lambda checked, offset=row: self._global_toggled(offset, checked)
                )
                self.table.setCellWidget(row, 7, global_toggle)
                self.global_toggles.append(global_toggle)
            else:
                self.table.setCellWidget(row, 1, QLabel("PROTOCOL"))
                self.table.setCellWidget(row, 7, QLabel("—"))

        self.global_table = QTableWidget(0, 8)
        self.global_table.setHorizontalHeaderLabels(
            [
                "Offset", "Category", "Hex", "Decimal", "Binary",
                "Known meaning", "Confidence", "Shared across payload states",
            ]
        )
        self.global_table.verticalHeader().hide()
        self.global_table.setAlternatingRowColors(True)
        self.global_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.global_table.itemChanged.connect(self._global_item_changed)
        self.global_table.itemSelectionChanged.connect(self._global_selection_changed)
        self.global_table.horizontalHeader().setStretchLastSection(False)
        self.global_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch
        )
        self.global_table.horizontalHeader().setSectionResizeMode(
            7, QHeaderView.ResizeMode.ResizeToContents
        )
        self.global_table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.global_table.setMaximumHeight(155)
        for column, width in enumerate((80, 125, 70, 80, 100, 390, 125, 145)):
            self.global_table.setColumnWidth(column, width)
        self.global_category_boxes: dict[int, QComboBox] = {}
        self.global_confidence_boxes: dict[int, QComboBox] = {}

        self.auto_checksum = QCheckBox("Auto checksum")
        self.auto_checksum.setChecked(True)
        self.auto_checksum.toggled.connect(self._auto_checksum_changed)
        self.live_values = QCheckBox("Live values")
        self.live_values.setToolTip(
            "Show the latest received Main-to-Display response. "
            "Live values are read-only and do not overwrite the saved frame."
        )
        self.live_values.toggled.connect(self._live_values_toggled)
        self.live_status = QLabel("No live response received")
        self.live_status.setStyleSheet("color:#999999")
        checksum_help = QLabel(
            "SUM16 covers Data[0..15]; transmitted as high byte then low byte."
        )

        self.bit_group = QGroupBox("Bit editor — select a Data row")
        self.bit_layout = QGridLayout(self.bit_group)
        self.bit_buttons: list[QPushButton] = []
        self.bit_meanings: list[QLineEdit] = []
        self.bit_confidences: list[QComboBox] = []
        for column, bit in enumerate(range(7, -1, -1)):
            button = QPushButton(f"bit{bit}\n0")
            button.setCheckable(True)
            button.clicked.connect(lambda checked, selected_bit=bit: self._bit_clicked(selected_bit, checked))
            self.bit_layout.addWidget(button, 0, column)
            self.bit_buttons.append(button)
            meaning = QLineEdit()
            meaning.setPlaceholderText("UNKNOWN")
            meaning.setToolTip(f"Meaning of the selected byte's bit{bit}")
            meaning.editingFinished.connect(
                lambda selected_bit=bit: self._save_bit_description(selected_bit)
            )
            self.bit_layout.addWidget(meaning, 1, column)
            self.bit_meanings.append(meaning)
            confidence = QComboBox()
            confidence.setEditable(True)
            confidence.addItems(CONFIDENCE_LEVELS)
            confidence.setToolTip(f"Confidence for bit{bit}")
            confidence.currentTextChanged.connect(
                lambda ignored, selected_bit=bit: self._save_bit_description(selected_bit)
            )
            self.bit_layout.addWidget(confidence, 2, column)
            self.bit_confidences.append(confidence)

        layout = QVBoxLayout(self)
        self.state_box = QGroupBox("Observed response payload states — discriminator unknown")
        self.state_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        state_layout = QHBoxLayout(self.state_box)
        state_layout.setContentsMargins(8, 6, 8, 6)
        self.state_selector = QComboBox()
        self.state_selector.currentIndexChanged.connect(self._state_selected)
        self.state_enabled = QCheckBox("Enabled")
        self.state_enabled.toggled.connect(self._state_enabled_changed)
        add_state = QPushButton("Add State")
        add_state.clicked.connect(self._add_state)
        duplicate_state = QPushButton("Duplicate State")
        duplicate_state.clicked.connect(self._duplicate_state)
        delete_state = QPushButton("Delete State")
        delete_state.clicked.connect(self._delete_state)
        state_layout.addWidget(QLabel("Payload state"))
        state_layout.addWidget(self.state_selector, 1)
        state_layout.addWidget(self.state_enabled)
        state_layout.addWidget(add_state)
        state_layout.addWidget(duplicate_state)
        state_layout.addWidget(delete_state)
        layout.addWidget(self.state_box)
        controls = QHBoxLayout()
        controls.addWidget(self.auto_checksum)
        controls.addWidget(checksum_help)
        controls.addStretch()
        controls.addWidget(self.live_values)
        controls.addWidget(self.live_status)
        layout.addLayout(controls)
        self.global_group = QGroupBox(
            "Shared Data — request-level values shared across payload states"
        )
        self.global_group.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        global_layout = QVBoxLayout(self.global_group)
        global_layout.setContentsMargins(8, 6, 8, 6)
        global_layout.addWidget(self.global_table)
        layout.addWidget(self.global_group)
        meta_label = QLabel(
            "Payload-state-specific values and SUM16 — meanings are shared across all states"
        )
        meta_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        layout.addWidget(meta_label)
        layout.addWidget(self.table, 1)
        self.bit_group.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        layout.addWidget(self.bit_group)
        self.frame_changed.connect(lambda *_: self._response_set_modified())
        self._refresh_state_controls()
        self.refresh()
        self.table.selectRow(0)

    def refresh(self) -> None:
        self._updating = True
        raw = self._display_response()
        live = self._live_active()
        checksum_good = validate_response(raw)
        global_offsets = sorted(self.response_set.shared_data)
        for row, value in enumerate(raw):
            self.table.setRowHidden(row, row < 16 and row in self.response_set.shared_data)
            offset = frame_byte_metadata(
                self.frame.dataset.value, len(raw), row
            ).logical_offset
            description = self.knowledge.field(self.frame.dataset, row, self._current_state_id())
            values = {
                0: offset,
                2: f"{value:02X}",
                3: str(value),
                4: f"{value:08b}",
                5: description["meaning"],
            }
            for column, text in values.items():
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                item.setText(text)
                flags = item.flags()
                if (
                    not live
                    and column in (2, 3)
                    and (row < 16 or not self.frame.auto_checksum)
                ) or column == 5:
                    item.setFlags(flags | Qt.ItemFlag.ItemIsEditable)
                else:
                    item.setFlags(flags & ~Qt.ItemFlag.ItemIsEditable)
                if row >= 16:
                    item.setBackground(QColor("#174f2a" if checksum_good else "#672525"))
                elif live and column in (2, 3, 4):
                    item.setBackground(QColor("#203f58"))
                else:
                    item.setBackground(QColor())
            confidence = self.confidence_boxes[row]
            confidence.blockSignals(True)
            confidence.setCurrentText(description["confidence"])
            confidence.blockSignals(False)
            if row < 16:
                category = self.category_boxes[row]
                category.blockSignals(True)
                category.setCurrentText(description["category"])
                category.blockSignals(False)
                global_toggle = self.global_toggles[row]
                global_toggle.blockSignals(True)
                global_toggle.setChecked(row in self.response_set.shared_data)
                global_toggle.setEnabled(not live)
                global_toggle.blockSignals(False)

        self.global_table.setRowCount(len(global_offsets))
        self.global_table.setMaximumHeight(35 + min(len(global_offsets), 4) * 30)
        self.global_group.setMaximumHeight(60 + min(len(global_offsets), 4) * 30)
        self.global_category_boxes.clear()
        self.global_confidence_boxes.clear()
        for global_row, offset in enumerate(global_offsets):
            value = raw[offset] if live else self.response_set.shared_data[offset]
            description = self.knowledge.field(
                self.frame.dataset, offset, self._current_state_id()
            )
            values = {
                0: f"Data[{offset}]",
                2: f"{value:02X}",
                3: str(value),
                4: f"{value:08b}",
                5: description["meaning"],
            }
            for column, text in values.items():
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, offset)
                if column not in ((5,) if live else (2, 3, 5)):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if live and column in (2, 3, 4):
                    item.setBackground(QColor("#203f58"))
                self.global_table.setItem(global_row, column, item)
            category = QComboBox()
            category.addItems(FIELD_CATEGORIES)
            category.setCurrentText(description["category"])
            category.currentTextChanged.connect(
                lambda ignored, selected_offset=offset: self._global_category_changed(
                    selected_offset
                )
            )
            self.global_table.setCellWidget(global_row, 1, category)
            self.global_category_boxes[offset] = category
            confidence = QComboBox()
            confidence.setEditable(True)
            confidence.addItems(CONFIDENCE_LEVELS)
            confidence.setCurrentText(description["confidence"])
            confidence.currentTextChanged.connect(
                lambda ignored, selected_offset=offset: self._global_confidence_changed(
                    selected_offset
                )
            )
            self.global_table.setCellWidget(global_row, 6, confidence)
            self.global_confidence_boxes[offset] = confidence
            toggle = QCheckBox()
            toggle.setChecked(True)
            toggle.setEnabled(not live)
            toggle.toggled.connect(
                lambda checked, selected_offset=offset: self._global_toggled(
                    selected_offset, checked
                )
            )
            self.global_table.setCellWidget(global_row, 7, toggle)
        self._updating = False
        if self._selected_global_offset in global_offsets:
            global_row = global_offsets.index(self._selected_global_offset)
            self.global_table.blockSignals(True)
            self.global_table.selectRow(global_row)
            self.global_table.blockSignals(False)
        self._update_bit_editor()

    def _current_state(self) -> ResponsePayloadState:
        return next(
            state
            for state in self.response_set.payload_states
            if state.frame is self.frame
        )

    def _current_state_id(self) -> str:
        return self._current_state().state_id

    def _current_response(self) -> bytes:
        return self.response_set.response_for_state(self._current_state())

    def _live_active(self) -> bool:
        return self.live_values.isChecked() and self._live_response is not None

    def _display_response(self) -> bytes:
        if self._live_active():
            return self._live_response  # type: ignore[return-value]
        return self._current_response()

    def observe_live_response(self, raw: bytes, timestamp: float) -> None:
        """Cache a received response without changing the editable frame model."""
        if len(raw) != 18:
            return
        self._live_response = bytes(raw)
        self._live_timestamp = timestamp
        received = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")[:-3]
        self.live_status.setText(f"Latest response: {received}")
        self.live_status.setStyleSheet("color:#55c5d9")
        if self.live_values.isChecked():
            self.refresh()

    def _live_values_toggled(self, checked: bool) -> None:
        self.auto_checksum.setEnabled(not checked)
        if checked and self._live_response is None:
            self.live_status.setText("Waiting for live response …")
            self.live_status.setStyleSheet("color:#e0b84d")
        elif not checked and self._live_response is not None:
            self.live_status.setText("Live response available")
            self.live_status.setStyleSheet("color:#999999")
        self.refresh()

    def _synchronize_global_knowledge(self) -> None:
        stored = self.knowledge.shared_fields(self.frame.dataset)
        for raw_offset, entry in stored.items():
            offset = int(raw_offset)
            if offset not in self.response_set.shared_data:
                self.response_set.set_global(offset, int(entry["value"]))
        for offset, value in self.response_set.shared_data.items():
            if not self.knowledge.is_shared(self.frame.dataset, offset):
                self.knowledge.make_shared(
                    self.frame.dataset, offset, value, self._current_state_id()
                )
            elif int(self.knowledge.shared_fields(self.frame.dataset)[str(offset)]["value"]) != value:
                self.knowledge.set_shared_value(self.frame.dataset, offset, value)

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        if item.column() == 5:
            row = item.row()
            try:
                self.knowledge.set_field(
                    self.frame.dataset,
                    row,
                    self.table.item(row, 5).text(),
                    self.confidence_boxes[row].currentText(),
                    self._current_state_id(),
                )
            except (OSError, ValueError) as exc:
                QMessageBox.critical(self, "Knowledge save failed", str(exc))
                self.refresh()
            return
        if item.column() not in (2, 3):
            return
        try:
            value = int(item.text(), 16 if item.column() == 2 else 10)
            if not 0 <= value <= 0xFF:
                raise ValueError
            if item.row() < 16:
                self.frame.set_data(item.row(), value)
            elif item.row() == 16:
                self.frame.checksum_high = value
            else:
                self.frame.checksum_low = value
        except ValueError:
            QMessageBox.warning(
                self,
                "Invalid byte",
                "Enter a value from 00 to FF in Hex or from 0 to 255 in Decimal.",
            )
        self.refresh()
        self.frame_changed.emit(self.frame.dataset.value, self._current_response())

    def _global_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        offset = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(offset, int):
            return
        if item.column() == 5:
            try:
                self.knowledge.set_field(
                    self.frame.dataset,
                    offset,
                    item.text(),
                    self.global_confidence_boxes[offset].currentText(),
                    self._current_state_id(),
                )
            except (OSError, ValueError) as exc:
                QMessageBox.critical(self, "Knowledge save failed", str(exc))
                self.refresh()
            return
        if item.column() not in (2, 3):
            return
        try:
            value = int(item.text(), 16 if item.column() == 2 else 10)
            if not 0 <= value <= 0xFF:
                raise ValueError
            self.knowledge.set_shared_value(self.frame.dataset, offset, value)
            self.response_set.set_global(offset, value)
        except (KeyError, OSError, ValueError):
            QMessageBox.warning(
                self,
                "Invalid shared byte",
                "Enter a value from 00 to FF in Hex or from 0 to 255 in Decimal.",
            )
        self._selected_global_offset = offset
        self.refresh()
        self._response_set_modified()

    def _global_toggled(self, offset: int, checked: bool) -> None:
        if self._updating or checked == self.response_set.is_global(offset):
            return
        try:
            if checked:
                value = self.frame.data[offset]
                self.knowledge.make_shared(
                    self.frame.dataset, offset, value, self._current_state_id()
                )
                self.response_set.set_global(offset, value)
                self._selected_global_offset = offset
            else:
                self.knowledge.make_state_specific(self.frame.dataset, offset)
                self.response_set.remove_global(offset)
                self._selected_global_offset = None
        except (KeyError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Shared Data save failed", str(exc))
        self.refresh()
        if not checked:
            self.table.selectRow(offset)
        self._response_set_modified()

    def _global_category_changed(self, offset: int) -> None:
        if self._updating:
            return
        try:
            self.knowledge.set_category(
                self.frame.dataset,
                offset,
                self.global_category_boxes[offset].currentText(),
                self._current_state_id(),
            )
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self.refresh()
            return
        self._selected_global_offset = offset
        self.refresh()

    def _global_confidence_changed(self, offset: int) -> None:
        if self._updating:
            return
        try:
            description = self.knowledge.field(
                self.frame.dataset, offset, self._current_state_id()
            )
            self.knowledge.set_field(
                self.frame.dataset,
                offset,
                description["meaning"],
                self.global_confidence_boxes[offset].currentText(),
                self._current_state_id(),
            )
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self.refresh()

    def _refresh_state_controls(self) -> None:
        current = next(
            (index for index, state in enumerate(self.response_set.payload_states) if state.frame is self.frame),
            0,
        )
        self.state_selector.blockSignals(True)
        self.state_selector.clear()
        for state in self.response_set.payload_states:
            self.state_selector.addItem(state.name or state.state_id, state.state_id)
        self.state_selector.setCurrentIndex(current)
        self.state_selector.blockSignals(False)
        self.state_enabled.blockSignals(True)
        self.state_enabled.setChecked(self.response_set.payload_states[current].enabled)
        self.state_enabled.blockSignals(False)

    def _state_selected(self, index: int) -> None:
        if not 0 <= index < len(self.response_set.payload_states):
            return
        state = self.response_set.payload_states[index]
        self.frame = state.frame
        self.response_set.selected_state = state.state_id
        self._refresh_state_controls()
        self.refresh()
        self._response_set_modified()

    def _unused_state_id(self) -> str:
        used = {state.state_id for state in self.response_set.payload_states}
        index = 1
        while f"observed_state_{index}" in used:
            index += 1
        return f"observed_state_{index}"

    def _add_state(self) -> None:
        state_id = self._unused_state_id()
        data = bytearray(16)
        high, low = sum16_bytes(data)
        frame = ResponseFrame(self.frame.dataset, data, high, low)
        self.response_set.payload_states.append(
            ResponsePayloadState(frame, state_id, "New payload state — discriminator unknown")
        )
        self.frame = frame
        self.response_set.selected_state = state_id
        self._refresh_state_controls()
        self.refresh()
        self._response_set_modified()

    def _duplicate_state(self) -> None:
        state_id = self._unused_state_id()
        frame = ResponseFrame.from_hex(self.frame.dataset, self.frame.to_hex())
        frame.auto_checksum = self.frame.auto_checksum
        self.response_set.payload_states.append(
            ResponsePayloadState(frame, state_id, "Copied payload state — discriminator unknown")
        )
        self.frame = frame
        self.response_set.selected_state = state_id
        self._refresh_state_controls()
        self.refresh()
        self._response_set_modified()

    def _delete_state(self) -> None:
        if len(self.response_set.payload_states) <= 1:
            QMessageBox.warning(self, "Delete state", "At least one payload state is required.")
            return
        index = self.state_selector.currentIndex()
        del self.response_set.payload_states[index]
        state = self.response_set.payload_states[max(0, index - 1)]
        self.frame = state.frame
        self.response_set.selected_state = state.state_id
        self._refresh_state_controls()
        self.refresh()
        self._response_set_modified()

    def _state_enabled_changed(self, enabled: bool) -> None:
        index = self.state_selector.currentIndex()
        if 0 <= index < len(self.response_set.payload_states):
            if not enabled and len(self.response_set.enabled_states()) <= 1:
                self.state_enabled.blockSignals(True)
                self.state_enabled.setChecked(True)
                self.state_enabled.blockSignals(False)
                QMessageBox.warning(
                    self,
                    "Disable state",
                    "At least one payload state must remain enabled.",
                )
                return
            self.response_set.payload_states[index].enabled = enabled
            self._response_set_modified()

    def _response_set_modified(self) -> None:
        self.response_set.selected_state = self._current_state_id()
        self.response_set._normalize_shared_backing_values()
        self._refresh_state_controls()
        self.response_set_changed.emit(self.frame.dataset.value, self.response_set)

    def _save_field_confidence(self, row: int) -> None:
        if self._updating or self.table.item(row, 5) is None:
            return
        try:
            self.knowledge.set_field(
                self.frame.dataset,
                row,
                self.table.item(row, 5).text(),
                self.confidence_boxes[row].currentText(),
                self._current_state_id(),
            )
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self.refresh()

    def _category_changed(self, row: int) -> None:
        if self._updating:
            return
        try:
            self.knowledge.set_category(
                self.frame.dataset, row, self.category_boxes[row].currentText(), self._current_state_id()
            )
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self.refresh()
            return
        if self.table.currentRow() == row:
            self._selection_changed()

    def _auto_checksum_changed(self, checked: bool) -> None:
        self.frame.auto_checksum = checked
        if checked:
            self.frame.recalculate()
        self.refresh()
        self.frame_changed.emit(self.frame.dataset.value, self._current_response())

    def _selection_changed(self) -> None:
        if self._updating or not self.table.selectedItems():
            return
        self._selected_global_offset = None
        self.global_table.blockSignals(True)
        self.global_table.clearSelection()
        self.global_table.blockSignals(False)
        self._update_bit_editor()

    def _global_selection_changed(self) -> None:
        if self._updating or not self.global_table.selectedItems():
            return
        row = self.global_table.currentRow()
        item = self.global_table.item(row, 0)
        if item is None:
            return
        self._selected_global_offset = int(item.data(Qt.ItemDataRole.UserRole))
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.blockSignals(False)
        self._update_bit_editor()

    def _selected_data_offset(self) -> int | None:
        if (
            self._selected_global_offset is not None
            and self.response_set.is_global(self._selected_global_offset)
        ):
            return self._selected_global_offset
        row = self.table.currentRow()
        return row if 0 <= row < 16 and not self.table.isRowHidden(row) else None

    def _update_bit_editor(self) -> None:
        offset = self._selected_data_offset()
        # Bit annotations are useful while the byte category is still UNKNOWN
        # or may later turn out not to be a pure bitfield.  Therefore every
        # payload byte exposes its eight bits; BITFIELD remains a semantic
        # category, not a prerequisite for reverse-engineering notes.
        enabled = offset is not None
        live = self._live_active()
        is_bitfield = offset is not None and self.knowledge.is_bitfield(
            self.frame.dataset, offset, self._current_state_id()
        )
        if offset is not None:
            category = self.knowledge.field(
                self.frame.dataset, offset, self._current_state_id()
            )["category"]
            scope = (
                "Shared value and shared meaning"
                if self.response_set.is_global(offset)
                else f"Shared meaning for {self.frame.dataset.value}"
            )
            self.bit_group.setTitle(
                f"Bit editor — Data[{offset}] / {category} / {scope}"
                + (" / LIVE values (read-only)" if live else "")
            )
        else:
            self.bit_group.setTitle("Bit editor — select a Data row")
        for index, bit in enumerate(range(7, -1, -1)):
            button = self.bit_buttons[index]
            meaning_edit = self.bit_meanings[index]
            confidence_edit = self.bit_confidences[index]
            button.setEnabled(enabled and not live)
            meaning_edit.setEnabled(enabled)
            confidence_edit.setEnabled(enabled)
            if enabled:
                byte_value = (
                    self._live_response[offset]
                    if live
                    else self.response_set.shared_data[offset]
                    if self.response_set.is_global(offset)
                    else self.frame.data[offset]
                )
                value = bool(byte_value & (1 << bit))
                description = (
                    self.knowledge.bit(
                        self.frame.dataset, offset, bit, self._current_state_id()
                    )
                    if is_bitfield
                    else {"meaning": "", "confidence": "UNKNOWN"}
                )
                button.blockSignals(True)
                button.setChecked(value)
                button.setText(f"bit{bit}\n{int(value)}")
                button.blockSignals(False)
                meaning_edit.blockSignals(True)
                meaning_edit.setText(description["meaning"])
                meaning_edit.blockSignals(False)
                confidence_edit.blockSignals(True)
                confidence_edit.setCurrentText(description["confidence"])
                confidence_edit.blockSignals(False)
            else:
                button.setText(f"bit{bit}\n-")
                meaning_edit.blockSignals(True)
                meaning_edit.clear()
                meaning_edit.blockSignals(False)
                confidence_edit.blockSignals(True)
                confidence_edit.setCurrentText("UNKNOWN")
                confidence_edit.blockSignals(False)

    def _bit_clicked(self, bit: int, checked: bool) -> None:
        if self._live_active():
            self._update_bit_editor()
            return
        offset = self._selected_data_offset()
        if offset is None:
            return
        if not self.knowledge.is_bitfield(
            self.frame.dataset, offset, self._current_state_id()
        ):
            try:
                self.knowledge.set_category(
                    self.frame.dataset, offset, "BITFIELD", self._current_state_id()
                )
            except (OSError, ValueError) as exc:
                QMessageBox.critical(self, "Knowledge save failed", str(exc))
                self._update_bit_editor()
                return
        value = (
            self.response_set.shared_data[offset]
            if self.response_set.is_global(offset)
            else self.frame.data[offset]
        )
        value = value | (1 << bit) if checked else value & ~(1 << bit)
        if self.response_set.is_global(offset):
            try:
                self.knowledge.set_shared_value(self.frame.dataset, offset, value)
            except (KeyError, OSError, ValueError) as exc:
                QMessageBox.critical(self, "Shared Data save failed", str(exc))
                return
            self.response_set.set_global(offset, value)
        else:
            self.frame.set_data(offset, value)
        self.refresh()
        self.frame_changed.emit(self.frame.dataset.value, self._current_response())

    def _save_bit_description(self, bit: int) -> None:
        if self._updating:
            return
        offset = self._selected_data_offset()
        if offset is None:
            return
        index = 7 - bit
        meaning = self.bit_meanings[index].text().strip()
        confidence = self.bit_confidences[index].currentText().strip()
        if (
            not self.knowledge.is_bitfield(
                self.frame.dataset, offset, self._current_state_id()
            )
            and meaning.upper() in ("", "UNKNOWN")
            and confidence.upper() in ("", "UNKNOWN")
        ):
            return
        try:
            if not self.knowledge.is_bitfield(
                self.frame.dataset, offset, self._current_state_id()
            ):
                self.knowledge.set_category(
                    self.frame.dataset, offset, "BITFIELD", self._current_state_id()
                )
            self.knowledge.set_bit(
                self.frame.dataset,
                offset,
                bit,
                meaning,
                confidence,
                self._current_state_id(),
            )
            category = self.category_boxes[offset]
            category.blockSignals(True)
            category.setCurrentText("BITFIELD")
            category.blockSignals(False)
            global_category = self.global_category_boxes.get(offset)
            if global_category is not None:
                global_category.blockSignals(True)
                global_category.setCurrentText("BITFIELD")
                global_category.blockSignals(False)
            self._update_bit_editor()
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self._update_bit_editor()

class SingleBitTest(QWidget):
    apply_requested = Signal(str, int, int)

    def __init__(
        self,
        frames: dict[Dataset, ResponseFrame],
        knowledge: ProtocolKnowledge,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.frames = frames
        self.knowledge = knowledge
        self.dataset = QComboBox()
        self.dataset.addItems([dataset.value for dataset in Dataset])
        self.offset = QSpinBox()
        self.offset.setRange(0, 15)
        self.bit = QSpinBox()
        self.bit.setRange(0, 7)
        self.preview = QLabel()
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.apply = QPushButton("Apply Test Value")
        self.apply.setStyleSheet("font-weight: bold; padding: 8px")
        self.apply.clicked.connect(self._apply)
        for widget in (self.dataset, self.offset, self.bit):
            if isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self.refresh)
            else:
                widget.valueChanged.connect(self.refresh)
        form = QFormLayout()
        form.addRow("Dataset", self.dataset)
        form.addRow("Byte offset", self.offset)
        form.addRow("Bit to toggle", self.bit)
        box = QGroupBox("Manual single-bit test (no scanning or brute force)")
        box_layout = QVBoxLayout(box)
        box_layout.addLayout(form)
        box_layout.addWidget(self.preview)
        box_layout.addWidget(self.apply)
        layout = QVBoxLayout(self)
        layout.addWidget(box)
        layout.addStretch()
        self.refresh()

    def selection(self) -> tuple[Dataset, int, int]:
        return Dataset(self.dataset.currentText()), self.offset.value(), self.bit.value()

    def refresh(self, ignored=None) -> None:
        dataset, offset, bit = self.selection()
        frame = self.frames[dataset]
        original = frame.data[offset]
        modified = original ^ (1 << bit)
        modified_data = bytearray(frame.data)
        modified_data[offset] = modified
        description = (
            self.knowledge.bit(dataset, offset, bit)
            if self.knowledge.is_bitfield(dataset, offset)
            else {"meaning": "Not a BITFIELD", "confidence": "UNKNOWN"}
        )
        old_checksum = (frame.checksum_high << 8) | frame.checksum_low
        self.preview.setText(
            f"Original: {original:02X} / {original:08b}\n"
            f"Modified: {modified:02X} / {modified:08b}\n"
            f"Old SUM16: {old_checksum:04X}\n"
            f"New SUM16: {calculate_sum16(modified_data):04X}\n"
            f"Meaning: {description['meaning']} [{description['confidence']}]"
        )

    def _apply(self) -> None:
        dataset, offset, bit = self.selection()
        self.apply_requested.emit(dataset.value, offset, bit)
        self.refresh()
