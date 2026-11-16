import pytest

from protocol.checksum import sum16_bytes
from protocol.display_role import (
    AAPayloadPreset,
    AutoAAMode,
    DisplayRequest,
    DisplayRequestScheduler,
    DisplayRoleConfig,
    DisplayRoleController,
    EmulatorRole,
    build_aa_frame,
    direction_labels,
    load_aa_command_presets,
    save_aa_command_presets,
)
from protocol.emulator import EmulatorCore
from protocol.parser import StreamParser


def response(payload: bytes) -> bytes:
    return payload + bytes(sum16_bytes(payload))


def test_direction_labels_are_role_correct():
    assert direction_labels(EmulatorRole.MAIN_MCU) == (
        "DISPLAY -> EMU",
        "EMU -> DISPLAY",
    )
    assert direction_labels(EmulatorRole.DISPLAY) == (
        "MAIN -> EMU(DISPLAY)",
        "EMU(DISPLAY) -> MAIN",
    )


def test_scheduler_start_stop_and_manual_step():
    config = DisplayRoleConfig()
    scheduler = DisplayRequestScheduler(config)
    assert scheduler.due(0.0, False) is None
    scheduler.start(1.0)
    assert scheduler.due(0.9, False) is None
    assert scheduler.due(1.0, False) is DisplayRequest.A5
    scheduler.sent(DisplayRequest.A5, 1.0)
    assert scheduler.due(1.079, False) is None
    scheduler.stop()
    assert scheduler.due(2.0, False) is None
    assert scheduler.step(False) is DisplayRequest.E5_B2


def test_display_controller_sends_each_known_request():
    controller = DisplayRoleController()
    expected = {
        DisplayRequest.A5: b"\xA5",
        DisplayRequest.E5_B2: b"\xE5\xB2",
        DisplayRequest.E5_E1: b"\xE5\xE1",
    }
    for index, (request, raw) in enumerate(expected.items()):
        event = controller.send_request(request, float(index))
        assert event.event == "tx"
        assert event.raw == raw
        controller.receive(response(bytes([index]) * 16), float(index) + 0.001)


def test_fragmented_18_byte_response_and_sum16_validation():
    controller = DisplayRoleController()
    controller.send_request(DisplayRequest.A5, 10.0)
    raw = response(bytes(range(16)))
    assert controller.receive(raw[:7], 10.001) == []
    events = controller.receive(raw[7:], 10.004)
    assert len(events) == 1
    assert events[0].raw == raw
    assert events[0].status == "VALID SUM16"
    assert events[0].checksum_valid is True

    controller.send_request(DisplayRequest.E5_B2, 11.0)
    invalid = raw[:-1] + bytes([raw[-1] ^ 1])
    event = controller.receive(invalid, 11.002)[0]
    assert event.status == "INVALID SUM16"
    assert event.checksum_valid is False


def test_aa_generation_excludes_header_and_ack_is_transaction_aware():
    payload = bytes(range(16))
    raw = build_aa_frame(payload)
    assert raw == b"\xAA" + payload + bytes(sum16_bytes(payload))

    config = DisplayRoleConfig()
    config.allow_aa_control = True
    config.aa_presets[0].payload = payload
    controller = DisplayRoleController(config)
    tx = controller.send_request(DisplayRequest.AA, 1.0)
    assert tx.raw == raw
    ack = controller.receive(b"\xCC", 1.003)[0]
    assert ack.status == "ACK RECEIVED"
    assert ack.latency_ms == pytest.approx(3.0)


def test_aa_manual_checksum_is_transmitted_exactly_and_persists():
    payload = bytes(range(16))
    preset = AAPayloadPreset(
        "Invalid checksum test",
        payload,
        True,
        "control_setpoint_00",
        False,
        0xDE,
        0xAD,
    )
    config = DisplayRoleConfig(aa_presets=[preset], allow_aa_control=True)
    event = DisplayRoleController(config).send_request(DisplayRequest.AA, 1.0)
    assert event.raw == b"\xAA" + payload + b"\xDE\xAD"

    restored = DisplayRoleConfig.from_dict(config.to_dict())
    restored_preset = restored.aa_presets[0]
    assert restored_preset.auto_checksum is False
    assert restored_preset.checksum() == (0xDE, 0xAD)
    assert restored_preset.frame() == event.raw


def test_selected_aa_is_sent_once_without_enabling_scheduler_entry():
    first = AAPayloadPreset("First", bytes(16), True)
    selected = AAPayloadPreset(
        "Selected invalid test", bytes([0x22]) * 16, False, None, False, 0xDE, 0xAD
    )
    config = DisplayRoleConfig(
        aa_presets=[first, selected],
        aa_round_robin=True,
        allow_aa_control=True,
    )
    config.requests[DisplayRequest.AA]["enabled"] = False
    controller = DisplayRoleController(config)

    event = controller.send_aa_preset(1, 1.0)

    assert event.status == "SENT SELECTED ONCE"
    assert event.raw == b"\xAA" + selected.payload + b"\xDE\xAD"
    assert controller.receive(b"\xCC", 1.001)[0].status == "ACK RECEIVED"
    assert controller.tick(2.0) == []


def test_aa_command_preset_library_round_trip_keeps_description(tmp_path):
    path = tmp_path / "aa_command_presets.json"
    preset = AAPayloadPreset(
        "Flush command",
        bytes([1]) + bytes(15),
        True,
        "control_setpoint_00",
        False,
        0x12,
        0x34,
        "Briefly opens the flushing valve for checksum experiments",
    )

    save_aa_command_presets([preset], path)
    restored = load_aa_command_presets(path)

    assert len(restored) == 1
    assert restored[0].name == "Flush command"
    assert restored[0].description.startswith("Briefly opens")
    assert restored[0].auto_checksum is False
    assert restored[0].checksum() == (0x12, 0x34)


def test_main_state_cache_updates_only_after_valid_complete_sum16():
    controller = DisplayRoleController()
    payload = bytearray(16)
    payload[2] = 25
    payload[3:5] = (321).to_bytes(2, "big")
    valid = response(payload)

    controller.send_request(DisplayRequest.A5, 1.0)
    controller.receive(valid[:-1] + bytes([valid[-1] ^ 1]), 1.001)
    assert controller.main_state.raw_a5 is None
    assert controller.main_state.a5_valid is False

    controller.send_request(DisplayRequest.A5, 2.0)
    controller.receive(valid, 2.001)
    assert controller.main_state.raw_a5 == bytes(payload)
    assert controller.main_state.current_dispense_volume == 321
    assert controller.main_state.outlet_temperature == 25

    b2 = bytearray(16)
    b2[4] = 0x29
    controller.send_request(DisplayRequest.E5_B2, 3.0)
    controller.receive(response(b2), 3.001)
    assert controller.main_state.external_tank_available is True
    assert controller.main_state.internal_high_sensor is True
    assert controller.main_state.internal_low_sensor is True


def test_below_low_polarity_and_a5_fallback_represent_same_physical_state():
    controller = DisplayRoleController()
    a5 = bytearray(16)
    a5[11] = 0x01
    controller.send_request(DisplayRequest.A5, 1.0)
    controller.receive(response(a5), 1.001)
    assert controller.main_state.a5_internal_below_low is True
    assert controller.main_state.internal_below_low is True

    b2 = bytearray(16)
    b2[4] = 0x00
    controller.send_request(DisplayRequest.E5_B2, 2.0)
    controller.receive(response(b2), 2.001)
    assert controller.main_state.internal_low_sensor is False
    assert controller.main_state.internal_below_low is False


def test_low_sensor_is_input_only_and_does_not_start_or_abort_filter():
    config = DisplayRoleConfig(
        allow_aa_control=True,
        auto_aa_enabled=True,
        auto_aa_mode=AutoAAMode.AUTO,
        auto_a3_mode=0x78,
        auto_a3_phase=0x02,
    )
    controller = DisplayRoleController(config)
    low_active = bytearray(16)
    low_active[4] = 0x08

    controller.send_request(DisplayRequest.E5_B2, 1.0)
    controller.receive(response(low_active), 1.001)

    assert controller.main_state.internal_low_sensor is True
    assert controller.auto_controller.state.a3_mode == 0x78
    assert controller.auto_controller.state.a3_phase == 0x02
    assert controller.emergency_aa_queue == []
    assert all(
        rule.required_mapping != "E5 B2 Data[4] bit3"
        for rule in controller.auto_controller.safety_rules
    )


def a3_preset(mode: int) -> AAPayloadPreset:
    payload = bytearray(16)
    payload[4] = mode
    payload[5] = 0x02
    payload[15] = 0xA3
    return AAPayloadPreset(f"A3 {mode:02X}", bytes(payload), True)


def feed_b2(controller: DisplayRoleController, d4: int, now: float) -> None:
    payload = bytearray(16)
    payload[4] = d4
    controller.send_request(DisplayRequest.E5_B2, now)
    controller.receive(response(payload), now + 0.001)


def start_manual_process(
    controller: DisplayRoleController, now: float = 2.0
) -> None:
    event = controller.send_aa_preset(0, now)
    assert event.event == "tx"
    controller.receive(b"\xCC", now + 0.001)


def test_filter_with_fresh_safe_b2_remains_active():
    config = DisplayRoleConfig(
        allow_aa_control=True, aa_presets=[a3_preset(0x78)]
    )
    controller = DisplayRoleController(config)
    feed_b2(controller, 0x01, 1.0)

    start_manual_process(controller)

    assert controller.auto_controller.state.process_mode == 0x78
    assert controller.emergency_aa_queue == []


def test_filter_high_level_queues_confirmed_base_abort():
    config = DisplayRoleConfig(
        allow_aa_control=True, aa_presets=[a3_preset(0x78)]
    )
    controller = DisplayRoleController(config)
    feed_b2(controller, 0x01, 1.0)
    start_manual_process(controller)

    feed_b2(controller, 0x21, 3.0)
    abort = controller.tick(3.002)[0]

    assert abort.status == "SAFETY FILTER STOP -> SENT"
    assert abort.reason == "Internal Tank HIGH"
    assert abort.raw == build_aa_frame(
        bytes.fromhex("00 00 00 00 68 02 00 00 00 00 00 00 00 00 00 A3")
    )
    assert controller.auto_controller.state.process_mode == 0x78
    controller.receive(b"\xCC", 3.003)
    assert controller.auto_controller.state.process_mode == 0x68


def test_automatic_filter_start_is_inhibited_when_high_is_already_active():
    config = DisplayRoleConfig(
        allow_aa_control=True,
        auto_aa_enabled=True,
        auto_aa_mode=AutoAAMode.AUTO,
        auto_a3_mode=0x78,
        auto_a3_phase=0x02,
    )
    controller = DisplayRoleController(config)
    feed_b2(controller, 0x21, 1.0)
    controller.start(2.0)
    controller.auto_controller.phase = 5
    controller.auto_controller.next_due = 2.0

    event = controller.tick(2.0)[0]

    assert event.status == "AUTO FILTER START INHIBITED"
    assert event.reason == "Internal Tank HIGH"
    assert controller.pending is None
    assert controller.auto_controller.state.process_mode == 0x68


def test_external_empty_aborts_active_filter():
    config = DisplayRoleConfig(
        allow_aa_control=True, aa_presets=[a3_preset(0x78)]
    )
    controller = DisplayRoleController(config)
    feed_b2(controller, 0x01, 1.0)
    start_manual_process(controller)

    feed_b2(controller, 0x00, 3.0)

    assert len(controller.emergency_aa_queue) == 1
    assert controller.emergency_aa_queue[0].reason == "External tank empty"


def test_low_sensor_changes_never_abort_active_filter():
    config = DisplayRoleConfig(
        allow_aa_control=True, aa_presets=[a3_preset(0x78)]
    )
    controller = DisplayRoleController(config)
    feed_b2(controller, 0x01, 1.0)
    start_manual_process(controller)

    feed_b2(controller, 0x09, 3.0)
    feed_b2(controller, 0x01, 4.0)

    assert controller.auto_controller.state.process_mode == 0x78
    assert controller.emergency_aa_queue == []


def test_manual_filter_injection_is_sent_once_then_safety_abort_is_prioritized():
    config = DisplayRoleConfig(
        allow_aa_control=True, aa_presets=[a3_preset(0x78)]
    )
    controller = DisplayRoleController(config)
    feed_b2(controller, 0x21, 1.0)

    manual = controller.send_aa_preset(0, 2.0)

    assert manual.status == "SENT SELECTED ONCE"
    assert controller.auto_controller.state.process_mode == 0x78
    assert len(controller.emergency_aa_queue) == 1
    controller.receive(b"\xCC", 2.001)
    assert controller.tick(2.002)[0].status == "SAFETY FILTER STOP -> SENT"


def test_stale_or_missing_b2_prevents_automatic_filter_start():
    config = DisplayRoleConfig(
        allow_aa_control=True,
        auto_aa_enabled=True,
        auto_aa_mode=AutoAAMode.AUTO,
        auto_a3_mode=0x78,
    )
    controller = DisplayRoleController(config)
    controller.start(10.0)
    controller.auto_controller.phase = 5
    controller.auto_controller.next_due = 10.0

    event = controller.tick(10.0)[0]

    assert event.status == "AUTO FILTER START INHIBITED"
    assert event.reason == "B2 stale or unavailable"
    assert controller.pending is None


def test_explicit_unsafe_override_bypasses_process_safety_only():
    config = DisplayRoleConfig(
        allow_aa_control=True,
        unsafe_process_safety_bypass=True,
        aa_presets=[a3_preset(0x78)],
    )
    controller = DisplayRoleController(config)
    feed_b2(controller, 0x20, 1.0)

    start_manual_process(controller)

    assert controller.auto_controller.state.process_mode == 0x78
    assert controller.emergency_aa_queue == []
    assert DisplayRoleConfig.from_dict(config.to_dict(), safe_load=True).unsafe_process_safety_bypass is False


def test_power_on_below_low_starts_initial_fill_with_a3_d5_zero():
    config = DisplayRoleConfig(allow_aa_control=True)
    controller = DisplayRoleController(config)
    controller.start(1.0)

    feed_b2(controller, 0x09, 1.01)

    state = controller.auto_controller.state
    assert state.initial_fill_active is True
    assert state.dispense_allowed is False
    event = controller.tick(1.012)[0]
    assert event.status == "INITIAL FILL FILTER -> SENT"
    assert event.raw == build_aa_frame(
        bytes.fromhex("00 00 00 00 78 00 00 00 00 00 00 00 00 00 00 A3")
    )
    assert controller.auto_controller.state.process_mode == 0x78


def test_crossing_above_low_unlocks_dispense_without_stopping_filter():
    config = DisplayRoleConfig(allow_aa_control=True)
    controller = DisplayRoleController(config)
    controller.start(1.0)
    feed_b2(controller, 0x09, 1.01)
    controller.tick(1.012)
    controller.receive(b"\xCC", 1.013)

    feed_b2(controller, 0x01, 1.02)

    state = controller.auto_controller.state
    assert state.initial_fill_active is True
    assert state.dispense_allowed is True
    assert state.process_mode == 0x78
    assert controller.emergency_aa_queue == []


def test_initial_fill_rejects_manual_and_automatic_aa00_dispense():
    dispense = bytearray(16)
    dispense[1] = 0x01
    config = DisplayRoleConfig(
        allow_aa_control=True,
        auto_aa_enabled=True,
        auto_aa_mode=AutoAAMode.AUTO,
        auto_dispense_command=0x01,
        aa_presets=[AAPayloadPreset("Dispense", bytes(dispense), True)],
    )
    controller = DisplayRoleController(config)
    controller.start(1.0)
    feed_b2(controller, 0x09, 1.01)
    controller.tick(1.012)
    controller.receive(b"\xCC", 1.013)

    manual = controller.send_aa_preset(0, 1.014)
    assert manual.status == "DISPENSE INHIBITED — INITIAL FILL"
    assert controller.pending is None

    controller.auto_controller.phase = 0
    controller.auto_controller.next_due = 1.015
    automatic = controller.tick(1.015)[0]
    assert automatic.status == "DISPENSE INHIBITED — INITIAL FILL"
    assert controller.pending is None


def test_auto_aa_uses_real_display_cadence_and_never_overlaps_transactions():
    config = DisplayRoleConfig(
        allow_aa_control=True,
        auto_aa_enabled=True,
        auto_aa_mode=AutoAAMode.AUTO,
    )
    controller = DisplayRoleController(config)
    controller.start(0.0)

    aa00 = controller.tick(0.0)[0]
    assert aa00.status == "AUTO AA00 -> SENT"
    assert aa00.raw[2] == 0x80
    assert aa00.raw[3] == 25
    assert aa00.raw[4:6] == b"\x00\x96"
    assert controller.tick(0.080) == []
    controller.receive(b"\xCC", 0.001)

    poll_a5 = controller.tick(0.080)[0]
    assert poll_a5.raw == b"\xA5"
    controller.receive(response(bytes(16)), 0.081)
    poll_b2 = controller.tick(0.160)[0]
    assert poll_b2.raw == b"\xE5\xB2"
    controller.receive(response(bytes(16)), 0.161)
    poll_e1 = controller.tick(0.240)[0]
    assert poll_e1.raw == b"\xE5\xE1"
    controller.receive(response(bytes(16)), 0.241)
    second_a5 = controller.tick(0.370)[0]
    assert second_a5.raw == b"\xA5"
    controller.receive(response(bytes(16)), 0.371)
    a3 = controller.tick(0.401)[0]
    assert a3.status == "AUTO A3 BASE -> SENT"
    assert a3.raw[5:7] == b"\x68\x00"
    controller.receive(b"\xCC", 0.402)
    next_cycle = controller.tick(0.402)[0]
    assert next_cycle.status == "AUTO AA00 -> SENT"
    assert controller.auto_controller.cycle_number == 1
    tx_stats = controller.statistics()["TX-to-TX"]
    assert tx_stats["maximum_ms"] < 750


def test_tx_arbiter_prioritizes_abort_then_manual_before_auto():
    config = DisplayRoleConfig(
        allow_aa_control=True,
        auto_aa_enabled=True,
        auto_aa_mode=AutoAAMode.AUTO_MANUAL_INJECT,
    )
    controller = DisplayRoleController(config)
    controller.start(0.0)
    controller.queue_manual_aa_preset(0)
    abort_queued = controller.queue_abort_water_process()
    assert abort_queued.status == "SAFETY ABORT -> QUEUED"

    abort_tx = controller.tick(0.0)[0]
    assert abort_tx.status == "SAFETY A3 BASE -> SENT"
    assert abort_tx.raw == build_aa_frame(
        bytes.fromhex("00 00 00 00 68 02 00 00 00 00 00 00 00 00 00 A3")
    )
    controller.receive(b"\xCC", 0.001)
    manual_tx = controller.tick(0.002)[0]
    assert manual_tx.status == "MANUAL AA -> SENT"


def test_manual_override_keeps_polling_but_pauses_automatic_aa():
    config = DisplayRoleConfig(
        allow_aa_control=True,
        auto_aa_enabled=True,
        auto_aa_mode=AutoAAMode.MANUAL_OVERRIDE,
    )
    controller = DisplayRoleController(config)
    controller.start(0.0)
    first = controller.tick(0.0)[0]
    assert first.request is DisplayRequest.A5
    assert first.raw == b"\xA5"
    controller.receive(response(bytes(16)), 0.001)
    second = controller.tick(0.080)[0]
    assert second.request is DisplayRequest.E5_B2


def test_abort_respects_hardware_safety_gate():
    controller = DisplayRoleController(DisplayRoleConfig(allow_aa_control=False))
    event = controller.queue_abort_water_process()
    assert event.event == "blocked"
    assert controller.has_queued_tx is False


def test_aa_safety_blocks_automatic_and_manual_send_by_default():
    config = DisplayRoleConfig()
    for request in DisplayRequest:
        config.requests[request]["enabled"] = request is DisplayRequest.AA
    controller = DisplayRoleController(config)
    assert controller.send_request(DisplayRequest.AA, 1.0).status == "AA SAFETY BLOCKED"
    controller.scheduler.start(1.0)
    # Even an enabled scheduler entry cannot bypass the independent AA lock.
    assert controller.tick(1.0) == []


def test_partial_response_is_reported_as_unexpected_length_at_timeout():
    config = DisplayRoleConfig()
    config.response_timeout_ms = 10
    controller = DisplayRoleController(config)
    controller.send_request(DisplayRequest.E5_E1, 1.0)
    assert controller.receive(b"\x00\x01\x02", 1.001) == []
    event = controller.tick(1.011)[0]
    assert event.status == "UNEXPECTED LENGTH"
    assert event.raw == b"\x00\x01\x02"


def test_ack_timeout_has_no_retry_by_default():
    config = DisplayRoleConfig()
    config.allow_aa_control = True
    config.ack_timeout_ms = 50
    controller = DisplayRoleController(config)
    controller.send_request(DisplayRequest.AA, 1.0)
    events = controller.tick(1.051)
    assert [event.status for event in events] == ["ACK TIMEOUT"]
    assert controller.pending is None
    assert controller.statistics()["AA"]["timeout_count"] == 1


def test_safe_profile_load_forces_outputs_off_but_keeps_scheduler_settings():
    config = DisplayRoleConfig.from_dict(
        {
            "requests": {
                "A5": {"enabled": True, "delay_ms": 44},
                "AA": {"enabled": True, "delay_ms": 9},
            },
            "allow_aa_control": True,
            "auto_polling": True,
        },
        safe_load=True,
    )
    assert config.requests[DisplayRequest.A5] == {"enabled": True, "delay_ms": 44}
    assert config.requests[DisplayRequest.AA]["enabled"] is False
    assert config.allow_aa_control is False
    assert config.to_dict()["auto_polling"] is False


def test_existing_main_mcu_parser_and_emulator_still_accept_requests():
    parser = StreamParser()
    core = EmulatorCore()
    frames = parser.feed(b"\xA5\xE5\xB2\xE5\xE1")
    assert [frame.kind.value for frame in frames] == ["A5", "E5 B2", "E5 E1"]
    for frame in frames:
        core.register_received(frame)
    snapshot = core.snapshot()
    assert snapshot["a5_requests"] == 1
    assert snapshot["e5_b2_requests"] == 1
    assert snapshot["e5_e1_requests"] == 1
