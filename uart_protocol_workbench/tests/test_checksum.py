from protocol.checksum import calculate_sum16, sum16_bytes, validate_aa_frame, validate_response
from protocol.frames import Dataset, ResponseFrame


def test_known_response_checksums():
    for dataset in Dataset:
        frame = ResponseFrame.default(dataset)
        assert sum16_bytes(frame.data) == (frame.checksum_high, frame.checksum_low)
        assert validate_response(frame.to_bytes())


def test_e5_response_id_observations_validate_without_generalizing_a5():
    assert ResponseFrame.default(Dataset.E5_B2).response_id_matches_request() is True
    assert ResponseFrame.default(Dataset.E5_E1).response_id_matches_request() is True
    assert ResponseFrame.default(Dataset.A5).response_id_matches_request() is None


def test_sum16_boundaries_and_both_transmitted_bytes():
    assert calculate_sum16(bytes([0x38] + [0x00] * 15)) == 0x0038
    assert sum16_bytes(bytes([0x10] * 16)) == (0x01, 0x00)
    assert sum16_bytes(bytes([0x20] * 16)) == (0x02, 0x00)


def test_editing_data_recalculates_high_and_low_bytes():
    frame = ResponseFrame.from_hex(
        Dataset.E5_B2,
        "00 00 00 00 20 0C 00 00 00 00 00 00 00 00 00 B2 00 DE",
    )
    frame.set_data(0, 0xFF)
    assert (frame.checksum_high, frame.checksum_low) == sum16_bytes(frame.data)
    assert (frame.checksum_high, frame.checksum_low) == (0x01, 0xDD)


def test_invalid_response_checksum_is_rejected():
    frame = bytearray(ResponseFrame.default(Dataset.E5_B2).to_bytes())
    frame[-1] ^= 0x01
    assert not validate_response(frame)


def test_aa_validation():
    data = bytes(range(16))
    frame = b"\xAA" + data + bytes(sum16_bytes(data))
    assert validate_aa_frame(frame)
    assert not validate_aa_frame(frame[:-1] + bytes((frame[-1] ^ 1,)))


def test_aa_header_is_excluded_from_sum16():
    data = bytes([0x20] * 16)
    frame = b"\xAA" + data + bytes(sum16_bytes(data))
    assert frame[-2:] == b"\x02\x00"
    assert validate_aa_frame(frame)
