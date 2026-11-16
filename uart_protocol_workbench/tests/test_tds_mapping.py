from protocol.checksum import validate_response
import pytest

from protocol.tds_mapping import TdsMappingSequencer, tds_display_value


BASE_B2 = bytes.fromhex(
    "00 00 00 00 00 0C 00 00 00 00 00 0C 00 01 14 B2 00 DF"
)


def test_tds_mapping_emits_measurement_then_commit_and_holds() -> None:
    sequence = TdsMappingSequencer()
    sequence.queue(0x32)

    measurement = sequence.transform_b2(BASE_B2)
    commit = sequence.transform_b2(BASE_B2)
    held = sequence.transform_b2(BASE_B2)

    assert measurement == bytes.fromhex(
        "00 00 00 32 00 0C 00 00 00 00 00 1C 00 01 14 B2 01 21"
    )
    assert commit == bytes.fromhex(
        "00 00 00 32 00 0C 00 00 00 00 00 0C 00 01 14 B2 01 11"
    )
    assert held == commit
    assert validate_response(measurement)
    assert validate_response(commit)
    assert sequence.phase == "hold"


def test_new_raw_value_restarts_latch_pulse() -> None:
    sequence = TdsMappingSequencer()
    sequence.queue(1)
    sequence.transform_b2(BASE_B2)
    sequence.transform_b2(BASE_B2)
    sequence.queue(255)

    assert sequence.transform_b2(BASE_B2)[11] & 0x10
    assert not sequence.transform_b2(BASE_B2)[11] & 0x10


def test_disabled_sequence_leaves_response_untouched() -> None:
    sequence = TdsMappingSequencer()
    sequence.queue(0x55)
    sequence.disable()
    assert sequence.transform_b2(BASE_B2) == BASE_B2


@pytest.mark.parametrize("raw, displayed", [
    (0x00, 0), (0x1F, 0), (0x20, 1), (0x2A, 1), (0x2B, 2),
    (0x36, 2), (0x37, 3), (0x58, 5), (0x59, 6), (0x6E, 6),
    (0x6F, 7), (0xA4, 11), (0xA5, 12), (0xC4, 13), (0xC5, 14),
    (0xF9, 18), (0xFA, 19), (0xFF, 19),
    (48, 2), (55, 3),
])
def test_captured_tds_threshold_mapping(raw: int, displayed: int) -> None:
    assert tds_display_value(raw) == displayed
