import pytest

from gui.udp_telemetry_worker import frame_checksum_valid, parse_udp_endpoint
from protocol.checksum import sum16_bytes


def test_parse_udp_endpoint_accepts_url_and_host_port():
    assert parse_udp_endpoint("udp://0.0.0.0:42042") == ("0.0.0.0", 42042)
    assert parse_udp_endpoint("127.0.0.1:1234") == ("127.0.0.1", 1234)


@pytest.mark.parametrize("endpoint", ["", "ws://127.0.0.1:42042", "udp://x:0"])
def test_parse_udp_endpoint_rejects_invalid_values(endpoint):
    with pytest.raises(ValueError):
        parse_udp_endpoint(endpoint)


def test_udp_response_checksum_classification():
    payload = bytes.fromhex("00 00 19 00 96 00 04 0D 5D 00 00 84 19 00 EE 00")
    response = payload + bytes(sum16_bytes(payload))
    assert frame_checksum_valid("A5", response) is True
    assert frame_checksum_valid("A5", response[:-1] + b"\x00") is False
    assert frame_checksum_valid("UNKNOWN", response) is None
