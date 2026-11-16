import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

import gui.main_window as main_window_module
from gui.aa_frame_editor import AAFrameEditor
from gui.display_emulator_tab import DisplayEmulatorTab
from gui.live_status_tab import LiveStatusTab
from protocol.checksum import validate_aa_frame
from protocol.display_role import AutoAAMode, DisplayRequest, EmulatorRole
from protocol.knowledge import ProtocolKnowledge
from protocol.mega_bridge import BridgeDirection, BridgeMode, FrameAction, MegaMessageType


def make_window(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        main_window_module,
        "ProtocolKnowledge",
        lambda: ProtocolKnowledge(tmp_path / "protocol_knowledge.json"),
    )
    return main_window_module.MainWindow()


def test_internal_max_display_uses_tank_level_wording():
    base = {"known": True, "value": False, "unit": "", "age_ms": 0,
            "freshness": "fresh", "source_frame": "E5 B2", "raw": 0x20,
            "confidence": "CONFIRMED"}
    assert LiveStatusTab._display("internal_max", base).startswith("FULL / MAX REACHED")
    assert LiveStatusTab._display("internal_max", {**base, "value": True}).startswith("BELOW MAX")


def test_mode_switch_updates_role_and_display_mode_starts_safe(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    index = window.role_selector.findData(EmulatorRole.DISPLAY.value)
    window.role_selector.setCurrentIndex(index)
    assert window.emulator_role is EmulatorRole.DISPLAY
    assert not window.display_emulator_tab.allow_aa.isChecked()
    assert not window.display_emulator_tab.request_enabled[DisplayRequest.AA].isChecked()
    assert window.display_emulator_tab.scheduler_status.text().startswith("Polling OFF")
    assert not window.replay_tab.isEnabled()
    window.close()


def test_network_transport_is_passive_and_uses_gateway_default(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    index = window.transport_mode.findData("network_ws")
    window.transport_mode.setCurrentIndex(index)
    assert window.port.currentText() == "ws://10.0.1.27:81/ws"
    assert window.endpoint_label.text() == "WS URL"
    assert not window.baud.isEnabled()
    assert not window.capture_button.isEnabled()
    assert not window.emulation_button.isVisible()
    assert not window.replay_tab.isEnabled()
    assert not window.display_emulator_tab.isEnabled()
    assert "PASSIVE NETWORK ANALYSIS" in window.safety_banner.text()
    window.close()


def test_udp_transport_is_passive_and_routes_response_to_frame_editor(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    index = window.transport_mode.findData("network_udp")
    window.transport_mode.setCurrentIndex(index)
    assert window.port.currentText() == "udp://0.0.0.0:42042"
    assert window.endpoint_label.text() == "UDP bind"
    assert not window.baud.isEnabled()
    assert not window.capture_button.isEnabled()

    editor = window.frame_editors[main_window_module.Dataset.A5]
    response = window.response_sets[main_window_module.Dataset.A5].selected_response()
    window._protocol_frame_observed(
        "MAIN_TO_DISPLAY", "A5", response, 123.5
    )
    assert editor._live_response == response
    window.close()


def test_homeserver_transport_is_passive_and_uses_tls_state_endpoint(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    index = window.transport_mode.findData("network_homeserver")
    window.transport_mode.setCurrentIndex(index)

    assert window.port.currentText() == "https://osmofresh-quella.server.home/state"
    assert window.endpoint_label.text() == "Home server URL"
    assert not window.baud.isEnabled()
    assert not window.capture_button.isEnabled()
    assert not window.emulation_button.isVisible()
    window.close()


def test_profile_contains_mode_and_safe_load_never_arms_aa(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    index = window.role_selector.findData(EmulatorRole.DISPLAY.value)
    window.role_selector.setCurrentIndex(index)
    window.display_emulator_tab.allow_aa.setChecked(True)
    window.display_emulator_tab.request_enabled[DisplayRequest.AA].setChecked(True)
    profile = window._profile_data()
    assert profile["emulation_mode"] == EmulatorRole.DISPLAY.value
    assert profile["display_role"]["auto_polling"] is False

    profile_path = tmp_path / "display-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(profile_path), "JSON (*.json)"),
    )
    window.load_profile()
    assert window.emulator_role is EmulatorRole.DISPLAY
    assert not window.display_emulator_tab.allow_aa.isChecked()
    assert not window.display_emulator_tab.request_enabled[DisplayRequest.AA].isChecked()
    window.close()


def test_mega_bridge_is_default_transport_and_profile_keeps_separate_bauds(
    tmp_path, monkeypatch
):
    window = make_window(tmp_path, monkeypatch)
    window.baud.setCurrentText("115200")
    window.quella_baud.setCurrentText("19200")
    window.mega_bridge_tab.mode.setCurrentIndex(
        window.mega_bridge_tab.mode.findData(BridgeMode.DISPLAY_ONLY_TEST)
    )

    profile = window._profile_data()

    assert window.transport_mode.currentData() == "mega_bridge"
    assert profile["serial"]["baud"] == 115200
    assert profile["serial"]["quella_baud"] == 19200
    assert profile["mega_bridge"]["mode"] == "DISPLAY_ONLY_TEST"
    window.close()


def test_mega_bridge_manual_injection_preserves_direction(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab
    captured = []
    tab.inject_requested.disconnect(window._mega_inject_requested)
    tab.inject_requested.connect(lambda direction, raw: captured.append((direction, raw)))
    tab.direction.setCurrentIndex(
        tab.direction.findData(BridgeDirection.MAIN_TO_DISPLAY)
    )
    tab.hex_input.setText("CC")

    tab._inject()

    assert captured == [(BridgeDirection.MAIN_TO_DISPLAY, b"\xCC")]
    window.close()


def test_mega_bridge_rule_ui_builds_exact_known_dispense_rule(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab
    captured = []
    tab.set_rule_requested.disconnect(window._mega_set_rule_requested)
    tab.set_rule_requested.connect(captured.append)
    tab.override_value.setValue(300)
    tab.rule_action.setCurrentIndex(tab.rule_action.findData(FrameAction.REPLACE))

    tab._set_rule()

    assert len(captured) == 1
    assert captured[0].match_payload == bytes.fromhex(
        "00 01 19 00 96 00 00 00 00 00 00 00 00 00 00 00"
    )
    assert captured[0].override_value == 300
    window.close()


def test_mega_bridge_master_checkbox_emits_enable_disable(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab
    commands = []
    tab.rule_command_requested.disconnect(window._mega_rule_command_requested)
    tab.rule_command_requested.connect(commands.append)

    tab.manipulation_enabled.setChecked(True)
    tab.manipulation_enabled.setChecked(False)

    assert commands == [MegaMessageType.ENABLE_RULE, MegaMessageType.DISABLE_RULE]
    window.close()


def test_mega_bridge_displays_completed_replace_tx_diagnostic(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab

    tab.append_event(
        {
            "event": "REPLACE_BUILD",
            "original": "AA 00 01 19 00 96",
            "replacement": "AA 00 01 19 00 97",
            "replacement_len": 19,
            "replacement_checksum": "00 B1",
        }
    )
    tab.append_event(
        {
            "event": "REPLACE_TX_END",
            "written": 19,
            "length": 19,
            "duration_us": 19800,
        }
    )

    assert tab.last_replacement.text() == "AA 00 01 19 00 97"
    assert "19/19" in tab.rule_status.text()
    assert "complete" in tab.rule_status.text()
    window.close()


def test_mega_bridge_send_dispense_emits_semantic_volume_not_raw_bytes(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab
    requested = []
    tab.manual_dispense_requested.disconnect(window._mega_manual_dispense_requested)
    tab.manual_dispense_requested.connect(lambda volume, speed: requested.append((volume, speed)))
    tab.set_connected(True)
    tab.manual_dispense_volume.setValue(10)
    tab.manual_feed_pump_parameter.setValue(1200)
    tab.capture_duration.setValue(25)

    tab.send_dispense.click()

    assert requested == [(10, 1200)]
    assert tab.sequence_capture.active
    assert tab.sequence_capture.scenario == "C"
    assert tab.sequence_capture.duration_us == 25_000_000
    window.close()


def test_mega_bridge_allows_1500_ml_and_adjustable_feed_pump_parameter(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab
    tab.set_connected(True)

    assert tab.manual_dispense_volume.maximum() == 1500
    assert tab.manual_feed_pump_parameter.minimum() == 100
    assert tab.manual_feed_pump_parameter.maximum() == 1200
    window.close()


def test_mega_bridge_manual_command_events_show_tx_and_followup(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab

    tab.append_event({"event": "MANUAL_CMD_QUEUED", "volume_ml": 10})
    assert "QUEUED" in tab.manual_command_status.text()
    tab.append_event(
        {
            "event": "MANUAL_CMD_TX_BEGIN",
            "direction": "DISPLAY_TO_MAIN",
            "timestamp_us": 100_000,
            "decoded_type": "AA",
            "checksum_valid": True,
            "traffic_source": "MEGA_LOCAL_MANUAL_TX",
            "raw": "AA 00 01 19 00 0A 00 00 00 00 00 00 00 00 00 00 00 00 24",
            "target_uart": "Serial2 / TX pin 16",
            "length": 19,
        }
    )
    tab.append_event(
        {"event": "MANUAL_CMD_TX_END", "written": 19, "length": 19, "duration_us": 19800}
    )
    assert "19/19" in tab.manual_command_status.text()
    tab.append_event(
        {"event": "MANUAL_FOLLOWUP_DISPLAY", "detail": "DISPLAY_TO_MAIN 42.0 ms after manual DISPENSE"}
    )
    assert "42.0 ms" in tab.manual_command_status.text()
    window.close()


def test_mega_bridge_auto_starts_sequence_capture_on_valid_display_dispense(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab
    frame = "AA 00 01 19 00 96 00 00 00 00 00 00 00 00 00 00 00 00 B0"

    tab.append_event(
        {
            "event": "RX_FRAME_DISPLAY",
            "direction": "DISPLAY_TO_MAIN",
            "timestamp_us": 123_000,
            "decoded_type": "AA",
            "checksum_valid": True,
            "raw": frame,
        }
    )

    assert tab.sequence_capture.active
    assert tab.sequence_capture.frame_count == 1
    assert tab.sequence_capture.records[1]["raw"] == frame
    assert "frames=1" in tab.capture_status.text()
    window.close()


def test_replace_capture_keeps_original_and_transmitted_replacement(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab
    tab.capture_scenario.setCurrentIndex(tab.capture_scenario.findData("D"))
    original = "AA 00 01 19 00 96 00 00 00 00 00 00 00 00 00 00 00 00 B0"
    replacement = "AA 00 01 19 00 0A 00 00 00 00 00 00 00 00 00 00 00 00 24"

    tab.append_event({"event": "REPLACE_BUILD", "original": original})
    tab.append_event(
        {
            "event": "REPLACE_TX_BEGIN",
            "direction": "DISPLAY_TO_MAIN",
            "timestamp_us": 500_000,
            "decoded_type": "AA",
            "checksum_valid": True,
            "traffic_source": "MEGA_LOCAL_REPLACE_TX",
            "raw": replacement,
        }
    )

    frames = [r for r in tab.sequence_capture.records if r.get("record_type") == "frame"]
    assert [frame["traffic_source"] for frame in frames] == [
        "MEGA_RULE_ORIGINAL", "MEGA_LOCAL_REPLACE_TX"
    ]
    assert frames[0]["raw"] == original
    assert frames[1]["raw"] == replacement
    window.close()


def test_dispense_override_runtime_status_is_visible(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab

    tab.append_event(
        {
            "event": "DISPENSE_PROGRESS",
            "override_active": True,
            "session_origin": "DISPLAY_OVERRIDE",
            "aborting": False,
            "target_ml": 10,
            "original_display_ml": 150,
            "refresh_count": 2,
            "blocked_abort_count": 1,
            "current_a5_ml": 8,
            "main_dispensing": True,
            "elapsed_us": 812_000,
        }
    )

    assert tab.override_active.text() == "YES"
    assert tab.override_origin.text() == "DISPLAY_OVERRIDE"
    assert tab.override_target.text() == "10 ml"
    assert tab.override_original.text() == "150 ml"
    assert tab.override_refreshes.text() == "2"
    assert tab.override_blocked_aborts.text() == "1"
    assert tab.override_progress.text() == "8 ml"
    assert tab.override_main_dispensing.text() == "YES"
    assert tab.override_elapsed.text() == "0.812 s"
    window.close()


def test_manual_remote_buttons_follow_active_aborting_and_end_states(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab
    stopped = []
    tab.stop_remote_dispense_requested.disconnect(window._mega_stop_remote_dispense_requested)
    tab.stop_remote_dispense_requested.connect(lambda: stopped.append(True))
    tab.set_connected(True)
    common = {
        "session_origin": "MANUAL/REMOTE",
        "target_ml": 50,
        "original_display_ml": 150,
        "refresh_count": 1,
        "blocked_abort_count": 2,
        "current_a5_ml": 12,
        "main_dispensing": True,
        "elapsed_us": 900_000,
    }

    tab.append_event({"event": "REMOTE_DISPENSE_START", "override_active": True,
                      "aborting": False, **common})
    assert tab.remote_session_status.text() == "ACTIVE"
    assert not tab.send_dispense.isEnabled()
    assert tab.stop_remote_dispense.isEnabled()

    tab.stop_remote_dispense.click()
    assert stopped == [True]
    assert tab.remote_session_status.text() == "ABORTING"
    assert not tab.send_dispense.isEnabled() and not tab.stop_remote_dispense.isEnabled()

    tab.append_event({"event": "REMOTE_DISPENSE_ABORT_REQUESTED", "override_active": False,
                      "aborting": True, **common})
    assert tab.remote_session_status.text() == "ABORTING"
    tab.append_event({"event": "REMOTE_DISPENSE_END", "override_active": False,
                      "aborting": False, **common})
    assert tab.remote_session_status.text() == "IDLE"
    assert tab.send_dispense.isEnabled() and not tab.stop_remote_dispense.isEnabled()
    window.close()


def test_mega_tank_status_and_config_are_visible(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    tab = window.mega_bridge_tab
    requested = []
    tab.tank_config_requested.disconnect(window._mega_tank_config_requested)
    tab.tank_config_requested.connect(requested.append)
    tab.set_connected(True)
    tab.external_delay_enabled.setChecked(True)
    tab.external_delay_ms.setValue(10000)
    tab.apply_tank_config.click()

    assert requested[0].external_water_delay_enabled is True
    assert requested[0].external_water_delay_ms == 10000

    tab.append_event({
        "event": "EXTERNAL_AVAILABLE_PENDING",
        "internal_min_known": True,
        "internal_min_active": True,
        "internal_max_known": True,
        "internal_max_active": False,
        "external_raw_known": True,
        "external_raw_available": True,
        "external_stable_known": True,
        "external_stable_available": False,
        "fill_allowed": False,
        "fill_active_known": True,
        "fill_active": False,
        "delay_enabled": True,
        "delay_ms": 10000,
        "delay_remaining_ms": 7425,
        "last_update_age_ms": 25,
        "raw": "00 00 00 00 28 00 00 00 00 00 00 00 00 00 00 B2 00 DA",
    })

    assert tab.tank_internal_min.text() == "ACTIVE"
    assert tab.tank_internal_max.text() == "FULL / MAX REACHED"
    assert tab.tank_external_low.text() == "INACTIVE"
    assert tab.tank_external_raw.text() == "AVAILABLE"
    assert tab.tank_external_stable.text() == "BLOCKED"
    assert tab.tank_delay_status.text() == "YES | 10000 ms | remaining 7425 ms"
    assert tab.tank_fill_allowed.text() == "NO"
    assert tab.tank_fill_active.text() == "INACTIVE"
    assert tab.tank_last_age.text() == "25 ms"
    window.close()


def test_live_status_tab_renders_state_without_manual_refresh(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    control = bytearray(16)
    control[1], control[2], control[3:5] = 0x01, 60, (150).to_bytes(2, "big")
    aa_raw = b"\xAA" + bytes(control) + (sum(control) & 0xFFFF).to_bytes(2, "big")
    window.live_state.observe_frame("DISPLAY_TO_MAIN", "AA", aa_raw, __import__("time").time())
    payload = bytearray(16)
    payload[1], payload[2], payload[3:5], payload[12], payload[14] = 1, 24, (30).to_bytes(2, "big"), 19, 245
    raw = bytes(payload) + (sum(payload) & 0xFFFF).to_bytes(2, "big")

    window.live_state.observe_frame("MAIN_TO_DISPLAY", "A5", raw, __import__("time").time())
    window.live_status_tab.refresh()

    assert "24 °C" in window.live_status_tab.value_labels["outlet_c"].text()
    assert "19 °C" in window.live_status_tab.value_labels["inlet_c"].text()
    assert "245 V" in window.live_status_tab.value_labels["ac_mains_v"].text()
    assert window.live_status_tab.value_labels["tds"].text() == "—"
    assert window.tabs.indexOf(window.live_status_tab) >= 0
    window.close()


def test_display_mode_frame_editor_shows_exactly_three_aa_classes(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    index = window.role_selector.findData(EmulatorRole.DISPLAY.value)
    window.role_selector.setCurrentIndex(index)

    assert all(
        not window.frame_editor_tabs.isTabVisible(tab_index)
        for tab_index in window.main_frame_editor_indices
    )
    assert all(
        window.frame_editor_tabs.isTabVisible(tab_index)
        for tab_index in window.aa_frame_editor_indices
    )
    assert [
        window.frame_editor_tabs.tabText(tab_index)
        for tab_index in window.aa_frame_editor_indices
    ] == ["AA — Data[15]=00", "AA — Data[15]=A3", "AA — Data[15]=E1"]
    window.close()


def test_aa_frame_editor_updates_matching_scheduler_payload(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = make_window(tmp_path, monkeypatch)
    editor = window.aa_frame_editors["system_process_a3"]

    editor.table.item(0, 2).setText("12")
    app.processEvents()

    preset = next(
        item
        for item in window.display_emulator_tab.config().aa_presets
        if item.knowledge_class == "system_process_a3"
    )
    assert preset.payload[0] == 0x12
    assert preset.payload[15] == 0xA3
    assert validate_aa_frame(editor.frame())
    window.close()


def test_aa_frame_editor_can_send_a_deliberately_invalid_checksum(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = make_window(tmp_path, monkeypatch)
    editor = window.aa_frame_editors["system_process_a3"]

    editor.auto_checksum.setChecked(False)
    editor.table.item(16, 2).setText("DE")
    editor.table.item(17, 2).setText("AD")
    app.processEvents()

    preset = next(
        item
        for item in window.display_emulator_tab.config().aa_presets
        if item.knowledge_class == "system_process_a3"
    )
    assert preset.auto_checksum is False
    assert preset.checksum() == (0xDE, 0xAD)
    assert editor.frame()[-2:] == b"\xDE\xAD"
    assert not validate_aa_frame(editor.frame())
    assert "INVALID" in editor.checksum_status.text()

    editor.auto_checksum.setChecked(True)
    app.processEvents()
    assert validate_aa_frame(editor.frame())
    window.close()


def test_send_selected_aa_button_emits_selected_row_not_generic_aa(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    tab = DisplayEmulatorTab(tmp_path / "aa_command_presets.json")
    selected_rows = []
    generic_requests = []
    tab.send_aa_preset_requested.connect(selected_rows.append)
    tab.send_requested.connect(generic_requests.append)
    tab.set_connected(True)
    tab.aa_table.selectRow(2)

    tab.send_selected_aa.click()

    assert selected_rows == [2]
    assert generic_requests == []
    tab.close()


def test_aa_preset_description_is_saved_automatically(tmp_path):
    QApplication.instance() or QApplication([])
    path = tmp_path / "aa_command_presets.json"
    tab = DisplayEmulatorTab(path)

    tab.aa_table.item(0, 1).setText("Pump prime")
    tab.aa_table.item(0, 2).setText("Prime feed pump once before dispensing")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["aa_commands"][0]["name"] == "Pump prime"
    assert saved["aa_commands"][0]["description"] == (
        "Prime feed pump once before dispensing"
    )
    tab.close()


def test_aa_editor_number_key_starts_cell_edit_without_moving_row(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = make_window(tmp_path, monkeypatch)
    window.show()
    app.processEvents()
    editor = window.aa_frame_editors["system_process_a3"]
    editor.table.setCurrentCell(0, 2)
    editor.table.setFocus()

    QTest.keyClick(editor.table, Qt.Key.Key_0, Qt.KeyboardModifier.KeypadModifier)
    assert editor.table.currentRow() == 0
    cell_editor = QApplication.focusWidget()
    assert isinstance(cell_editor, QLineEdit)
    QTest.keyClick(cell_editor, Qt.Key.Key_1, Qt.KeyboardModifier.KeypadModifier)
    QTest.keyClick(cell_editor, Qt.Key.Key_Return)
    app.processEvents()

    assert editor.payload[0] == 0x01
    window.close()


def test_each_aa_frame_editor_has_a_send_once_button(tmp_path):
    QApplication.instance() or QApplication([])
    knowledge = ProtocolKnowledge(tmp_path / "protocol_knowledge.json")
    editor = AAFrameEditor("system_process_e1", bytes(15) + b"\xE1", knowledge)
    requested = []
    editor.send_once_requested.connect(requested.append)
    editor.set_send_enabled(True)

    editor.send_once_button.click()

    assert requested == ["system_process_e1"]
    editor.close()


def test_auto_aa_ui_round_trip_and_explicit_apply_from_frame(tmp_path):
    QApplication.instance() or QApplication([])
    tab = DisplayEmulatorTab(tmp_path / "aa_command_presets.json")
    tab.enable_auto_aa.setChecked(True)
    tab.auto_aa_mode.setCurrentText(AutoAAMode.AUTO_MANUAL_INJECT.value)
    tab.apply_payload_to_auto_state(
        bytes.fromhex("00 01 19 01 2C 00 00 00 00 00 00 00 00 00 00 00")
    )

    config = tab.config()
    assert config.auto_aa_enabled is True
    assert config.auto_aa_mode is AutoAAMode.AUTO_MANUAL_INJECT
    assert config.auto_dispense_command == 0x01
    assert config.auto_temperature == 25
    assert config.auto_volume_ml == 300
    tab.close()


def test_abort_button_stays_available_when_connected_and_emits_request(tmp_path):
    QApplication.instance() or QApplication([])
    tab = DisplayEmulatorTab(tmp_path / "aa_command_presets.json")
    requested = []
    tab.abort_requested.connect(lambda: requested.append(True))
    tab.set_connected(True)

    tab.abort_process.click()

    assert requested == [True]
    tab.close()


def test_display_state_diagnostic_shows_valid_cache_and_pending_queues(tmp_path):
    QApplication.instance() or QApplication([])
    tab = DisplayEmulatorTab(tmp_path / "aa_command_presets.json")
    tab.update_display_state(
        {
            "main_state": {
                "A5": {"valid": True, "age_ms": 12, "raw_payload": "00 " * 15 + "00"},
                "E5 B2": {"valid": False, "age_ms": None, "raw_payload": ""},
                "E5 E1": {"valid": False, "age_ms": None, "raw_payload": ""},
            },
            "auto_aa": {
                "enabled": True,
                "mode": AutoAAMode.AUTO_MANUAL_INJECT.value,
                "aa00_state": {
                    "dispense_command": "0x80",
                    "temperature": 25,
                    "volume_ml": 150,
                },
                "a3_mode": "0x68",
                "a3_phase": "0x00",
                "e1_flow_parameter": 750,
                "pending_manual_aa": 1,
                "pending_safety_action": 0,
                "last_decision_reason": "periodic display state",
            },
        }
    )

    assert tab.main_state_table.item(0, 1).text() == "YES"
    assert "Manual queued: 1" in tab.auto_state_summary.text()
    assert "periodic display state" in tab.auto_state_summary.text()
    tab.close()


def test_live_value_panel_decodes_confirmed_fields_with_units(tmp_path):
    QApplication.instance() or QApplication([])
    tab = DisplayEmulatorTab(tmp_path / "aa_command_presets.json")
    payload = bytearray(16)
    payload[2] = 25
    payload[3:5] = (150).to_bytes(2, "big")
    payload[12] = 18
    payload[14] = 249
    tab.update_display_state(
        {
            "main_state": {
                "A5": {
                    "valid": True,
                    "age_ms": 42,
                    "raw_payload": bytes(payload).hex(" ").upper(),
                },
                "E5 B2": {"valid": False, "age_ms": None, "raw_payload": ""},
                "E5 E1": {"valid": False, "age_ms": None, "raw_payload": ""},
                "decoded": {},
            },
            "auto_aa": {},
        }
    )

    assert tab.live_value_table.item(0, 3).text() == "25"
    assert tab.live_value_table.cellWidget(0, 2).text() == "°C"
    assert tab.live_value_table.item(1, 3).text() == "18"
    assert tab.live_value_table.item(2, 3).text() == "249"
    assert tab.live_value_table.item(3, 3).text() == "150"
    assert tab.live_value_table.item(3, 4).text() == "00 96"
    assert tab.live_value_table.item(3, 5).text() == "42 ms"
    tab.close()


def test_live_value_field_format_and_unit_are_profile_configurable(tmp_path):
    QApplication.instance() or QApplication([])
    tab = DisplayEmulatorTab(tmp_path / "aa_command_presets.json")
    field_box = tab.live_value_table.cellWidget(0, 0)
    field_box.setCurrentIndex(field_box.findData("b2_internal_low_sensor"))
    tab.live_value_table.cellWidget(0, 1).setCurrentText("Boolean")
    tab.live_value_table.cellWidget(0, 2).setText("active")

    config = tab.config()
    restored = type(config).from_dict(config.to_dict())

    assert restored.live_value_fields[0] == {
        "field": "b2_internal_low_sensor",
        "format": "Boolean",
        "unit": "active",
    }
    tab.close()


def test_frame_editor_one_click_request_buttons_send_expected_types(tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    selected = []
    monkeypatch.setattr(window, "_send_display_request", selected.append)
    for button in window.display_request_once_buttons:
        button.setEnabled(True)
        button.click()

    assert selected == ["A5", "E5 B2", "E5 E1"]
    window.close()


def test_display_aa_editor_persists_category_meaning_and_confidence(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = make_window(tmp_path, monkeypatch)
    window.role_selector.setCurrentIndex(
        window.role_selector.findData(EmulatorRole.DISPLAY.value)
    )
    editor = window.aa_frame_editors["system_process_e1"]

    assert editor.table.item(6, 5).flags() & Qt.ItemFlag.ItemIsEditable
    editor.category_boxes[6].setCurrentText("VALUE")
    editor.table.item(6, 5).setText("E1-specific measured value")
    editor.confidence_boxes[6].setCurrentText("HIGH")
    app.processEvents()

    saved = ProtocolKnowledge(tmp_path / "protocol_knowledge.json")
    field = saved.field("AA", 6, "system_process_e1")
    assert field["category"] == "VALUE"
    assert field["meaning"] == "E1-specific measured value"
    assert field["confidence"] == "HIGH"
    assert saved.field("AA", 6, "system_process_a3")["meaning"] == "UNKNOWN"
    assert "saved" in editor.save_status.text().lower()
    window.close()
