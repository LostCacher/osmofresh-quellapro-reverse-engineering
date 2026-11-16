"""Main window for the UART Protocol Workbench."""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QDoubleSpinBox,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from protocol.frames import (
    Dataset,
    RequestResponseSet,
    ResponseFrame,
    frame_payload_class,
    load_default_response_sets,
    parse_hex_bytes,
)
from protocol.framing import frame_byte_metadata
from protocol.datasets import DatasetTracker
from protocol.checksum import sum16_bytes
from protocol.display_role import (
    AAPayloadPreset,
    DisplayRequest,
    DisplayRoleConfig,
    EmulatorRole,
    default_aa_presets,
    direction_labels,
)
from protocol.knowledge import ProtocolKnowledge
from protocol.live_state import QuellaLiveState
from protocol.mega_bridge import BridgeDirection, MegaConfig, MegaMessageType, MegaRule
from protocol.replay import ReplayMode, ReplayProfile
from .frame_editor import FrameEditor, SingleBitTest
from .aa_frame_editor import AAFrameEditor
from .display_emulator_tab import DisplayEmulatorTab
from .live_analysis_tab import LiveAnalysisTab
from .live_status_tab import LiveStatusTab
from .mega_bridge_tab import MegaBridgeTab
from .network_capture_worker import NetworkCaptureWorker
from .udp_telemetry_worker import UdpTelemetryWorker
from .homeserver_state_worker import HomeServerStateWorker
from .replay_tab import ReplayTab
from .serial_worker import SerialWorker


def timestamp_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")[:-3]


def preferred_serial_port(ports: list[str]) -> str | None:
    """Return the first USB serial device, independent of its full path."""
    return next(
        (port for port in ports if port.rsplit("/", 1)[-1].lower().startswith("ttyusb")),
        None,
    )


class DetachedTabWindow(QMainWindow):
    """Top-level host that returns its live tab widget when closed."""

    dock_requested = Signal(object)

    def __init__(self, content: QWidget, title: str) -> None:
        super().__init__()
        self._dock_on_close = True
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(f"{title} — UART Protocol Workbench")
        self.setCentralWidget(content)
        # QTabWidget keeps inactive pages explicitly hidden. Reparenting such a
        # page does not clear that hidden state automatically.
        content.show()
        toolbar = QToolBar("Window controls", self)
        toolbar.setMovable(False)
        dock_button = QPushButton("Dock back into main window")
        dock_button.setToolTip("Return this view to its original tab")
        dock_button.clicked.connect(self.close)
        toolbar.addWidget(dock_button)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

    def close_without_docking(self) -> None:
        self._dock_on_close = False
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._dock_on_close:
            content = self.takeCentralWidget()
            if content is not None:
                self.dock_requested.emit(content)
        event.accept()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("UART PROTOCOL WORKBENCH — bidirectional UART reverse engineering")
        self.resize(1420, 920)
        self.worker: SerialWorker | NetworkCaptureWorker | UdpTelemetryWorker | HomeServerStateWorker | None = None
        self.emulator_role = EmulatorRole.MAIN_MCU
        self.knowledge = ProtocolKnowledge()
        self.live_state = QuellaLiveState()
        self.dataset_tracker = DatasetTracker()
        self.response_sets = load_default_response_sets()
        self.frames = {
            dataset: response_set.selected_payload_state().frame
            for dataset, response_set in self.response_sets.items()
        }
        self.frame_editors: dict[Dataset, FrameEditor] = {}
        self.log_entries: list[dict] = []
        self.history_entries: list[tuple[str, str, str | None, bytes, str]] = []
        self._detached_windows: dict[QWidget, DetachedTabWindow] = {}
        self._closing = False
        self._build_ui()
        self.refresh_ports()
        self._set_status("DISCONNECTED")

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.addWidget(self._role_panel())
        self.safety_banner = QLabel(
            "DISPLAY-SIDE EMULATOR — ONLY FOR A DISPLAY PCB COMPLETELY DISCONNECTED FROM THE REAL MAIN PCB"
        )
        self.safety_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.safety_banner.setStyleSheet("background:#842626;color:white;font-weight:bold;padding:9px")
        outer.addWidget(self.safety_banner)
        outer.addWidget(self._serial_panel())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._live_tab(), "Live UART")
        self.live_analysis_tab = LiveAnalysisTab(self.knowledge)
        self.tabs.addTab(self.live_analysis_tab, "Live Changes")
        self.live_status_tab = LiveStatusTab(self.live_state)
        self.tabs.addTab(self.live_status_tab, "Live Status")
        self.display_emulator_tab = DisplayEmulatorTab(
            self.knowledge.path.with_name("aa_command_presets.json")
        )
        self.display_emulator_tab.config_changed.connect(self._display_config_changed)
        self.display_emulator_tab.start_requested.connect(self._start_display_polling)
        self.display_emulator_tab.stop_requested.connect(self._stop_display_polling)
        self.display_emulator_tab.step_requested.connect(self._step_display_request)
        self.display_emulator_tab.send_requested.connect(self._send_display_request)
        self.display_emulator_tab.send_aa_preset_requested.connect(
            self._send_display_aa_preset
        )
        self.display_emulator_tab.abort_requested.connect(
            self._abort_water_process
        )
        self.display_tab_index = self.tabs.addTab(
            self.display_emulator_tab, "Display Emulator"
        )
        self.tabs.addTab(self._raw_tab(), "Raw RX monitor")
        self.frame_editor_tabs = self._frames_tab()
        self.tabs.addTab(self.frame_editor_tabs, "Frame editor")
        self.replay_tab = ReplayTab(self.frames)
        self.replay_tab.profile_changed.connect(self._replay_profile_changed)
        self.replay_tab.start_requested.connect(self._start_replay)
        self.replay_tab.reset_requested.connect(self._reset_replay)
        self.replay_tab.pause_requested.connect(self._pause_replay)
        self.replay_tab.next_requested.connect(self._next_replay_step)
        self.replay_tab.static_requested.connect(self._switch_replay_static)
        self.tabs.addTab(self.replay_tab, "Startup / Replay")
        self.bit_test = SingleBitTest(self.frames, self.knowledge)
        self.bit_test.apply_requested.connect(self._apply_bit_test)
        self.tabs.addTab(self.bit_test, "Single-bit test")
        self.tabs.addTab(self._notes_tab(), "Experiment notes")
        self.tabs.addTab(self._history_tab(), "Frame history / diff")
        self.tabs.addTab(self._dataset_inspector_tab(), "Dataset Inspector")
        self.tabs.addTab(self._statistics_tab(), "Statistics")
        self.mega_bridge_tab = MegaBridgeTab(self.knowledge)
        self.mega_bridge_tab.config_requested.connect(self._mega_config_requested)
        self.mega_bridge_tab.inject_requested.connect(self._mega_inject_requested)
        self.mega_bridge_tab.set_rule_requested.connect(self._mega_set_rule_requested)
        self.mega_bridge_tab.rule_command_requested.connect(self._mega_rule_command_requested)
        self.mega_bridge_tab.manual_dispense_requested.connect(self._mega_manual_dispense_requested)
        self.mega_bridge_tab.stop_remote_dispense_requested.connect(self._mega_stop_remote_dispense_requested)
        self.mega_bridge_tab.tank_config_requested.connect(self._mega_tank_config_requested)
        self.mega_bridge_tab.tds_test_requested.connect(self._mega_tds_test_requested)
        self.mega_bridge_tab.tds_test_disable_requested.connect(self._mega_tds_test_disable_requested)
        self.tabs.addTab(self.mega_bridge_tab, "Mega Bridge")
        self._tab_order = [
            (self.tabs.widget(index), self.tabs.tabText(index))
            for index in range(self.tabs.count())
        ]
        self._detachable_tabs = {
            self.live_analysis_tab,
            self.frame_editor_tabs,
        }
        self.detach_tab_button = QPushButton("Detach tab ↗")
        self.detach_tab_button.setToolTip(
            "Open Live Changes or Frame editor in a separate movable window"
        )
        self.detach_tab_button.clicked.connect(self._detach_current_tab)
        self.tabs.setCornerWidget(
            self.detach_tab_button, Qt.Corner.TopRightCorner
        )
        self.tabs.currentChanged.connect(self._update_detach_button)
        self.tabs.tabBarDoubleClicked.connect(self._detach_tab)
        self._update_detach_button()
        outer.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._apply_role_ui()

    def _role_panel(self) -> QGroupBox:
        box = QGroupBox("Emulation role")
        layout = QHBoxLayout(box)
        self.role_selector = QComboBox()
        self.role_selector.addItem("Main MCU Emulator", EmulatorRole.MAIN_MCU.value)
        self.role_selector.addItem("Display Emulator", EmulatorRole.DISPLAY.value)
        self.role_selector.setMinimumWidth(240)
        self.role_selector.setStyleSheet("font-weight:bold;padding:5px")
        self.role_explanation = QLabel()
        self.role_explanation.setWordWrap(True)
        layout.addWidget(QLabel("Active role:"))
        layout.addWidget(self.role_selector)
        layout.addWidget(self.role_explanation, 1)
        self.role_selector.currentIndexChanged.connect(self._role_changed)
        return box

    def _role_changed(self, ignored: int | None = None) -> None:
        self.emulator_role = EmulatorRole(self.role_selector.currentData())
        self._apply_role_ui()

    def _apply_role_ui(self) -> None:
        display_role = self.emulator_role is EmulatorRole.DISPLAY
        mega_transport = (
            hasattr(self, "transport_mode")
            and self.transport_mode.currentData() == "mega_bridge"
        )
        network_transport = self._is_network_transport()
        if mega_transport:
            self.safety_banner.setText(
                "ATMEGA2560 MITM BRIDGE — DEFAULT IS TRANSPARENT PASS; VERIFY COMMON GROUND AND TX/RX WIRING"
            )
        if network_transport:
            self.safety_banner.setText(
                "PASSIVE NETWORK ANALYSIS — ESP8266 INPUT; ALL TRANSMIT CONTROLS ARE DISABLED"
            )
        if display_role:
            self.safety_banner.setText(
                "DISPLAY EMULATOR — CONNECT ONLY TO A VERIFIED MAIN MCU UART; POLLING AND AA START OFF"
            )
            self.role_explanation.setText(
                "Workbench sends display requests to the real Main MCU and validates its replies."
            )
            self.display_emulator_tab.force_safe_idle()
        else:
            self.safety_banner.setText(
                "MAIN MCU EMULATOR — ONLY FOR A DISPLAY PCB COMPLETELY DISCONNECTED FROM THE REAL MAIN PCB"
            )
            self.role_explanation.setText(
                "Workbench receives display requests and emulates the Main MCU responses."
            )
        if mega_transport:
            self.safety_banner.setText(
                "ATMEGA2560 MITM BRIDGE — DEFAULT IS TRANSPARENT PASS; VERIFY COMMON GROUND AND TX/RX WIRING"
            )
        if network_transport:
            self.safety_banner.setText(
                "PASSIVE NETWORK ANALYSIS — ESP8266 INPUT; ALL TRANSMIT CONTROLS ARE DISABLED"
            )
        if hasattr(self, "auto_respond"):
            self.auto_respond.setEnabled(not display_role and not mega_transport and not network_transport)
            self.delay.setEnabled(not display_role and not mega_transport and not network_transport)
            self.capture_button.setText(
                "Passive listen only" if display_role else "Capture startup without responding"
            )
            self.emulation_button.setVisible(not display_role and not mega_transport and not network_transport)
            self.replay_tab.setEnabled(not display_role and not network_transport)
            self.display_emulator_tab.setEnabled(display_role and not network_transport)
            for index in self.main_frame_editor_indices:
                self.frame_editor_tabs.setTabVisible(index, not display_role)
            for index in self.aa_frame_editor_indices:
                self.frame_editor_tabs.setTabVisible(index, display_role)
            self.display_request_buttons.setVisible(display_role)

    def _update_detach_button(self, ignored: int | None = None) -> None:
        widget = self.tabs.currentWidget()
        enabled = widget in self._detachable_tabs
        self.detach_tab_button.setEnabled(enabled)
        self.detach_tab_button.setToolTip(
            "Open this view in a separate movable window"
            if enabled
            else "Only Live Changes and Frame editor can currently be detached"
        )

    def _detach_current_tab(self) -> None:
        self._detach_tab(self.tabs.currentIndex())

    def _detach_tab(self, index: int) -> None:
        if not 0 <= index < self.tabs.count():
            return
        content = self.tabs.widget(index)
        if content not in self._detachable_tabs or content in self._detached_windows:
            return
        title = self.tabs.tabText(index)
        self.tabs.removeTab(index)
        window = DetachedTabWindow(content, title)
        window.setWindowIcon(self.windowIcon())
        window.resize(max(900, int(self.width() * 0.8)), max(650, int(self.height() * 0.8)))
        window.dock_requested.connect(self._reattach_tab)
        window.destroyed.connect(
            lambda ignored=None, tab=content: self._detached_windows.pop(tab, None)
        )
        self._detached_windows[content] = window
        window.show()
        content.show()
        window.raise_()
        window.activateWindow()
        self._update_detach_button()

    def _reattach_tab(self, content: QWidget) -> None:
        if self._closing or self.tabs.indexOf(content) >= 0:
            return
        canonical_index = next(
            index
            for index, (widget, ignored) in enumerate(self._tab_order)
            if widget is content
        )
        title = self._tab_order[canonical_index][1]
        insert_at = sum(
            self.tabs.indexOf(widget) >= 0
            for widget, ignored in self._tab_order[:canonical_index]
        )
        self.tabs.insertTab(insert_at, content, title)
        self.tabs.setCurrentWidget(content)
        content.show()
        self._detached_windows.pop(content, None)
        self._update_detach_button()

    def _serial_panel(self) -> QGroupBox:
        box = QGroupBox("Serial — 8N1, idle HIGH")
        layout = QGridLayout(box)
        self.port = QComboBox()
        self.port.setEditable(True)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_ports)
        self.transport_mode = QComboBox()
        self.transport_mode.addItem("ATmega2560 Bridge", "mega_bridge")
        self.transport_mode.addItem("Direct UART (legacy)", "direct_uart")
        self.transport_mode.addItem("Network UDP telemetry (passive)", "network_udp")
        self.transport_mode.addItem("Home server HTTP state (passive)", "network_homeserver")
        self.transport_mode.addItem("Network WebSocket (passive)", "network_ws")
        self.transport_mode.currentIndexChanged.connect(self._transport_changed)
        self.baud = QComboBox()
        self.baud.setEditable(True)
        self.baud.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.baud.setCurrentText("115200")
        self.quella_baud = QComboBox()
        self.quella_baud.setEditable(True)
        self.quella_baud.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        self.capture_button = QPushButton("Capture startup without responding")
        self.capture_button.clicked.connect(self.start_capture)
        self.emulation_button = QPushButton("Start emulation")
        self.emulation_button.clicked.connect(self.toggle_emulation)
        self.emulation_button.setEnabled(False)
        self.auto_respond = QCheckBox("Automatically respond")
        self.auto_respond.setChecked(True)
        self.auto_respond.toggled.connect(self._auto_changed)
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0, 1000)
        self.delay.setDecimals(3)
        self.delay.setValue(1.0)
        self.delay.setSuffix(" ms")
        self.delay.valueChanged.connect(self._delay_changed)
        self.status = QLabel()
        self.status.setMinimumWidth(180)
        self.profile_save = QPushButton("Save profile")
        self.profile_load = QPushButton("Load profile")
        self.profile_save.clicked.connect(self.save_profile)
        self.profile_load.clicked.connect(self.load_profile)

        layout.addWidget(QLabel("Transport"), 0, 0)
        layout.addWidget(self.transport_mode, 0, 1)
        self.endpoint_label = QLabel("COM port")
        layout.addWidget(self.endpoint_label, 0, 2)
        layout.addWidget(self.port, 0, 3)
        layout.addWidget(self.refresh_button, 0, 4)
        layout.addWidget(QLabel("PC baud"), 0, 5)
        layout.addWidget(self.baud, 0, 6)
        layout.addWidget(self.connect_button, 0, 7)
        layout.addWidget(self.capture_button, 0, 8)
        layout.addWidget(self.emulation_button, 0, 9)
        layout.addWidget(self.status, 0, 10)
        layout.addWidget(self.auto_respond, 1, 0, 1, 2)
        layout.addWidget(QLabel("Response delay"), 1, 2)
        layout.addWidget(self.delay, 1, 3)
        layout.addWidget(QLabel("Quella baud"), 1, 4)
        layout.addWidget(self.quella_baud, 1, 5)
        layout.addWidget(self.profile_save, 1, 8)
        layout.addWidget(self.profile_load, 1, 9)
        layout.setColumnStretch(3, 1)
        return box

    def _transport_changed(self, ignored: int | None = None) -> None:
        mega = self.transport_mode.currentData() == "mega_bridge"
        mode = str(self.transport_mode.currentData())
        network = mode in ("network_udp", "network_homeserver", "network_ws")
        running = bool(self.worker and self.worker.isRunning())
        self.endpoint_label.setText(
            "UDP bind" if mode == "network_udp"
            else ("Home server URL" if mode == "network_homeserver"
                  else ("WS URL" if mode == "network_ws" else "COM port"))
        )
        self.refresh_button.setEnabled(not network and not running)
        self.baud.setEnabled(not network and not running)
        self.quella_baud.setEnabled(mega and not running)
        self.auto_respond.setEnabled(not mega and not network and self.emulator_role is EmulatorRole.MAIN_MCU)
        self.delay.setEnabled(not mega and not network and self.emulator_role is EmulatorRole.MAIN_MCU)
        self.capture_button.setEnabled(not network)
        endpoint = self.port.currentText().strip()
        if mode == "network_udp" and not endpoint.startswith("udp://"):
            self.port.setCurrentText("udp://0.0.0.0:42042")
        elif mode == "network_homeserver" and not endpoint.startswith(("http://", "https://")):
            self.port.setCurrentText("https://osmofresh-quella.server.home/state")
        elif mode == "network_ws" and not endpoint.startswith(("ws://", "wss://")):
            self.port.setCurrentText("ws://10.0.1.27:81/ws")
        elif not network and endpoint.startswith(("udp://", "http://", "https://", "ws://", "wss://")):
            self.refresh_ports()
        self._apply_role_ui()

    def _is_network_transport(self) -> bool:
        return bool(
            hasattr(self, "transport_mode")
            and self.transport_mode.currentData() in (
                "network_udp", "network_homeserver", "network_ws"
            )
        )

    def _mega_config(self) -> MegaConfig:
        config = self.mega_bridge_tab.config()
        try:
            quella_baud = int(self.quella_baud.currentText())
        except ValueError:
            quella_baud = 9600
        return MegaConfig(config.mode, quella_baud, config.idle_gap_us, config.sniff_enabled)

    def _mega_config_requested(self, config: MegaConfig) -> None:
        self.quella_baud.setCurrentText(str(config.quella_baud))
        if self.worker and self.worker.isRunning():
            self.worker.configure_mega(self._mega_config())

    def _mega_inject_requested(
        self, direction: BridgeDirection, payload: bytes
    ) -> None:
        if not self.worker or not self.worker.isRunning():
            QMessageBox.warning(self, "Disconnected", "Connect the Mega bridge first.")
            return
        self.worker.inject_mega(direction, payload)

    def _mega_tds_test_requested(self, raw_value: int) -> None:
        if not isinstance(self.worker, SerialWorker) or not self.worker.isRunning():
            QMessageBox.warning(self, "Disconnected", "Connect the Mega bridge first.")
            return
        self.worker.queue_tds_test_value(raw_value)

    def _mega_tds_test_disable_requested(self) -> None:
        if isinstance(self.worker, SerialWorker) and self.worker.isRunning():
            self.worker.disable_tds_test()

    def _mega_set_rule_requested(self, rule: MegaRule) -> None:
        if not self.worker or not self.worker.isRunning():
            QMessageBox.warning(self, "Disconnected", "Connect the Mega bridge first.")
            return
        self.worker.set_mega_rule(rule)

    def _mega_rule_command_requested(self, command: MegaMessageType) -> None:
        if not self.worker or not self.worker.isRunning():
            QMessageBox.warning(self, "Disconnected", "Connect the Mega bridge first.")
            return
        self.worker.mega_rule_command(command)

    def _mega_manual_dispense_requested(
        self, volume_ml: int, feed_pump_parameter: int
    ) -> None:
        if not self.worker or not self.worker.isRunning():
            QMessageBox.warning(self, "Disconnected", "Connect the Mega bridge first.")
            return
        self.worker.mega_command_dispense(volume_ml, feed_pump_parameter)

    def _mega_stop_remote_dispense_requested(self) -> None:
        if not self.worker or not self.worker.isRunning():
            QMessageBox.warning(self, "Disconnected", "Connect the Mega bridge first.")
            return
        self.worker.mega_stop_remote_dispense()

    def _mega_tank_config_requested(self, config) -> None:
        if not self.worker or not self.worker.isRunning():
            QMessageBox.warning(self, "Disconnected", "Connect the Mega bridge first.")
            return
        self.worker.configure_mega_tank(config)

    def _live_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        controls = QHBoxLayout()
        self.pause_log = QCheckBox("Pause display")
        self.pause_log.toggled.connect(self._pause_changed)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_log)
        save = QPushButton("Save")
        save.clicked.connect(self._save_log)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.live_log.toPlainText()))
        controls.addWidget(self.pause_log)
        controls.addWidget(clear)
        controls.addWidget(save)
        controls.addWidget(copy)
        controls.addWidget(QLabel("Filters:"))
        self.filters: dict[str, QCheckBox] = {}
        for kind in ("A5", "E5 B2", "E5 E1", "AA", "CC", "UNKNOWN"):
            checkbox = QCheckBox(kind)
            checkbox.setChecked(True)
            checkbox.toggled.connect(self._render_log)
            self.filters[kind] = checkbox
            controls.addWidget(checkbox)
        controls.addStretch()
        layout.addLayout(controls)
        self.live_log = QTextEdit()
        self.live_log.setReadOnly(True)
        self.live_log.setFont(QFont("monospace"))
        layout.addWidget(self.live_log)

        manual = QGroupBox("Manual send — one transmission per click")
        manual_layout = QHBoxLayout(manual)
        self.manual_hex = QLineEdit()
        self.manual_hex.setPlaceholderText("A5   or   CC   or   00 00 19 ...")
        self.manual_auto_sum16 = QCheckBox("Append SUM16")
        self.manual_auto_sum16.setToolTip(
            "Append SUM16 to 16 payload bytes, or to AA + 16 payload bytes (AA excluded)."
        )
        send = QPushButton("Send once")
        send.clicked.connect(self.manual_send)
        manual_layout.addWidget(self.manual_hex)
        manual_layout.addWidget(self.manual_auto_sum16)
        manual_layout.addWidget(send)
        layout.addWidget(manual)
        return widget

    def _raw_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        label = QLabel("Every serial read is shown here, independent of protocol recognition and live-log pause.")
        self.raw_log = QTextEdit()
        self.raw_log.setReadOnly(True)
        self.raw_log.setFont(QFont("monospace"))
        clear = QPushButton("Clear raw monitor")
        clear.clicked.connect(self.raw_log.clear)
        layout.addWidget(label)
        layout.addWidget(self.raw_log)
        layout.addWidget(clear)
        return widget

    def _frames_tab(self) -> QTabWidget:
        tabs = QTabWidget()
        self.main_frame_editor_indices = []
        for dataset in Dataset:
            editor = FrameEditor(
                self.frames[dataset], self.knowledge, self.response_sets[dataset]
            )
            editor.frame_changed.connect(self._frame_changed)
            editor.response_set_changed.connect(self._response_set_changed)
            self.frame_editors[dataset] = editor
            self.main_frame_editor_indices.append(tabs.addTab(editor, dataset.value))
        self.aa_frame_editors: dict[str, AAFrameEditor] = {}
        self.aa_frame_editor_indices = []
        config = self.display_emulator_tab.config()
        class_titles = (
            ("control_setpoint_00", "AA — Data[15]=00"),
            ("system_process_a3", "AA — Data[15]=A3"),
            ("system_process_e1", "AA — Data[15]=E1"),
        )
        defaults = {preset.knowledge_class: preset for preset in default_aa_presets()}
        for class_id, title in class_titles:
            preset = next(
                (item for item in config.aa_presets if item.knowledge_class == class_id),
                defaults[class_id],
            )
            editor = AAFrameEditor(
                class_id,
                preset.payload,
                self.knowledge,
                preset.auto_checksum,
                preset.checksum_high,
                preset.checksum_low,
            )
            editor.preset_changed.connect(self._aa_editor_preset_changed)
            editor.send_once_requested.connect(self._send_aa_class_once)
            editor.apply_auto_requested.connect(self._apply_aa_frame_to_auto_state)
            self.aa_frame_editors[class_id] = editor
            self.aa_frame_editor_indices.append(tabs.addTab(editor, title))

        self.display_request_buttons = QWidget()
        request_layout = QHBoxLayout(self.display_request_buttons)
        request_layout.setContentsMargins(0, 0, 4, 0)
        request_layout.addWidget(QLabel("Request once:"))
        self.display_request_once_buttons = []
        for request in (DisplayRequest.A5, DisplayRequest.E5_B2, DisplayRequest.E5_E1):
            button = QPushButton(request.value)
            button.setToolTip(f"Send {request.value} once and capture the Main-MCU response")
            button.clicked.connect(
                lambda ignored=False, selected=request: self._send_display_request(selected.value)
            )
            button.setEnabled(False)
            request_layout.addWidget(button)
            self.display_request_once_buttons.append(button)
        tabs.setCornerWidget(
            self.display_request_buttons, Qt.Corner.TopRightCorner
        )
        return tabs

    def _notes_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.notes = QTableWidget(0, 11)
        self.notes.setHorizontalHeaderLabels(
            ["Dataset", "Offset", "Bit", "Original", "Modified", "Timestamp",
             "Visible effect", "Displayed error", "LED effect", "Buzzer effect", "Notes"]
        )
        self.notes.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.notes.horizontalHeader().setStretchLastSection(True)
        controls = QHBoxLayout()
        add = QPushButton("Add note row")
        add.clicked.connect(lambda: self.notes.insertRow(self.notes.rowCount()))
        export_json = QPushButton("Export JSON")
        export_json.clicked.connect(lambda: self._export_notes("json"))
        export_csv = QPushButton("Export CSV")
        export_csv.clicked.connect(lambda: self._export_notes("csv"))
        controls.addWidget(add)
        controls.addWidget(export_json)
        controls.addWidget(export_csv)
        controls.addStretch()
        layout.addLayout(controls)
        layout.addWidget(self.notes)
        return widget

    def _history_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(
            ["Timestamp", "Direction", "Frame", "Payload class", "Raw frame"]
        )
        self.history.horizontalHeader().setStretchLastSection(True)
        self.history.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history.itemSelectionChanged.connect(self._update_diff)
        self.diff = QTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setFont(QFont("monospace"))
        self.diff.setPlaceholderText("Select a frame to compare it with the previous frame of the same kind.")
        splitter.addWidget(self.history)
        splitter.addWidget(self.diff)
        layout.addWidget(splitter)
        return widget

    def _dataset_inspector_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        help_text = QLabel(
            "Observed traffic grouped by direction, request/frame and real payload class. "
            "SUM16 bytes are never used as frame identity."
        )
        help_text.setWordWrap(True)
        self.dataset_table = QTableWidget(0, 9)
        self.dataset_table.setHorizontalHeaderLabels(
            [
                "Direction", "Frame", "Payload class", "Count", "First seen",
                "Last seen", "Unique payloads", "Changed raw / logical offsets", "Latest raw frame",
            ]
        )
        self.dataset_table.setFont(QFont("monospace"))
        self.dataset_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(help_text)
        layout.addWidget(self.dataset_table)
        return widget

    def _statistics_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        summary = QWidget()
        form = QFormLayout(summary)
        names = [
            "a5_requests", "e5_b2_requests", "e5_e1_requests", "aa_frames",
            "valid_aa_checksums", "invalid_aa_checksums", "unknown_bytes",
            "responses_sent", "average_latency_ms", "minimum_latency_ms", "maximum_latency_ms",
        ]
        self.stat_labels = {}
        for name in names:
            label = QLabel("0")
            self.stat_labels[name] = label
            form.addRow(name.replace("_", " ").title(), label)
        self.class_histogram = QTableWidget(0, 4)
        self.class_histogram.setHorizontalHeaderLabels(
            ["Direction", "Request / Frame", "Payload class", "Count"]
        )
        self.class_histogram.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(summary)
        layout.addWidget(QLabel("Payload class histogram"))
        layout.addWidget(self.class_histogram)
        return widget

    def refresh_ports(self) -> None:
        current = self.port.currentText()
        try:
            from serial.tools import list_ports
            ports = [entry.device for entry in list_ports.comports()]
        except ImportError:
            ports = []
        self.port.clear()
        self.port.addItems(ports)
        if current and current not in ports:
            self.port.addItem(current)
        if current:
            self.port.setCurrentText(current)
        else:
            preferred = preferred_serial_port(ports)
            if preferred:
                self.port.setCurrentText(preferred)

    def toggle_connection(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.request_stop()
            self.connect_button.setEnabled(False)
            return
        self._start_worker(
            emulation_active=self.emulator_role is EmulatorRole.MAIN_MCU
        )

    def start_capture(self) -> None:
        if self.worker and self.worker.isRunning():
            if self.emulator_role is EmulatorRole.DISPLAY:
                self.worker.stop_display_polling()
                self.display_emulator_tab.force_safe_idle()
                self._set_status("PASSIVE LISTEN")
            else:
                self.worker.set_emulation(False)
                self.emulation_button.setText("Start emulation")
                self._set_status("CONNECTED")
        else:
            self._start_worker(emulation_active=False)

    def _start_worker(self, emulation_active: bool) -> None:
        port = self.port.currentText().strip()
        transport_mode = str(self.transport_mode.currentData())
        if not port:
            QMessageBox.warning(self, "No endpoint", "Enter a network endpoint or select a serial port.")
            return
        if transport_mode == "network_udp":
            self.worker = UdpTelemetryWorker(port)
            self.worker.network_frame.connect(self._network_frame_received)
            self.worker.telemetry_statistics.connect(self._udp_statistics_received)
            emulation_active = False
        elif transport_mode == "network_homeserver":
            self.worker = HomeServerStateWorker(port)
            self.worker.network_frame.connect(self._network_frame_received)
            self.worker.telemetry_statistics.connect(self._udp_statistics_received)
            emulation_active = False
        elif transport_mode == "network_ws":
            self.worker = NetworkCaptureWorker(port)
            self.worker.network_frame.connect(self._network_frame_received)
            emulation_active = False
        else:
            try:
                baud = int(self.baud.currentText())
            except ValueError:
                QMessageBox.warning(self, "Invalid baud", "Baud rate must be an integer.")
                return
            responses = {
                dataset: response_set.clone()
                for dataset, response_set in self.response_sets.items()
            }
            if transport_mode == "mega_bridge":
                emulation_active = False
            if self.emulator_role is EmulatorRole.DISPLAY:
                emulation_active = False
                self.display_emulator_tab.force_safe_idle()
            self.worker = SerialWorker(
                port,
                baud,
                responses,
                self.delay.value(),
                emulation_active,
                self.auto_respond.isChecked(),
                self.replay_tab.current_profile,
                emulator_role=self.emulator_role,
                display_config=self.display_emulator_tab.config(),
                transport_mode=transport_mode,
                mega_config=self._mega_config(),
            )
            self.worker.frame_received.connect(self._frame_received)
            self.worker.bytes_sent.connect(self._bytes_sent)
            self.worker.statistics.connect(self._statistics_received)
            self.worker.replay_event.connect(self.replay_tab.append_event)
            self.worker.replay_state.connect(self.replay_tab.update_state)
            self.worker.display_event.connect(self._display_event_received)
            self.worker.display_statistics.connect(self.display_emulator_tab.update_statistics)
            self.worker.display_state.connect(self.display_emulator_tab.update_display_state)
            self.worker.bridge_event.connect(self.mega_bridge_tab.append_event)
            self.worker.bridge_event.connect(self._live_state_bridge_event)
            self.worker.tds_test_state.connect(self.mega_bridge_tab.update_tds_test_state)
        self.worker.connected.connect(lambda: self._worker_connected(emulation_active))
        self.worker.disconnected.connect(self._worker_disconnected)
        self.worker.failure.connect(self._worker_failure)
        self.worker.raw_received.connect(self._raw_received)
        self.worker.protocol_frame.connect(self.live_analysis_tab.observe_frame)
        self.worker.protocol_frame.connect(self._protocol_frame_observed)
        self.worker.protocol_frame.connect(self.live_state.observe_frame)
        self._set_status("CONNECTING")
        self.worker.start()

    def _worker_connected(self, emulation_active: bool) -> None:
        network = self._is_network_transport()
        self.connect_button.setText("Disconnect")
        self.connect_button.setEnabled(True)
        self.capture_button.setEnabled(True)
        self.emulation_button.setEnabled(not network)
        self._set_serial_inputs_enabled(False)
        self.role_selector.setEnabled(False)
        self.transport_mode.setEnabled(False)
        self.mega_bridge_tab.set_connected(
            self.transport_mode.currentData() == "mega_bridge"
        )
        display_role = self.emulator_role is EmulatorRole.DISPLAY
        self.display_emulator_tab.set_connected(display_role and not network)
        for button in self.display_request_once_buttons:
            button.setEnabled(display_role and not network)
        for editor in self.aa_frame_editors.values():
            editor.set_send_enabled(display_role and not network)
        if network:
            self.capture_button.setEnabled(False)
            self._set_status("NETWORK PASSIVE")
        elif display_role:
            self._set_status("PASSIVE LISTEN")
            self.emulation_button.setText("Start emulation")
        else:
            self._set_status("EMULATION ACTIVE" if emulation_active else "CONNECTED")
            self.emulation_button.setText("Stop emulation" if emulation_active else "Start emulation")

    def _worker_disconnected(self) -> None:
        self.connect_button.setText("Connect")
        self.connect_button.setEnabled(True)
        self.capture_button.setEnabled(True)
        self.emulation_button.setEnabled(False)
        self.emulation_button.setText("Start emulation")
        self._set_serial_inputs_enabled(True)
        self.role_selector.setEnabled(True)
        self.transport_mode.setEnabled(True)
        self.mega_bridge_tab.set_connected(False)
        self.display_emulator_tab.set_connected(False)
        for button in self.display_request_once_buttons:
            button.setEnabled(False)
        for editor in self.aa_frame_editors.values():
            editor.set_send_enabled(False)
        self._set_status("DISCONNECTED")
        self.capture_button.setEnabled(not self._is_network_transport())

    def _worker_failure(self, message: str) -> None:
        self._append_system(message)
        QMessageBox.critical(self, "Connection error", message)

    def toggle_emulation(self) -> None:
        if (
            not self.worker
            or not self.worker.isRunning()
            or self._is_network_transport()
        ):
            return
        active = self.emulation_button.text() == "Start emulation"
        self.worker.set_emulation(active)
        self.emulation_button.setText("Stop emulation" if active else "Start emulation")
        self._set_status("EMULATION ACTIVE" if active else "CONNECTED")

    def _set_serial_inputs_enabled(self, enabled: bool) -> None:
        self.port.setEnabled(enabled)
        network = self._is_network_transport()
        self.baud.setEnabled(enabled and not network)
        self.refresh_button.setEnabled(enabled and not network)
        self.quella_baud.setEnabled(
            enabled and self.transport_mode.currentData() == "mega_bridge" and not network
        )

    def _set_status(self, status: str) -> None:
        colors = {"DISCONNECTED": "#d65c5c", "CONNECTED": "#e0b84d", "PASSIVE LISTEN": "#e0b84d", "NETWORK PASSIVE": "#55c57a", "POLLING ACTIVE": "#55c57a", "EMULATION ACTIVE": "#55c57a", "PAUSED": "#8ba7d8"}
        self.status.setText(status)
        self.status.setStyleSheet(f"font-weight:bold;color:{colors.get(status, '#bbbbbb')}")

    def _pause_changed(self, paused: bool) -> None:
        if paused:
            self._set_status("PAUSED")
        elif self.worker and self.worker.isRunning():
            if self._is_network_transport():
                self._set_status("NETWORK PASSIVE")
            elif self.emulator_role is EmulatorRole.DISPLAY:
                self._set_status("PASSIVE LISTEN")
            else:
                active = self.emulation_button.text() == "Stop emulation"
                self._set_status("EMULATION ACTIVE" if active else "CONNECTED")
            self._render_log()
        else:
            self._set_status("DISCONNECTED")

    def _raw_received(self, chunk: bytes, timestamp: float) -> None:
        self.raw_log.append(f"{timestamp_text(timestamp)}  RX READ  {chunk.hex(' ').upper()}")

    def _frame_received(self, kind: str, raw: bytes, valid, timestamp: float) -> None:
        suffix = ""
        if kind == "AA" and self.emulator_role is EmulatorRole.MAIN_MCU:
            suffix = "  [checksum VALID]" if valid else "  [checksum INVALID — no CC]"
        elif self.emulator_role is EmulatorRole.DISPLAY and kind in ("A5", "E5 B2", "E5 E1"):
            suffix = "  [VALID SUM16]" if valid else "  [INVALID SUM16]"
        elif self.emulator_role is EmulatorRole.DISPLAY and kind == "CC":
            suffix = "  [ACK received]"
        rx_direction, ignored = direction_labels(self.emulator_role)
        entry = {"time": timestamp, "direction": rx_direction, "kind": kind,
                 "raw": raw, "suffix": suffix}
        self.log_entries.append(entry)
        self._append_log_entry(entry)

    def _network_frame_received(
        self, direction: str, kind: str, raw: bytes, valid, timestamp: float
    ) -> None:
        suffix = ""
        if valid is not None:
            suffix = "  [checksum VALID]" if valid else "  [checksum INVALID]"
        entry = {"time": timestamp, "direction": direction, "kind": kind,
                 "raw": raw, "suffix": suffix}
        self.log_entries.append(entry)
        self._append_log_entry(entry)

    def _udp_statistics_received(self, statistics: dict) -> None:
        packets = int(statistics.get("packets", 0))
        frames = int(statistics.get("frames", 0))
        lost = int(statistics.get("lost", 0))
        malformed = int(statistics.get("malformed", 0))
        mega_dropped = int(statistics.get("mega_dropped", 0))
        self.status.setToolTip(
            f"UDP packets: {packets} | frames: {frames} | "
            f"sequence gaps: {lost} | malformed: {malformed} | "
            f"Mega dropped raw frames: {mega_dropped}"
        )

    def _bytes_sent(
        self, kind: str, raw: bytes, timestamp: float, latency_ms: float
    ) -> None:
        if latency_ms >= 0:
            suffix = f"  [latency {latency_ms:.3f} ms]"
        elif latency_ms == -2.0:
            suffix = "  [display request]"
        else:
            suffix = "  [manual]"
        ignored, tx_direction = direction_labels(self.emulator_role)
        entry = {"time": timestamp, "direction": tx_direction, "kind": kind,
                 "raw": raw, "suffix": suffix}
        self.log_entries.append(entry)
        self._append_log_entry(entry)

    def _entry_text(self, entry: dict) -> str:
        payload_class = frame_payload_class(entry["kind"], entry["raw"])
        label = entry["kind"] + (f" / {payload_class}" if payload_class else "")
        raw = entry["raw"]
        if entry["kind"] == "AA" and len(raw) == 19:
            raw_text = f"{raw[:17].hex(' ').upper()} | {raw[17]:02X} | {raw[18]:02X}"
        elif entry["kind"] in ("A5", "E5 B2", "E5 E1") and len(raw) == 18:
            raw_text = f"{raw[:16].hex(' ').upper()} | {raw[16]:02X} | {raw[17]:02X}"
        else:
            raw_text = raw.hex(" ").upper()
        return (
            f"{timestamp_text(entry['time'])}  {entry['direction']:<17} "
            f"{label:<19} {raw_text}{entry['suffix']}"
        )

    def _entry_visible(self, entry: dict) -> bool:
        if entry["kind"] in ("TX", "MANUAL", "AA ACK"):
            return True
        return self.filters.get(entry["kind"], self.filters["UNKNOWN"]).isChecked()

    def _append_log_entry(self, entry: dict) -> None:
        if self.pause_log.isChecked() or not self._entry_visible(entry):
            return
        if self._is_network_transport():
            color = "#72b7ff" if entry["direction"] == "DISPLAY_TO_MAIN" else "#75d694"
        else:
            rx_direction, ignored = direction_labels(self.emulator_role)
            color = "#72b7ff" if entry["direction"] == rx_direction else "#75d694"
        escaped = self._entry_text(entry).replace("&", "&amp;").replace("<", "&lt;")
        self.live_log.append(f'<span style="color:{color};white-space:pre">{escaped}</span>')

    def _render_log(self, ignored=None) -> None:
        if self.pause_log.isChecked():
            return
        self.live_log.clear()
        for entry in self.log_entries:
            if self._entry_visible(entry):
                self._append_log_entry(entry)
        self.live_log.moveCursor(QTextCursor.MoveOperation.End)

    def _append_system(self, message: str) -> None:
        self.live_log.append(f'<span style="color:#ef8e8e">SYSTEM: {message}</span>')

    def _clear_log(self) -> None:
        self.log_entries.clear()
        self.live_log.clear()

    def _save_log(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, "Save UART log", "uart-log.txt", "Text (*.txt)")
        if filename:
            try:
                Path(filename).write_text("\n".join(self._entry_text(e) for e in self.log_entries), encoding="utf-8")
            except OSError as exc:
                QMessageBox.critical(self, "Save failed", str(exc))

    def manual_send(self) -> None:
        if not self.worker or not self.worker.isRunning():
            QMessageBox.warning(self, "Disconnected", "Connect a serial port before sending.")
            return
        try:
            payload = parse_hex_bytes(self.manual_hex.text())
            if not payload:
                raise ValueError("enter at least one byte")
            if self.manual_auto_sum16.isChecked():
                if len(payload) == 16:
                    payload += bytes(sum16_bytes(payload))
                elif len(payload) == 17 and payload[0] == 0xAA:
                    payload += bytes(sum16_bytes(payload[1:]))
                else:
                    raise ValueError(
                        "automatic SUM16 requires 16 payload bytes or AA + 16 payload bytes"
                    )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid hex", str(exc))
            return
        self.worker.send_once(payload)

    def _display_config_changed(self, config: DisplayRoleConfig) -> None:
        if hasattr(self, "aa_frame_editors"):
            for class_id, editor in self.aa_frame_editors.items():
                preset = next(
                    (
                        item
                        for item in config.aa_presets
                        if item.knowledge_class == class_id
                    ),
                    None,
                )
                if preset is not None:
                    editor.set_frame_config(
                        preset.payload,
                        preset.auto_checksum,
                        preset.checksum_high,
                        preset.checksum_low,
                    )
        if (
            self.emulator_role is EmulatorRole.DISPLAY
            and self.worker
            and self.worker.isRunning()
        ):
            self.worker.configure_display_role(config)

    def _aa_editor_preset_changed(
        self,
        class_id: str,
        payload: bytes,
        auto_checksum: bool,
        checksum_high: int,
        checksum_low: int,
    ) -> None:
        config = self.display_emulator_tab.config()
        preset = next(
            (item for item in config.aa_presets if item.knowledge_class == class_id),
            None,
        )
        if preset is None:
            template = next(
                item for item in default_aa_presets() if item.knowledge_class == class_id
            )
            preset = AAPayloadPreset(
                template.name,
                payload,
                template.enabled,
                class_id,
                auto_checksum,
                checksum_high,
                checksum_low,
            )
            config.aa_presets.append(preset)
        else:
            preset.payload = bytes(payload)
            preset.auto_checksum = auto_checksum
            preset.checksum_high = checksum_high
            preset.checksum_low = checksum_low
        self.display_emulator_tab.load_config(config)
        self._display_config_changed(config)

    def _require_display_connection(self) -> bool:
        if (
            self.emulator_role is not EmulatorRole.DISPLAY
            or not self.worker
            or not self.worker.isRunning()
        ):
            QMessageBox.warning(
                self,
                "Display Emulator disconnected",
                "Select Display Emulator and connect the UART first.",
            )
            return False
        self.worker.configure_display_role(self.display_emulator_tab.config())
        return True

    def _start_display_polling(self) -> None:
        if self._require_display_connection():
            self.worker.start_display_polling()

    def _stop_display_polling(self) -> None:
        if self._require_display_connection():
            self.worker.stop_display_polling()

    def _step_display_request(self) -> None:
        if self._require_display_connection():
            self.worker.step_display_request()

    def _send_display_request(self, request: str) -> None:
        if self._require_display_connection():
            self.worker.send_display_request(DisplayRequest(request))

    def _send_display_aa_preset(self, preset_index: int) -> None:
        if self._require_display_connection():
            self.worker.send_display_aa_preset(preset_index)

    def _abort_water_process(self) -> None:
        if self._require_display_connection():
            self.worker.abort_water_process()

    def _send_aa_class_once(self, class_id: str) -> None:
        config = self.display_emulator_tab.config()
        preset_index = next(
            (
                index
                for index, preset in enumerate(config.aa_presets)
                if preset.knowledge_class == class_id
            ),
            -1,
        )
        if preset_index < 0:
            QMessageBox.warning(
                self,
                "AA preset missing",
                f"No AA command preset is assigned to {class_id}.",
            )
            return
        self._send_display_aa_preset(preset_index)

    def _live_state_bridge_event(self, event: dict) -> None:
        self.live_state.observe_bridge_event(event, time.time())

    def _apply_aa_frame_to_auto_state(self, class_id: str, payload: bytes) -> None:
        try:
            self.display_emulator_tab.apply_payload_to_auto_state(payload)
            self._set_status(f"{class_id} applied to automatic AA state")
        except ValueError as exc:
            QMessageBox.warning(self, "Auto AA state", str(exc))

    def _display_event_received(self, event: dict) -> None:
        self.display_emulator_tab.append_event(event)
        status = str(event.get("status", ""))
        if status == "POLLING STARTED":
            self._set_status("POLLING ACTIVE")
        elif status in ("POLLING STOPPED", "NO TRAFFIC OBSERVED"):
            self._set_status("PASSIVE LISTEN")
        if (
            "TIMEOUT" in status
            or "BLOCKED" in status
            or "UNEXPECTED" in status
            or status == "NO TRAFFIC OBSERVED"
        ):
            self._append_system(status)

    def _frame_changed(self, dataset_name: str, raw: bytes) -> None:
        dataset = Dataset(dataset_name)
        if isinstance(self.worker, SerialWorker) and self.worker.isRunning():
            self.worker.set_response_set(dataset, self.response_sets[dataset])
        self.bit_test.refresh()
        self.replay_tab.refresh_comparison()

    def _response_set_changed(
        self, dataset_name: str, response_set: RequestResponseSet
    ) -> None:
        dataset = Dataset(dataset_name)
        self.response_sets[dataset] = response_set
        self.frames[dataset] = response_set.selected_payload_state().frame
        if isinstance(self.worker, SerialWorker) and self.worker.isRunning():
            self.worker.set_response_set(dataset, response_set)
        self.bit_test.refresh()
        self.replay_tab.refresh_comparison()

    def _replay_profile_changed(self, profile: ReplayProfile) -> None:
        self.delay.setValue(profile.response_delay_ms)
        if not self.worker or not self.worker.isRunning():
            baud = profile.uart.get("baud")
            if baud:
                self.baud.setCurrentText(str(baud))
        elif isinstance(self.worker, SerialWorker):
            self.worker.configure_replay(profile)

    def _start_replay(self, mode: ReplayMode) -> None:
        if not self.worker or not self.worker.isRunning():
            QMessageBox.warning(self, "Disconnected", "Connect the UART before starting replay or capture learning.")
            return
        self.worker.configure_replay(self.replay_tab.current_profile)
        if mode is ReplayMode.CAPTURE_LEARN:
            self.worker.set_emulation(False)
            self.emulation_button.setText("Start emulation")
            self._set_status("CONNECTED")
        else:
            self.auto_respond.setChecked(True)
            self.worker.set_emulation(True)
            self.emulation_button.setText("Stop emulation")
            self._set_status("EMULATION ACTIVE")
        self.worker.start_replay(mode)

    def _reset_replay(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.reset_replay()

    def _pause_replay(self, paused: bool) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.pause_replay(paused)
            self._set_status("PAUSED" if paused else "EMULATION ACTIVE")

    def _next_replay_step(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.next_replay_step()

    def _switch_replay_static(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.set_emulation(True)
            self.worker.switch_replay_to_static()
            self.emulation_button.setText("Stop emulation")
            self._set_status("EMULATION ACTIVE")

    def _apply_bit_test(self, dataset_name: str, offset: int, bit: int) -> None:
        dataset = Dataset(dataset_name)
        frame = self.frames[dataset]
        response_set = self.response_sets[dataset]
        original = response_set.shared_data.get(offset, frame.data[offset])
        modified = original ^ (1 << bit)
        if response_set.is_global(offset):
            try:
                self.knowledge.set_shared_value(dataset, offset, modified)
            except (KeyError, OSError, ValueError) as exc:
                QMessageBox.critical(self, "Shared Data save failed", str(exc))
                return
            response_set.set_global(offset, modified)
        else:
            frame.set_data(offset, modified)
        self.frame_editors[dataset].refresh()
        self._response_set_changed(dataset_name, response_set)
        row = self.notes.rowCount()
        self.notes.insertRow(row)
        initial = [dataset.value, str(offset), str(bit), f"{original:02X}", f"{modified:02X}",
                   datetime.now().isoformat(timespec="seconds"), "", "", "", "", ""]
        for column, value in enumerate(initial):
            self.notes.setItem(row, column, QTableWidgetItem(value))

    def _export_notes(self, kind: str) -> None:
        suffix = "json" if kind == "json" else "csv"
        filename, _ = QFileDialog.getSaveFileName(self, "Export experiment notes", f"experiments.{suffix}")
        if not filename:
            return
        headers = [self.notes.horizontalHeaderItem(c).text() for c in range(self.notes.columnCount())]
        rows = [[self.notes.item(r, c).text() if self.notes.item(r, c) else ""
                 for c in range(self.notes.columnCount())] for r in range(self.notes.rowCount())]
        try:
            if kind == "json":
                Path(filename).write_text(json.dumps([dict(zip(headers, row)) for row in rows], indent=2), encoding="utf-8")
            else:
                with Path(filename).open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(headers)
                    writer.writerows(rows)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def _protocol_frame_observed(
        self, direction: str, kind: str, raw: bytes, timestamp: float
    ) -> None:
        if direction == "MAIN_TO_DISPLAY" and len(raw) == 18:
            try:
                dataset = Dataset(kind)
            except ValueError:
                pass
            else:
                editor = self.frame_editors.get(dataset)
                if editor is not None:
                    editor.observe_live_response(raw, timestamp)
        observation = self.dataset_tracker.observe(direction, kind, raw, timestamp)
        self._add_history(direction, kind, observation.key.payload_class, raw, timestamp)
        self._refresh_dataset_views()

    def _refresh_dataset_views(self) -> None:
        rows = self.dataset_tracker.rows()
        self.dataset_table.setRowCount(len(rows))
        histogram_rows = [row for row in rows if row["payload_class"] is not None]
        self.class_histogram.setRowCount(len(histogram_rows))
        for row_index, entry in enumerate(rows):
            values = (
                entry["direction"],
                entry["frame_type"],
                entry["payload_class"] or "—",
                str(entry["count"]),
                timestamp_text(entry["first_seen"]),
                timestamp_text(entry["last_seen"]),
                str(entry["unique_payloads"]),
                ", ".join(
                    f"raw[{field['raw_offset']}]/{field['logical_offset']}"
                    for field in entry["changed_fields"]
                ) or "—",
                entry["latest"].hex(" ").upper(),
            )
            for column, value in enumerate(values):
                self.dataset_table.setItem(row_index, column, QTableWidgetItem(value))
        for row_index, entry in enumerate(histogram_rows):
            values = (
                entry["direction"],
                entry["frame_type"],
                entry["payload_class"] or "—",
                str(entry["count"]),
            )
            for column, value in enumerate(values):
                self.class_histogram.setItem(row_index, column, QTableWidgetItem(value))

    def _add_history(
        self,
        direction: str,
        kind: str,
        payload_class: str | None,
        raw: bytes,
        timestamp: float,
    ) -> None:
        display_time = timestamp_text(timestamp)
        self.history_entries.append((direction, kind, payload_class, raw, display_time))
        if len(self.history_entries) > 200:
            self.history_entries.pop(0)
            self.history.removeRow(0)
        row = self.history.rowCount()
        self.history.insertRow(row)
        for column, value in enumerate(
            (
                display_time,
                direction,
                kind,
                payload_class or "—",
                raw.hex(" ").upper(),
            )
        ):
            self.history.setItem(row, column, QTableWidgetItem(value))

    def _update_diff(self) -> None:
        row = self.history.currentRow()
        if not 0 <= row < len(self.history_entries):
            return
        direction, kind, payload_class, current, _ = self.history_entries[row]
        previous = None
        for candidate_direction, candidate_kind, candidate_class, raw, _ in reversed(
            self.history_entries[:row]
        ):
            if (
                candidate_direction == direction
                and candidate_kind == kind
                and candidate_class == payload_class
                and len(raw) == len(current)
            ):
                previous = raw
                break
        if previous is None:
            self.diff.setText(
                "No previous frame with the same direction, frame type, payload class, and length."
            )
            return
        xor = bytes(a ^ b for a, b in zip(current, previous))
        changed = [
            f"raw[{index}] / {frame_byte_metadata(kind, len(current), index).logical_offset}"
            for index, value in enumerate(xor)
            if value
        ]
        bit_changes = [
            f"raw[{index}] / {frame_byte_metadata(kind, len(current), index).logical_offset}: bits "
            + ",".join(str(bit) for bit in range(8) if value & (1 << bit))
            for index, value in enumerate(xor)
            if value
        ]
        self.diff.setText(
            f"Previous: {previous.hex(' ').upper()}\n"
            f"Current:  {current.hex(' ').upper()}\n"
            f"XOR:      {xor.hex(' ').upper()}\n"
            f"Changed bytes: {changed or 'none'}\n"
            f"Changed bits: {'; '.join(bit_changes) if bit_changes else 'none'}"
        )

    def _statistics_received(self, stats: dict) -> None:
        for name, label in self.stat_labels.items():
            value = stats.get(name, 0)
            label.setText(f"{value:.3f} ms" if name.endswith("latency_ms") else str(value))

    def _auto_changed(self, checked: bool) -> None:
        if (
            isinstance(self.worker, SerialWorker)
            and self.worker.isRunning()
        ):
            self.worker.set_auto_respond(checked)

    def _delay_changed(self, value: float) -> None:
        if (
            isinstance(self.worker, SerialWorker)
            and self.worker.isRunning()
        ):
            self.worker.set_delay(value)

    def _profile_data(self) -> dict:
        mega_config = self._mega_config()
        return {
            "emulation_mode": self.emulator_role.value,
            "serial": {
                "transport": self.transport_mode.currentData(),
                "port": self.port.currentText(),
                "baud": int(self.baud.currentText() or 115200),
                "quella_baud": mega_config.quella_baud,
                "data_bits": 8,
                "parity": "N",
                "stop_bits": 1,
            },
            "mega_bridge": {
                "mode": mega_config.mode.name,
                "idle_gap_us": mega_config.idle_gap_us,
                "sniff_enabled": mega_config.sniff_enabled,
            },
            "response_delay_ms": self.delay.value(),
            "auto_respond": self.auto_respond.isChecked(),
            "responses": {
                dataset.value: response_set.selected_response().hex(" ").upper()
                for dataset, response_set in self.response_sets.items()
            },
            "response_sets": {
                dataset.value: response_set.to_dict()
                for dataset, response_set in self.response_sets.items()
            },
            "auto_checksum": {dataset.value: frame.auto_checksum for dataset, frame in self.frames.items()},
            "display_role": self.display_emulator_tab.config().to_dict(),
        }

    def save_profile(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, "Save profile", "uart-profile.json", "JSON (*.json)")
        if filename:
            try:
                Path(filename).write_text(json.dumps(self._profile_data(), indent=2), encoding="utf-8")
            except (OSError, ValueError) as exc:
                QMessageBox.critical(self, "Save failed", str(exc))

    def load_profile(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Load profile", "", "JSON (*.json)")
        if not filename:
            return
        try:
            profile = json.loads(Path(filename).read_text(encoding="utf-8"))
            if self.worker and self.worker.isRunning():
                raise ValueError("Disconnect the UART before loading a profile that may change roles.")
            role_value = profile.get(
                "emulation_mode",
                profile.get("emulation_role", EmulatorRole.MAIN_MCU.value),
            )
            role = EmulatorRole(role_value)
            role_index = self.role_selector.findData(role.value)
            if role_index >= 0:
                self.role_selector.setCurrentIndex(role_index)
            display_config = DisplayRoleConfig.from_dict(
                profile.get("display_role", {}), safe_load=True
            )
            self.display_emulator_tab.load_config(display_config)
            self.display_emulator_tab.force_safe_idle()
            self._display_config_changed(self.display_emulator_tab.config())
            serial_config = profile.get("serial", {})
            transport = str(serial_config.get("transport", "direct_uart"))
            transport_index = self.transport_mode.findData(transport)
            if transport_index >= 0:
                self.transport_mode.setCurrentIndex(transport_index)
            self.port.setCurrentText(str(serial_config.get("port", self.port.currentText())))
            self.baud.setCurrentText(str(serial_config.get("baud", 9600)))
            self.quella_baud.setCurrentText(
                str(serial_config.get("quella_baud", 9600))
            )
            mega_values = profile.get("mega_bridge", {})
            if isinstance(mega_values, dict):
                from protocol.mega_bridge import BridgeMode

                try:
                    mode = BridgeMode[str(mega_values.get("mode", "DIRECT_PASS"))]
                except KeyError:
                    mode = BridgeMode.DIRECT_PASS
                self.mega_bridge_tab.load_config(
                    MegaConfig(
                        mode,
                        int(serial_config.get("quella_baud", 9600)),
                        int(mega_values.get("idle_gap_us", 4000)),
                        bool(mega_values.get("sniff_enabled", True)),
                    )
                )
            self.delay.setValue(float(profile.get("response_delay_ms", 1.0)))
            self.auto_respond.setChecked(bool(profile.get("auto_respond", True)))
            for dataset in Dataset:
                if isinstance(profile.get("response_sets", {}).get(dataset.value), dict):
                    response_set = RequestResponseSet.from_config(
                        dataset, profile["response_sets"][dataset.value]
                    )
                else:
                    # Graceful migration of old one-response profiles.
                    response_set = RequestResponseSet.from_config(
                        dataset, profile["responses"][dataset.value]
                    )
                auto_checksum = bool(
                    profile.get("auto_checksum", {}).get(dataset.value, True)
                )
                for state in response_set.payload_states:
                    state.frame.auto_checksum = auto_checksum
                self.response_sets[dataset] = response_set
                self.frames[dataset] = response_set.selected_payload_state().frame
                editor = self.frame_editors[dataset]
                editor.response_set = response_set
                editor.frame = self.frames[dataset]
                editor._synchronize_global_knowledge()
                editor.auto_checksum.blockSignals(True)
                editor.auto_checksum.setChecked(auto_checksum)
                editor.auto_checksum.blockSignals(False)
                editor._refresh_state_controls()
                editor.refresh()
                self._response_set_changed(dataset.value, response_set)
            self.bit_test.refresh()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "Invalid profile", str(exc))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.request_stop()
            if not self.worker.wait(2000):
                event.ignore()
                QMessageBox.warning(self, "Serial worker busy", "Disconnect the serial port and try again.")
                return
        self._closing = True
        for window in list(self._detached_windows.values()):
            window.close_without_docking()
        self._detached_windows.clear()
        event.accept()
