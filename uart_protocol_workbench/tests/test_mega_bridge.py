import struct

from protocol.mega_bridge import (
    BridgeDirection,
    BridgeMode,
    DispenseOverrideEvent,
    DispenseOverrideReference,
    DispenseSessionOrigin,
    ExternalTankState,
    ExternalWaterDelayReference,
    FrameAction,
    IdleInjectionQueue,
    MegaBridgeLink,
    MegaConfig,
    MegaMessage,
    MegaMessageType,
    MegaRule,
    TankConfig,
    TankEvent,
    MegaProtocolParser,
    classify_main_frame,
    apply_rule_reference,
    decode_dispense_override_event,
    decode_dispense_override_status,
    decode_rule_status,
    decode_replace_event,
    decode_manual_command_event,
    decode_rx_payload,
    decode_tank_event,
    decode_tank_status,
    encode_message,
)


DISPENSE = bytes.fromhex("00 01 19 00 96 00 00 00 00 00 00 00 00 00 00 00")


def aa_frame(payload: bytes) -> bytes:
    return b"\xAA" + payload + (sum(payload) & 0xFFFF).to_bytes(2, "big")


def b2_frame(d4: int, d11: int = 0) -> bytes:
    payload = bytearray(16)
    payload[4] = d4
    payload[11] = d11
    payload[15] = 0xB2
    return bytes(payload) + (sum(payload) & 0xFFFF).to_bytes(2, "big")


def test_binary_protocol_round_trip_survives_fragmentation():
    raw = encode_message(
        MegaMessage(MegaMessageType.INJECT_DISPLAY_TO_MAIN, b"\xAA\x01\x02", 42)
    )
    parser = MegaProtocolParser()

    assert parser.feed(b"noise" + raw[:4]) == []
    assert parser.feed(raw[4:9]) == []
    messages = parser.feed(raw[9:])

    assert messages == [
        MegaMessage(MegaMessageType.INJECT_DISPLAY_TO_MAIN, b"\xAA\x01\x02", 42)
    ]


def test_crc_error_resynchronizes_to_next_valid_message():
    broken = bytearray(encode_message(MegaMessage(MegaMessageType.PING, b"bad", 1)))
    broken[-1] ^= 0x80
    valid = encode_message(MegaMessage(MegaMessageType.PING, b"good", 2))
    parser = MegaProtocolParser()

    messages = parser.feed(bytes(broken) + valid)

    assert messages == [MegaMessage(MegaMessageType.PING, b"good", 2)]
    assert parser.crc_errors == 1


def test_config_and_injection_messages_have_distinct_control_envelopes():
    link = MegaBridgeLink()
    config = MegaConfig(BridgeMode.DISPLAY_ONLY_TEST, 19200, 5000, True)
    config_sequence, config_raw = link.set_config(config)
    inject_sequence, inject_raw = link.inject(
        BridgeDirection.MAIN_TO_DISPLAY, b"\x00\x01"
    )
    parser = MegaProtocolParser()
    messages = parser.feed(config_raw + inject_raw)

    assert config_sequence != inject_sequence
    assert MegaConfig.from_payload(messages[0].payload) == config
    assert messages[1].message_type is MegaMessageType.INJECT_MAIN_TO_DISPLAY
    assert messages[1].payload == b"\x00\x01"


def test_tank_config_wire_round_trip_and_status_request():
    link = MegaBridgeLink()
    config = TankConfig(True, 10_000)
    ignored, set_packet = link.set_tank_config(config)
    ignored, get_packet = link.get_tank_status()
    messages = MegaProtocolParser().feed(set_packet + get_packet)

    assert messages[0].message_type is MegaMessageType.SET_TANK_CONFIG
    assert TankConfig.from_payload(messages[0].payload) == config
    assert messages[1].message_type is MegaMessageType.GET_TANK_STATUS


def test_external_empty_latches_immediately_and_available_stays_blocked():
    model = ExternalWaterDelayReference()
    available = b2_frame(0x28, 0x04)  # bit0=0 available; MIN active; MAX inactive
    low = b2_frame(0x29, 0x04)
    passed, events = model.process_b2(low, 1_000)
    assert passed == low
    assert events == [TankEvent.EXTERNAL_TANK_LOW, TankEvent.EXTERNAL_STOP_LATCHED]
    assert model.state is ExternalTankState.LOW
    blocked, events = model.process_b2(available, 1_100)
    assert events == [] and blocked[4] == 0x29
    assert blocked[16:] == (sum(blocked[:16]) & 0xFFFF).to_bytes(2, "big")


def test_external_latch_requires_start_then_idle_abort_acknowledgement():
    model = ExternalWaterDelayReference()
    available, low = b2_frame(0x20), b2_frame(0x21)
    model.process_b2(low, 0)
    model.process_b2(available, 100)
    allowed, events = model.process_control(0x01, idle=True)
    assert not allowed and events == [TankEvent.EXTERNAL_RESET_ARMED]
    allowed, events = model.process_control(0x00, idle=True)
    assert allowed
    assert events == [TankEvent.EXTERNAL_STOP_CLEARED, TankEvent.EXTERNAL_AVAILABLE_STABLE]
    assert not model.stop_latched and model.stable_available


def test_active_process_does_not_arm_external_reset():
    model = ExternalWaterDelayReference()
    model.process_b2(b2_frame(0x21), 0)
    model.process_b2(b2_frame(0x20), 100)
    allowed, events = model.process_control(0x01, idle=False)
    assert not allowed and events == [] and not model.reset_armed


def test_tank_status_and_event_wire_decode():
    raw = b2_frame(0x28, 0x04)
    flags = 0x01 | 0x02 | 0x04 | 0x10 | 0x20 | 0x40
    flags2 = 0x02 | 0x04 | 0x10 | 0x20
    status_payload = struct.pack("<BBBIII18s", 2, flags, flags2, 10_000, 7_500, 25, raw)
    status = decode_tank_status(status_payload)
    event, timestamp, event_status = decode_tank_event(
        bytes((TankEvent.EXTERNAL_AVAILABLE_PENDING,))
        + struct.pack("<I", 123_456)
        + status_payload
    )

    assert status.external_state is ExternalTankState.AVAILABLE_PENDING
    assert status.raw_available and not status.stable_available
    assert status.internal_min_active and not status.internal_max_active
    assert status.fill_active and not status.fill_allowed
    assert status.external_stop_latched and status.external_reset_armed
    assert status.last_frame == raw
    assert event is TankEvent.EXTERNAL_AVAILABLE_PENDING
    assert timestamp == 123_456 and event_status == status


def test_idle_injection_queue_forwards_unknown_bytes_unchanged_and_waits_for_gap():
    model = IdleInjectionQueue(idle_gap_us=4000)
    unknown = b"\xDE\xAD\xBE\xEF"
    assert model.observe(BridgeDirection.DISPLAY_TO_MAIN, unknown, 10_000) == unknown
    model.inject(BridgeDirection.MAIN_TO_DISPLAY, b"\xCC")

    assert model.due(13_999) is None
    assert model.due(14_000) == (BridgeDirection.MAIN_TO_DISPLAY, b"\xCC")


def test_main_frame_classification_and_rx_payload_decode():
    b2 = bytearray(18)
    b2[15] = 0xB2
    payload = (123456).to_bytes(4, "little") + len(b2).to_bytes(2, "little") + b2

    timestamp, raw = decode_rx_payload(payload)

    assert timestamp == 123456
    assert raw == bytes(b2)
    assert classify_main_frame(raw) == "E5 B2"
    assert classify_main_frame(b"\xCC") == "CC"


def test_rule_wire_round_trip_and_control_message_type():
    rule = MegaRule(
        BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 275, FrameAction.REPLACE, True
    )
    link = MegaBridgeLink()
    ignored, packet = link.set_rule(rule)
    message = MegaProtocolParser().feed(packet)[0]

    assert message.message_type is MegaMessageType.SET_RULE
    assert MegaRule.from_payload(message.payload) == rule


def test_exact_dispense_match_replaces_volume_and_recalculates_sum16():
    rule = MegaRule(
        BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 300, FrameAction.REPLACE, True
    )

    action, output, matched = apply_rule_reference(
        rule, BridgeDirection.DISPLAY_TO_MAIN, aa_frame(DISPENSE)
    )

    assert matched and action is FrameAction.REPLACE
    assert output[4:6] == (300).to_bytes(2, "big")
    assert output[17:] == (sum(output[1:17]) & 0xFFFF).to_bytes(2, "big")


def test_disabled_unknown_wrong_direction_and_bad_checksum_are_byte_exact_pass():
    original = aa_frame(DISPENSE)
    cases = [
        (MegaRule(BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 300), BridgeDirection.DISPLAY_TO_MAIN, original),
        (MegaRule(BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 300, enabled=True), BridgeDirection.MAIN_TO_DISPLAY, original),
        (MegaRule(BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 300, enabled=True), BridgeDirection.DISPLAY_TO_MAIN, b"\xDE\xAD"),
        (MegaRule(BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 300, enabled=True), BridgeDirection.DISPLAY_TO_MAIN, original[:-1] + bytes((original[-1] ^ 1,))),
    ]
    for rule, direction, raw in cases:
        action, output, matched = apply_rule_reference(rule, direction, raw)
        assert action is FrameAction.PASS
        assert output == raw
        assert not matched


def test_exact_rule_supports_pass_and_suppress_actions():
    original = aa_frame(DISPENSE)
    for expected, expected_output in (
        (FrameAction.PASS, original), (FrameAction.SUPPRESS, b"")
    ):
        rule = MegaRule(
            BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 150, expected, True
        )
        action, output, matched = apply_rule_reference(
            rule, BridgeDirection.DISPLAY_TO_MAIN, original
        )
        assert matched and action is expected and output == expected_output


def test_display_initiated_override_replaces_start_refresh_and_passes_user_abort():
    rule = MegaRule(
        BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 10, FrameAction.REPLACE, True
    )
    session = DispenseOverrideReference(rule)
    start = aa_frame(DISPENSE)
    refresh_payload = bytearray(DISPENSE)
    refresh_payload[1] = 0x80
    refresh = aa_frame(bytes(refresh_payload))
    abort_payload = bytearray(DISPENSE)
    abort_payload[1] = 0x00
    abort = aa_frame(bytes(abort_payload))

    action, output, event = session.apply_display(start)
    assert (action, event) == (FrameAction.REPLACE, "DISPENSE_OVERRIDE_START")
    assert output[4:6] == b"\x00\x0A" and output[-2:] == b"\x00\x24"

    session.main_dispensing = True
    session.last_progress_ml = 8
    action, output, event = session.apply_display(refresh)
    assert (action, event) == (FrameAction.REPLACE, "DISPENSE_REFRESH_REPLACED")
    assert output[4:6] == b"\x00\x0A" and output[-2:] == b"\x00\xA3"
    assert session.refresh_count == 1

    action, output, event = session.apply_display(abort)
    assert (action, output, event) == (
        FrameAction.PASS, abort, "DISPENSE_USER_ABORT"
    )
    assert not session.active and session.aborting
    assert session.blocked_abort_count == 0

    a5_idle = bytearray(18)
    a5_idle[3:5] = (8).to_bytes(2, "big")
    a5_idle[16:] = (sum(a5_idle[:16]) & 0xFFFF).to_bytes(2, "big")
    assert session.observe_a5(bytes(a5_idle)) == "DISPENSE_OVERRIDE_END"
    assert not session.active and not session.aborting


def test_manual_remote_origin_retains_separately_scoped_early_abort_policy():
    rule = MegaRule(
        BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 10, FrameAction.REPLACE, True
    )
    session = DispenseOverrideReference(rule)
    session.start_remote(10)
    session.main_dispensing = True
    session.last_progress_ml = 8
    abort_payload = bytearray(DISPENSE)
    abort_payload[1] = 0x00

    action, output, event = session.apply_display(aa_frame(bytes(abort_payload)))

    assert (action, output, event) == (
        FrameAction.SUPPRESS, b"", "REMOTE_DISPLAY_IDLE_SUPPRESSED"
    )
    assert session.blocked_abort_count == 1


def test_manual_remote_session_replaces_refresh_suppresses_idle_and_builds_confirmed_abort():
    rule = MegaRule(BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 10)
    session = DispenseOverrideReference(rule)
    start = session.start_remote(50)
    assert start == bytes.fromhex(
        "AA 00 01 19 00 32 00 00 00 00 00 00 00 00 00 00 00 00 4C"
    )

    display_payload = bytearray(DISPENSE)
    display_payload[1] = 0x80
    refresh = aa_frame(bytes(display_payload))
    action, output, event = session.apply_display(refresh)
    assert (action, event) == (FrameAction.REPLACE, "REMOTE_REFRESH_REPLACED")
    assert output[4:6] == (50).to_bytes(2, "big")

    session.main_dispensing = True
    session.last_progress_ml = 12
    display_payload[1] = 0x00
    action, output, event = session.apply_display(aa_frame(bytes(display_payload)))
    assert (action, output, event) == (
        FrameAction.SUPPRESS, b"", "REMOTE_DISPLAY_IDLE_SUPPRESSED"
    )

    abort = session.request_remote_abort()
    assert abort == bytes.fromhex(
        "AA 00 00 19 00 96 00 00 00 00 00 00 00 00 00 00 00 00 AF"
    )
    assert not session.active and session.aborting


def test_stateful_dispense_override_uses_a5_as_authority_and_fails_open():
    rule = MegaRule(
        BridgeDirection.DISPLAY_TO_MAIN, DISPENSE, 10, FrameAction.REPLACE, True
    )
    session = DispenseOverrideReference(rule)
    session.apply_display(aa_frame(DISPENSE))
    a5 = bytearray(18)
    a5[1] = 0x01
    a5[3:5] = (9).to_bytes(2, "big")
    a5[16:] = (sum(a5[:16]) & 0xFFFF).to_bytes(2, "big")

    assert session.observe_a5(bytes(a5)) == "DISPENSE_PROGRESS"
    assert session.active and session.main_dispensing and session.last_progress_ml == 9
    a5[1] = 0x00
    a5[3:5] = (10).to_bytes(2, "big")
    a5[16:] = (sum(a5[:16]) & 0xFFFF).to_bytes(2, "big")
    assert session.observe_a5(bytes(a5)) == "DISPENSE_OVERRIDE_END"
    assert not session.active and not session.main_dispensing

    session.apply_display(aa_frame(DISPENSE))
    action, output, event = session.apply_display(b"\xAA\x00")
    assert action is FrameAction.PASS and output == b"\xAA\x00" and event == "FAIL_SAFE"
    assert not session.active


def test_rule_status_decodes_counters_and_last_frames():
    import struct

    original = aa_frame(DISPENSE)
    changed = bytearray(original)
    changed[4:6] = (300).to_bytes(2, "big")
    changed[17:] = (sum(changed[1:17]) & 0xFFFF).to_bytes(2, "big")
    payload = struct.pack(
        "<BBBBBHII19s19s", 1, 1, 0, 2, 1, 300, 7, 6, original, changed
    )

    status = decode_rule_status(payload)

    assert status.enabled and status.configured
    assert status.match_count == 7 and status.replace_count == 6
    assert status.last_original == original
    assert status.last_replacement == bytes(changed)


def test_dispense_override_status_and_event_decode_all_runtime_fields():
    import struct

    status_payload = struct.pack(
        "<BBBHHIBIIHBI", 1, DispenseSessionOrigin.DISPLAY_OVERRIDE, 0,
        10, 150, 123_000, 1, 4, 2, 9, 1, 875_000
    )
    status = decode_dispense_override_status(status_payload)

    assert status.active and status.start_seen and status.main_dispensing
    assert not status.aborting
    assert status.origin is DispenseSessionOrigin.DISPLAY_OVERRIDE
    assert status.target_ml == 10 and status.original_display_ml == 150
    assert status.refresh_count == 4 and status.blocked_abort_count == 2
    assert status.last_progress_ml == 9 and status.elapsed_us == 875_000

    event, event_status, original = decode_dispense_override_event(
        bytes((DispenseOverrideEvent.USER_ABORT,)) + status_payload + aa_frame(DISPENSE)
    )
    assert event is DispenseOverrideEvent.USER_ABORT
    assert event_status == status
    assert original == aa_frame(DISPENSE)


def test_replace_diagnostics_decode_build_and_physical_tx_timing():
    import struct

    original = aa_frame(DISPENSE)
    changed_payload = bytearray(DISPENSE)
    changed_payload[3:5] = (151).to_bytes(2, "big")
    replacement = aa_frame(bytes(changed_payload))
    build = struct.pack(
        "<BBBBBBB19s19s", 0, 19, 19, 0x00, 0xB0, 0x00, 0xB1,
        original, replacement,
    )
    begin = struct.pack("<BBBII19s", 0, 2, 19, 100_000, 100_125, replacement)
    end = struct.pack("<BBBHII", 0, 2, 19, 19, 100_125, 120_000)

    built = decode_replace_event(MegaMessageType.REPLACE_BUILD, build)
    started = decode_replace_event(MegaMessageType.REPLACE_TX_BEGIN, begin)
    finished = decode_replace_event(MegaMessageType.REPLACE_TX_END, end)

    assert built["original"] == original
    assert built["replacement"] == replacement
    assert built["replacement_checksum"] == b"\x00\xB1"
    assert started["target_uart"] == 2 and started["length"] == 19
    assert finished["written"] == 19
    assert finished["end_us"] - finished["begin_us"] == 19_875


def test_semantic_dispense_command_sends_only_bounded_volume_to_firmware():
    link = MegaBridgeLink()
    ignored, packet = link.command_dispense(10)
    message = MegaProtocolParser().feed(packet)[0]

    assert message.message_type is MegaMessageType.COMMAND_DISPENSE
    assert message.payload == b"\x0A\x00"

    ignored, controlled_packet = link.command_dispense(250, 1200)
    controlled = MegaProtocolParser().feed(controlled_packet)[0]
    assert controlled.message_type is MegaMessageType.COMMAND_DISPENSE
    assert controlled.payload == struct.pack("<HH", 250, 1200)

    ignored, maximum_packet = link.command_dispense(1500)
    assert MegaProtocolParser().feed(maximum_packet)[0].payload == struct.pack("<H", 1500)

    for invalid in (0, 1501, 65535):
        try:
            link.command_dispense(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe volume {invalid} was accepted")

    for invalid_speed in (0, 99, 1201, 65535):
        try:
            link.command_dispense(250, invalid_speed)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe feed-pump parameter {invalid_speed} was accepted")

    ignored, stop_packet = link.stop_remote_dispense()
    stop_message = MegaProtocolParser().feed(stop_packet)[0]
    assert stop_message.message_type is MegaMessageType.STOP_REMOTE_DISPENSE
    assert stop_message.payload == b""

def test_manual_dispense_events_report_known_frame_and_full_serial2_write():
    import struct

    payload = bytes.fromhex("00 01 19 00 0A 00 00 00 00 00 00 00 00 00 00 00")
    frame = aa_frame(payload)
    queued_payload = struct.pack("<BHB19s", 1, 10, 19, frame)
    begin_payload = struct.pack("<BHBBI19s", 1, 10, 2, 19, 100_000, frame)
    end_payload = struct.pack("<BHBBHII", 1, 10, 2, 19, 19, 100_000, 119_800)

    queued = decode_manual_command_event(MegaMessageType.MANUAL_CMD_QUEUED, queued_payload)
    started = decode_manual_command_event(MegaMessageType.MANUAL_CMD_TX_BEGIN, begin_payload)
    ended = decode_manual_command_event(MegaMessageType.MANUAL_CMD_TX_END, end_payload)

    assert queued["frame"] == bytes.fromhex(
        "AA 00 01 19 00 0A 00 00 00 00 00 00 00 00 00 00 00 00 24"
    )
    assert started["target_uart"] == 2
    assert ended["written"] == ended["length"] == 19
