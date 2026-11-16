import pytest

from gui.homeserver_state_worker import (
    decode_state_frames,
    normalize_state_url,
    state_statistics,
)


def test_normalize_homeserver_state_url():
    assert normalize_state_url("osmofresh-quella.server.home") == (
        "https://osmofresh-quella.server.home/state"
    )
    assert normalize_state_url("http://server:8001/state") == "http://server:8001/state"
    with pytest.raises(ValueError):
        normalize_state_url("ftp://server/state")


def test_decode_homeserver_snapshot_for_normal_gui_pipeline():
    snapshot = {
        "written_at": 100.0,
        "receiver": {
            "packets_total": 12,
            "frames_total": 30,
            "lost_packets_total": 2,
            "mega_dropped_raw_frames": 1,
        },
        "frames": {
            "MAIN_TO_DISPLAY:A5": {
                "direction": "MAIN_TO_DISPLAY",
                "frame_type": "A5",
                "received_at": 99.5,
                "checksum_valid": True,
                "raw_bytes": list(range(18)),
            },
            "DISPLAY_TO_MAIN:AA00": {
                "direction": "DISPLAY_TO_MAIN",
                "frame_type": "AA00",
                "received_at": 99.6,
                "checksum_valid": False,
                "raw_hex": "AA " + "00 " * 18,
            },
        },
    }

    decoded = decode_state_frames(snapshot)

    assert decoded[0][:4] == ("MAIN_TO_DISPLAY", "A5", bytes(range(18)), True)
    assert decoded[1][0:2] == ("DISPLAY_TO_MAIN", "AA")
    assert state_statistics(snapshot)["lost"] == 2
