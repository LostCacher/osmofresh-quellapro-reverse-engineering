"""Payload-class-aware AA frame editor used by the Display Emulator role."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protocol.checksum import sum16_bytes
from protocol.knowledge import CONFIDENCE_LEVELS, FIELD_CATEGORIES, ProtocolKnowledge
from protocol.framing import frame_byte_metadata


class ByteEditTable(QTableWidget):
    """Start numeric cell editing with the first regular or keypad digit."""

    def keyPressEvent(self, event) -> None:
        item = self.currentItem()
        text = event.text().upper()
        column = self.currentColumn()
        valid_first_key = (
            column == 2 and text in "0123456789ABCDEF"
        ) or (column == 3 and text in "0123456789")
        if (
            item is not None
            and valid_first_key
            and item.flags() & Qt.ItemFlag.ItemIsEditable
        ):
            self.editItem(item)
            editor = QApplication.focusWidget()
            if isinstance(editor, QLineEdit):
                editor.selectAll()
                editor.insert(text)
                event.accept()
                return
        super().keyPressEvent(event)


class AAFrameEditor(QWidget):
    payload_changed = Signal(str, bytes)
    preset_changed = Signal(str, bytes, bool, int, int)
    send_once_requested = Signal(str)
    apply_auto_requested = Signal(str, bytes)

    def __init__(
        self,
        class_id: str,
        payload: bytes,
        knowledge: ProtocolKnowledge,
        auto_checksum: bool = True,
        checksum_high: int | None = None,
        checksum_low: int | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.class_id = class_id
        self.knowledge = knowledge
        self.payload = bytearray(payload)
        computed_high, computed_low = sum16_bytes(self.payload)
        self.checksum_high = computed_high if checksum_high is None else checksum_high
        self.checksum_low = computed_low if checksum_low is None else checksum_low
        self.auto_checksum_enabled = auto_checksum
        self._updating = False
        self.category_boxes: list[QComboBox] = []
        self.confidence_boxes: list[QComboBox] = []
        self._build_ui()
        self.refresh()
        self.table.selectRow(0)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        description = self.knowledge.page("AA", self.class_id)
        info = QLabel(
            f"AA + Payload[0..15] + SUM16 High + SUM16 Low — "
            f"class: {description['name']}, discriminator "
            f"Data[{description['discriminator']['offset']}]="
            f"{description['discriminator']['value']}"
        )
        info.setStyleSheet("font-weight:bold")
        layout.addWidget(info)

        checksum_controls = QHBoxLayout()
        self.auto_checksum = QCheckBox("Auto checksum")
        self.auto_checksum.setToolTip(
            "Disable this to edit SUM16_H and SUM16_L and deliberately transmit an invalid AA frame."
        )
        self.auto_checksum.setChecked(self.auto_checksum_enabled)
        self.auto_checksum.toggled.connect(self._auto_checksum_toggled)
        self.checksum_status = QLabel()
        checksum_controls.addWidget(self.auto_checksum)
        checksum_controls.addWidget(self.checksum_status)
        checksum_controls.addStretch()
        self.send_once_button = QPushButton("Send this AA once")
        self.send_once_button.setToolTip(
            "Transmit this AA frame exactly once; AA polling does not need to be enabled."
        )
        self.send_once_button.setEnabled(False)
        self.send_once_button.clicked.connect(
            lambda: self.send_once_requested.emit(self.class_id)
        )
        self.apply_auto_button = QPushButton("Apply frame to Auto state")
        self.apply_auto_button.setToolTip(
            "Explicitly copy this payload into the automatic Display state."
        )
        self.apply_auto_button.clicked.connect(
            lambda: self.apply_auto_requested.emit(
                self.class_id, bytes(self.payload)
            )
        )
        checksum_controls.addWidget(self.apply_auto_button)
        checksum_controls.addWidget(self.send_once_button)
        layout.addLayout(checksum_controls)

        self.raw_preview = QLabel()
        self.raw_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.raw_preview.setStyleSheet("font-family:monospace")
        layout.addWidget(self.raw_preview)

        self.table = ByteEditTable(18, 7)
        self.table.setHorizontalHeaderLabels(
            ["Offset", "Category", "Hex", "Decimal", "Binary", "Known meaning", "Confidence"]
        )
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._update_bit_editor)
        self.table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch
        )
        for row in range(18):
            confidence = QComboBox()
            confidence.addItems(CONFIDENCE_LEVELS)
            confidence.currentTextChanged.connect(
                lambda ignored, selected=row: self._confidence_changed(selected)
            )
            self.table.setCellWidget(row, 6, confidence)
            self.confidence_boxes.append(confidence)
            if row < 16:
                category = QComboBox()
                category.addItems(FIELD_CATEGORIES)
                category.currentTextChanged.connect(
                    lambda ignored, selected=row: self._category_changed(selected)
                )
                self.table.setCellWidget(row, 1, category)
                self.category_boxes.append(category)
            else:
                self.table.setCellWidget(row, 1, QLabel("PROTOCOL"))
        layout.addWidget(self.table, 1)

        self.save_status = QLabel(
            "Double-click Known meaning or select Category / Confidence to edit."
        )
        self.save_status.setStyleSheet("color:#aeb8c4")
        layout.addWidget(self.save_status)

        self.bit_group = QGroupBox("Bit editor — select a BITFIELD row")
        bit_layout = QGridLayout(self.bit_group)
        self.bit_buttons: list[QPushButton] = []
        self.bit_meanings: list[QLineEdit] = []
        self.bit_confidences: list[QComboBox] = []
        for column, bit in enumerate(range(7, -1, -1)):
            button = QPushButton(f"bit{bit}\n-")
            button.setCheckable(True)
            button.clicked.connect(
                lambda checked, selected=bit: self._bit_clicked(selected, checked)
            )
            meaning = QLineEdit()
            meaning.setPlaceholderText("UNKNOWN")
            meaning.editingFinished.connect(
                lambda selected=bit: self._save_bit(selected)
            )
            confidence = QComboBox()
            confidence.addItems(CONFIDENCE_LEVELS)
            confidence.currentTextChanged.connect(
                lambda ignored, selected=bit: self._save_bit(selected)
            )
            bit_layout.addWidget(button, 0, column)
            bit_layout.addWidget(meaning, 1, column)
            bit_layout.addWidget(confidence, 2, column)
            self.bit_buttons.append(button)
            self.bit_meanings.append(meaning)
            self.bit_confidences.append(confidence)
        layout.addWidget(self.bit_group)

    def set_send_enabled(self, enabled: bool) -> None:
        self.send_once_button.setEnabled(enabled)

    def set_frame_config(
        self,
        payload: bytes,
        auto_checksum: bool,
        checksum_high: int,
        checksum_low: int,
    ) -> None:
        if len(payload) != 16:
            raise ValueError("AA payload must contain exactly 16 bytes")
        if (
            bytes(self.payload) == bytes(payload)
            and self.auto_checksum_enabled == auto_checksum
            and self.checksum_high == checksum_high
            and self.checksum_low == checksum_low
        ):
            return
        self.payload[:] = payload
        self.auto_checksum_enabled = auto_checksum
        self.checksum_high = checksum_high
        self.checksum_low = checksum_low
        self._updating = True
        self.auto_checksum.setChecked(auto_checksum)
        self._updating = False
        self.refresh()

    def set_payload(self, payload: bytes) -> None:
        self.set_frame_config(
            payload,
            self.auto_checksum_enabled,
            self.checksum_high,
            self.checksum_low,
        )

    def frame(self) -> bytes:
        if self.auto_checksum_enabled:
            high, low = sum16_bytes(self.payload)
        else:
            high, low = self.checksum_high, self.checksum_low
        return b"\xAA" + bytes(self.payload) + bytes((high, low))

    def refresh(self) -> None:
        self._updating = True
        raw_values = self.frame()[1:]
        for row, value in enumerate(raw_values):
            offset = frame_byte_metadata("AA", 19, row + 1).logical_offset
            field = self.knowledge.field("AA", row, self.class_id)
            values = (offset, f"{value:02X}", str(value), f"{value:08b}", field["meaning"])
            for column, text in zip((0, 2, 3, 4, 5), values):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                item.setText(text)
                editable = column == 5 or (
                    column in (2, 3)
                    and (row < 16 or not self.auto_checksum_enabled)
                )
                item.setFlags(
                    item.flags() | Qt.ItemFlag.ItemIsEditable
                    if editable
                    else item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
            self.confidence_boxes[row].setCurrentText(field["confidence"])
            if row < 16:
                self.category_boxes[row].setCurrentText(field["category"])
        self.raw_preview.setText(self.frame().hex(" ").upper())
        expected = sum16_bytes(self.payload)
        actual = tuple(self.frame()[-2:])
        if actual == expected:
            self.checksum_status.setText(
                f"SUM16 valid: {actual[0]:02X} {actual[1]:02X}"
            )
            self.checksum_status.setStyleSheet("color:#73d795;font-weight:bold")
        else:
            self.checksum_status.setText(
                f"INTENTIONALLY INVALID: {actual[0]:02X} {actual[1]:02X} "
                f"(calculated {expected[0]:02X} {expected[1]:02X})"
            )
            self.checksum_status.setStyleSheet("color:#ef6c6c;font-weight:bold")
        self._updating = False
        self._update_bit_editor()

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        row = item.row()
        try:
            if item.column() == 5:
                self.knowledge.set_field(
                    "AA", row, item.text(), self.confidence_boxes[row].currentText(), self.class_id
                )
                self._saved(f"Known meaning saved for Data[{row}]")
                return
            if item.column() not in (2, 3):
                return
            value = int(item.text(), 16 if item.column() == 2 else 10)
            if not 0 <= value <= 0xFF:
                raise ValueError
            if row < 16:
                self.payload[row] = value
            elif not self.auto_checksum_enabled:
                if row == 16:
                    self.checksum_high = value
                else:
                    self.checksum_low = value
            self._emit_preset_changed()
        except ValueError:
            QMessageBox.warning(self, "Invalid AA value", "Enter HEX 00..FF or decimal 0..255.")
        except (KeyError, OSError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
        self.refresh()

    def _auto_checksum_toggled(self, enabled: bool) -> None:
        if self._updating:
            return
        if not enabled:
            self.checksum_high, self.checksum_low = sum16_bytes(self.payload)
        self.auto_checksum_enabled = enabled
        self._emit_preset_changed()
        self.refresh()

    def _emit_preset_changed(self) -> None:
        payload = bytes(self.payload)
        self.payload_changed.emit(self.class_id, payload)
        self.preset_changed.emit(
            self.class_id,
            payload,
            self.auto_checksum_enabled,
            self.checksum_high,
            self.checksum_low,
        )

    def _category_changed(self, row: int) -> None:
        if self._updating:
            return
        try:
            self.knowledge.set_category(
                "AA", row, self.category_boxes[row].currentText(), self.class_id
            )
        except (KeyError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self.refresh()
            return
        self._saved(f"Category saved for Data[{row}]")
        self.refresh()

    def _confidence_changed(self, row: int) -> None:
        if self._updating:
            return
        try:
            field = self.knowledge.field("AA", row, self.class_id)
            self.knowledge.set_field(
                "AA", row, field["meaning"], self.confidence_boxes[row].currentText(), self.class_id
            )
        except (KeyError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self.refresh()
            return
        self._saved(f"Confidence saved for Data[{row}]")

    def _selected_offset(self) -> int | None:
        row = self.table.currentRow()
        return row if 0 <= row < 16 else None

    def _update_bit_editor(self) -> None:
        offset = self._selected_offset()
        enabled = offset is not None and self.knowledge.is_bitfield("AA", offset, self.class_id)
        self._updating = True
        for index, bit in enumerate(range(7, -1, -1)):
            button = self.bit_buttons[index]
            meaning = self.bit_meanings[index]
            confidence = self.bit_confidences[index]
            button.setEnabled(enabled)
            meaning.setEnabled(enabled)
            confidence.setEnabled(enabled)
            if enabled:
                value = bool(self.payload[offset] & (1 << bit))
                field = self.knowledge.bit("AA", offset, bit, self.class_id)
                button.setChecked(value)
                button.setText(f"bit{bit}\n{int(value)}")
                meaning.setText(field["meaning"])
                confidence.setCurrentText(field["confidence"])
            else:
                button.setText(f"bit{bit}\n-")
                meaning.clear()
                confidence.setCurrentText("UNKNOWN")
        self._updating = False

    def _bit_clicked(self, bit: int, checked: bool) -> None:
        offset = self._selected_offset()
        if offset is None:
            return
        self.payload[offset] = (
            self.payload[offset] | (1 << bit)
            if checked
            else self.payload[offset] & ~(1 << bit)
        )
        self._emit_preset_changed()
        self.refresh()

    def _save_bit(self, bit: int) -> None:
        if self._updating:
            return
        offset = self._selected_offset()
        if offset is None or not self.knowledge.is_bitfield("AA", offset, self.class_id):
            return
        index = 7 - bit
        try:
            self.knowledge.set_bit(
                "AA",
                offset,
                bit,
                self.bit_meanings[index].text(),
                self.bit_confidences[index].currentText(),
                self.class_id,
            )
        except (KeyError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Knowledge save failed", str(exc))
            self._update_bit_editor()
            return
        self._saved(f"bit{bit} knowledge saved for Data[{offset}]")

    def _saved(self, message: str) -> None:
        self.save_status.setText(message)
        self.save_status.setStyleSheet("color:#73d795;font-weight:bold")
