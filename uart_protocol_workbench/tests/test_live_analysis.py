from protocol.live_analysis import LiveChangeAnalyzer, logical_offset
from protocol.checksum import sum16_bytes


def test_previous_frame_byte_and_bit_changes():
    analyzer = LiveChangeAnalyzer()
    assert analyzer.observe("DISPLAY -> EMU", "AA", bytes.fromhex("AA 00 20"), 1.0) is None
    difference = analyzer.observe("DISPLAY -> EMU", "AA", bytes.fromhex("AA 00 21"), 1.1)
    assert len(difference.changes) == 1
    change = difference.changes[0]
    assert (change.raw_offset, change.before, change.after, change.xor) == (2, 0x20, 0x21, 0x01)
    assert [(bit.bit, bit.before, bit.after) for bit in change.bits] == [(0, 0, 1)]


def test_frame_groups_and_counters_are_independent():
    analyzer = LiveChangeAnalyzer()
    analyzer.observe("EMU -> DISPLAY", "A5", b"\x00\x10", 1.0)
    analyzer.observe("EMU -> DISPLAY", "E5 B2", b"\x00\x20", 1.1)
    assert not analyzer.observe("EMU -> DISPLAY", "A5", b"\x00\x10", 1.2).changes
    change = analyzer.observe("EMU -> DISPLAY", "E5 B2", b"\x00\x21", 1.3)
    assert change.reference == b"\x00\x20"


def test_return_from_action_state_to_baseline_keeps_falling_edge():
    analyzer = LiveChangeAnalyzer("baseline")
    analyzer.observe("D", "X", b"\x00", 1.0)
    first = analyzer.observe("D", "X", b"\x01", 1.1)
    assert first.reference == b"\x00"
    difference = analyzer.observe("D", "X", b"\x00", 1.2)
    assert difference.reference == b"\x01"
    assert [(change.before, change.after) for change in difference.changes] == [(1, 0)]


def test_capture_window_collects_all_alternating_variants_for_one_frame_group():
    analyzer = LiveChangeAnalyzer()
    analyzer.observe("D", "X", b"\x00", 1.0)
    analyzer.begin_baseline_capture()
    for timestamp, raw in ((1.1, b"\x01"), (1.2, b"\x00"), (1.3, b"\x01")):
        assert analyzer.observe("D", "X", raw, timestamp) is None
    analyzer.finish_baseline_capture()

    key = ("D", "X", None, 1)
    assert analyzer.baseline_variants[key] == (b"\x00", b"\x01")
    assert analyzer.baseline_variant_count == 2
    assert not analyzer.observe("D", "X", b"\x00", 2.0).changes
    assert not analyzer.observe("D", "X", b"\x01", 2.1).changes

    changed = analyzer.observe("D", "X", b"\x03", 2.2)
    assert changed.changes

    falling_edge = analyzer.observe("D", "X", b"\x01", 2.3)
    assert falling_edge.reference == b"\x03"
    assert [(change.before, change.after) for change in falling_edge.changes] == [
        (3, 1)
    ]


def test_marker_correlation_window():
    analyzer = LiveChangeAnalyzer(marker_window_s=2.0)
    analyzer.observe("D", "X", b"\x00", 10.0)
    analyzer.mark("Power touch", 10.5)
    near = analyzer.observe("D", "X", b"\x01", 10.75)
    far = analyzer.observe("D", "X", b"\x02", 13.0)
    assert near.marker == "Power touch"
    assert near.marker_delta_ms == 250.0
    assert far.marker == ""


def test_baseline_and_atomic_reset():
    analyzer = LiveChangeAnalyzer()
    analyzer.observe("D", "A", b"\x01", 1.0)
    analyzer.observe("D", "B", b"\x02", 1.1)
    analyzer.set_baseline()
    assert len(analyzer.baselines) == 2
    analyzer.reset()
    assert not analyzer.previous and not analyzer.baselines and not analyzer.differences
    assert not analyzer.baseline_variants and not analyzer.recent_variants
    assert not analyzer.baseline_previous
    assert analyzer.frames_observed == 0


def test_aa_payload_classes_have_independent_previous_frames_and_baselines():
    analyzer = LiveChangeAnalyzer()
    data_a = bytearray(16)
    data_b = bytearray(16)
    data_b[0] = 0x11
    data_b[15] = 0xA3
    variant_a = b"\xAA" + bytes(data_a) + bytes(sum16_bytes(data_a))
    variant_b = b"\xAA" + bytes(data_b) + bytes(sum16_bytes(data_b))
    key_a = ("DISPLAY -> EMU", "AA", "CONTROL / SETPOINT (Data[15]=00 candidate)", 19)
    key_b = ("DISPLAY -> EMU", "AA", "SYSTEM / PROCESS (Data[15]=A3 candidate)", 19)

    for timestamp, raw in enumerate((variant_a, variant_b, variant_a, variant_b), 1):
        analyzer.observe("DISPLAY -> EMU", "AA", raw, float(timestamp))
    analyzer.set_baseline()
    analyzer.set_reference_mode("baseline")

    assert analyzer.baseline_variants[key_a] == (variant_a,)
    assert analyzer.baseline_variants[key_b] == (variant_b,)
    assert analyzer.baseline_variant_count == 2
    assert not analyzer.observe("DISPLAY -> EMU", "AA", variant_a, 5.0).changes
    assert not analyzer.observe("DISPLAY -> EMU", "AA", variant_b, 6.0).changes

    changed_data = bytearray(data_a)
    changed_data[2] = 0x31
    changed_a = b"\xAA" + bytes(changed_data) + bytes(sum16_bytes(changed_data))
    difference = analyzer.observe("DISPLAY -> EMU", "AA", changed_a, 7.0)
    assert difference.reference == variant_a
    assert [(change.raw_offset, change.before, change.after) for change in difference.changes] == [
        (3, 0x00, 0x31), (18, 0x00, 0x31)
    ]

    changed_data[2] = 0x32
    changed_a_again = b"\xAA" + bytes(changed_data) + bytes(sum16_bytes(changed_data))
    next_difference = analyzer.observe("DISPLAY -> EMU", "AA", changed_a_again, 8.0)
    assert next_difference.reference == changed_a
    assert [(change.before, change.after) for change in next_difference.changes] == [
        (0x31, 0x32), (0x31, 0x32)
    ]


def test_protocol_logical_offsets_and_serialization():
    assert logical_offset("DISPLAY -> EMU", "AA", 1, 19) == "Data[0]"
    assert logical_offset("DISPLAY -> EMU", "AA", 17, 19) == "SUM16 High"
    assert logical_offset("DISPLAY -> EMU", "AA", 18, 19) == "SUM16 Low"
    assert logical_offset("EMU -> DISPLAY", "A5", 16, 18) == "SUM16 High"
    assert logical_offset("MAIN -> EMU(DISPLAY)", "A5", 16, 18) == "SUM16 High"
    assert logical_offset("MAIN -> EMU(DISPLAY)", "A5", 17, 18) == "SUM16 Low"
    analyzer = LiveChangeAnalyzer()
    analyzer.observe("EMU -> DISPLAY", "A5", b"\x00\x10", 1.0)
    difference = analyzer.observe("EMU -> DISPLAY", "A5", b"\x00\x11", 1.1)
    payload = difference.to_dict()
    assert payload["changes"][0]["after_hex"] == "11"
    assert payload["changes"][0]["after_binary"] == "00010001"
    assert payload["changes"][0]["xor_decimal"] == 1
    assert payload["changes"][0]["xor_binary"] == "00000001"
    assert payload["changes"][0]["bit_changes"] == [{"bit": 0, "before": 0, "after": 1}]


def test_e5_b2_checksum_high_is_not_used_as_frame_identity():
    analyzer = LiveChangeAnalyzer()
    meta00 = bytes.fromhex(
        "00 00 00 00 20 0C 00 00 00 00 00 00 00 00 00 B2 00 DE"
    )
    meta01 = bytes.fromhex(
        "00 00 00 49 00 0C 00 00 00 00 00 14 00 01 1C B2 01 38"
    )

    assert analyzer.observe("EMU -> DISPLAY", "E5 B2", meta00, 1.0) is None
    difference = analyzer.observe("EMU -> DISPLAY", "E5 B2", meta01, 2.0)
    assert difference.changes
    assert len(analyzer.previous) == 1


def test_e5_b2_same_meta_page_reports_payload_change():
    analyzer = LiveChangeAnalyzer()
    original = bytes.fromhex(
        "00 00 00 49 00 0C 00 00 00 00 00 14 00 01 1C B2 01 38"
    )
    changed = bytearray(original)
    changed[3] = 0x4A
    changed[17] = 0x39

    analyzer.observe("EMU -> DISPLAY", "E5 B2", original, 1.0)
    difference = analyzer.observe("EMU -> DISPLAY", "E5 B2", bytes(changed), 2.0)

    assert difference.payload_class is None
    assert [(change.raw_offset, change.before, change.after) for change in difference.changes] == [
        (3, 0x49, 0x4A),
        (17, 0x38, 0x39),
    ]
