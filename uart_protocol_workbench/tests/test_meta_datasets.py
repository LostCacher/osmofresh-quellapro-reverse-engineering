from protocol.checksum import calculate_sum16, sum16_bytes, validate_response
from protocol.datasets import DatasetTracker
from protocol.frames import (
    Dataset,
    FrameKey,
    RequestResponseSet,
    ResponseFrame,
    frame_key,
    load_default_response_sets,
)
from protocol.parser import FrameKind, ParsedFrame
from protocol.replay import ReplayEngine, ReplayMode, ReplayProfile


E5_B2_STATE_A = bytes.fromhex(
    "00 00 00 00 20 0C 00 00 00 00 00 00 00 00 00 B2 00 DE"
)
E5_B2_STATE_B = bytes.fromhex(
    "00 00 00 49 00 0C 00 00 00 00 00 14 00 01 1C B2 01 38"
)
A5_CAPTURE = bytes.fromhex(
    "00 00 19 00 96 00 03 B8 7A 00 00 94 19 00 F9 00 03 8A"
)
E5_E1_CAPTURE = bytes.fromhex(
    "00 02 EE 35 00 00 00 00 00 00 00 00 00 08 CA E1 02 D8"
)


def test_e5_b2_captures_have_two_byte_sum16():
    page0 = ResponseFrame.from_hex(Dataset.E5_B2, E5_B2_STATE_A.hex(" "))
    page1 = ResponseFrame.from_hex(Dataset.E5_B2, E5_B2_STATE_B.hex(" "))

    assert (page0.checksum_high, page0.checksum_low, page0.is_valid()) == (0x00, 0xDE, True)
    assert (page1.checksum_high, page1.checksum_low, page1.is_valid()) == (0x01, 0x38, True)


def test_response_requires_both_sum16_bytes():
    payload = E5_B2_STATE_B[:16]
    assert calculate_sum16(payload) == 0x0138
    assert validate_response(payload + bytes(sum16_bytes(payload)))
    assert not validate_response(payload + b"\x00\x38")


def test_default_e5_b2_does_not_round_robin_payload_states():
    response_set = load_default_response_sets()[Dataset.E5_B2]
    assert "selection_mode" not in response_set.to_dict()
    assert "fixed_meta" not in response_set.to_dict()
    assert [response_set.next_response() for _ in range(4)] == [E5_B2_STATE_A] * 4


def test_legacy_fixed_meta_cannot_select_a_state_from_checksum_high_byte():
    response_set = RequestResponseSet.from_config(
        Dataset.E5_B2,
        {
            "fixed_meta": "01",
            "payload_states": [
                {"id": "first", "response": E5_B2_STATE_A.hex(" ")},
                {"id": "second", "response": E5_B2_STATE_B.hex(" ")},
            ],
        },
    )

    assert response_set.selected_state == "first"
    assert response_set.selected_response() == E5_B2_STATE_A


def test_all_current_default_captures_are_preserved_byte_for_byte():
    response_sets = load_default_response_sets()

    assert response_sets[Dataset.A5].selected_payload_state().frame.to_bytes() == A5_CAPTURE
    assert [
        state.frame.to_bytes()
        for state in response_sets[Dataset.E5_B2].payload_states
    ] == [E5_B2_STATE_A]
    assert response_sets[Dataset.E5_E1].selected_payload_state().frame.to_bytes() == E5_E1_CAPTURE


def test_replay_static_mode_keeps_selected_payload_state():
    response_sets = load_default_response_sets()
    profile = ReplayProfile.from_dict({"name": "static", "sequences": {}})
    engine = ReplayEngine(profile, response_sets)
    engine.start(ReplayMode.STATIC)
    request = ParsedFrame(FrameKind.E5_B2, b"\xE5\xB2")

    assert [engine.handle(request).response for _ in range(4)] == [E5_B2_STATE_A] * 4


def test_old_single_response_configuration_migrates_without_data_loss():
    response_set = RequestResponseSet.from_config(
        Dataset.E5_B2, E5_B2_STATE_B.hex(" ")
    )
    assert response_set.selected_payload_state().frame.to_bytes() == E5_B2_STATE_B


def test_aa_frame_keys_and_dataset_tracker_use_payload_class():
    data0 = bytes(16)
    data1 = bytes((*([0] * 15), 0xA3))
    aa0 = b"\xAA" + data0 + bytes(sum16_bytes(data0))
    aa1 = b"\xAA" + data1 + bytes(sum16_bytes(data1))

    assert frame_key("DISPLAY -> EMU", "AA", aa0) == FrameKey(
        "DISPLAY -> EMU", "AA", "CONTROL / SETPOINT (Data[15]=00 candidate)"
    )
    assert frame_key("DISPLAY -> EMU", "AA", aa1).payload_class.startswith("SYSTEM / PROCESS")

    tracker = DatasetTracker()
    tracker.observe("DISPLAY -> EMU", "AA", aa0, 1.0)
    tracker.observe("DISPLAY -> EMU", "AA", aa1, 2.0)
    assert len(tracker.rows()) == 2
    assert {row["payload_class"] for row in tracker.rows()} == {
        "CONTROL / SETPOINT (Data[15]=00 candidate)",
        "SYSTEM / PROCESS (Data[15]=A3 candidate)",
    }
