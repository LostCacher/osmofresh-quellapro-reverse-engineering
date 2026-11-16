import json

from protocol.frames import Dataset, ResponseFrame
from protocol.parser import FrameKind, ParsedFrame, StreamParser
from protocol.replay import ReplayEngine, ReplayMode, ReplayProfile, RequestType


STATIC = {dataset: ResponseFrame.default(dataset).to_bytes() for dataset in Dataset}
B0 = bytes.fromhex("00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 B2 00 B2")
B1 = bytes.fromhex("00 00 00 00 01 00 00 00 00 00 00 00 00 00 00 B2 00 B3")
C0 = bytes.fromhex("00 01 F4 00 00 00 00 00 00 00 00 00 00 08 CA E1 02 A8")


def profile(values=None, on_complete="hold_final"):
    sequences = values or {
        "A5": [],
        "E5_B2": [{"name": "B0", "response": B0.hex(" ")}, {"name": "B1", "response": B1.hex(" "), "hold": True}],
        "E5_E1": [{"name": "C0", "response": C0.hex(" "), "hold": True}],
        "AA": [],
    }
    return ReplayProfile.from_dict({"name": "test", "sequences": sequences, "on_complete": on_complete})


def request(kind, raw):
    return ParsedFrame(kind, raw)


def test_queue_advancement_and_independent_queues():
    engine = ReplayEngine(profile(), STATIC)
    engine.start(ReplayMode.STARTUP_REPLAY)
    assert engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2")).response == B0
    assert engine.indices[RequestType.E5_B2] == 1
    assert engine.indices[RequestType.E5_E1] == 0
    assert engine.handle(request(FrameKind.E5_E1, b"\xE5\xE1")).response == C0
    assert engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2")).response == B1


def test_hold_final_state_and_reset():
    engine = ReplayEngine(profile(), STATIC)
    engine.start(ReplayMode.STARTUP_REPLAY)
    engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2"))
    engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2"))
    assert engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2")).response == B1
    assert RequestType.E5_B2 not in engine.completed
    engine.reset()
    assert engine.indices[RequestType.E5_B2] == 0
    assert engine.phase == 0
    assert all(value == 0 for value in engine.repeat_counts.values())


def test_current_startup_profile_preserves_exact_frames_and_hold_state():
    profile_path = (
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "config"
        / "startup_profiles"
        / "real_main_startup.json"
    )
    replay_profile = ReplayProfile.load(profile_path)
    engine = ReplayEngine(replay_profile, STATIC)
    engine.start(ReplayMode.STARTUP_REPLAY)
    a5_request = request(FrameKind.A5, b"\xA5")

    a5 = replay_profile.sequences[RequestType.A5][0].response
    responses = [engine.handle(a5_request).response for _ in range(5)]
    assert responses == [a5] * 5
    assert a5 == bytes.fromhex(
        "00 00 19 00 96 00 03 B8 7A 00 00 94 19 00 F9 00 03 8A"
    )
    assert engine.indices[RequestType.A5] == 0
    assert engine.mode is ReplayMode.STARTUP_REPLAY
    assert RequestType.A5 not in engine.completed
    status = engine.snapshot()["sequence_status"]["A5"]
    assert status["name"] == "A5"
    assert status["hold"] is True


def test_repeat_counts_and_reset_are_independent_for_all_datasets():
    replay_profile = ReplayProfile.from_dict(
        {
            "name": "independent repeats",
            "on_complete": "hold_final",
            "sequences": {
                "A5": [{"name": "A", "response": STATIC[Dataset.A5].hex(" "), "repeat": 4, "hold": True}],
                "E5_B2": [{"name": "B", "response": B1.hex(" "), "repeat": 2, "hold": True}],
                "E5_E1": [{"name": "C", "response": C0.hex(" "), "repeat": 3, "hold": True}],
                "AA": [],
            },
        }
    )
    engine = ReplayEngine(replay_profile, STATIC)
    engine.start(ReplayMode.STARTUP_REPLAY)
    engine.handle(request(FrameKind.A5, b"\xA5"))
    engine.handle(request(FrameKind.A5, b"\xA5"))
    engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2"))

    status = engine.snapshot()["sequence_status"]
    assert status["A5"]["repetition"] == 2
    assert status["E5_B2"]["repetition"] == 1
    assert status["E5_E1"]["repetition"] == 0

    engine.reset()
    status = engine.snapshot()["sequence_status"]
    assert all(status[key]["index"] == 0 for key in ("A5", "E5_B2", "E5_E1"))
    assert all(status[key]["repetition"] == 0 for key in ("A5", "E5_B2", "E5_E1"))


def test_completion_switches_to_static():
    values = {"A5": [], "E5_B2": [{"name": "B0", "response": B0.hex(" ")}], "E5_E1": [], "AA": []}
    engine = ReplayEngine(profile(values, "switch_to_static"), STATIC)
    engine.start(ReplayMode.STARTUP_REPLAY)
    decision = engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2"))
    assert decision.switched_to_static
    assert engine.mode is ReplayMode.STATIC
    assert engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2")).response == STATIC[Dataset.E5_B2]


def test_manual_step_requires_authorization_to_advance():
    engine = ReplayEngine(profile(), STATIC)
    engine.start(ReplayMode.MANUAL_STEP)
    assert engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2")).response == B0
    assert engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2")).response == B0
    engine.authorize_next()
    assert engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2")).response == B0
    assert engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2")).response == B1


def test_manual_step_requires_authorization_to_switch_static():
    values = {"A5": [], "E5_B2": [{"name": "B0", "response": B0.hex(" ")}], "E5_E1": [], "AA": []}
    engine = ReplayEngine(profile(values, "switch_to_static"), STATIC)
    engine.start(ReplayMode.MANUAL_STEP)
    engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2"))
    assert engine.mode is ReplayMode.MANUAL_STEP
    engine.authorize_next()
    decision = engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2"))
    assert decision.switched_to_static
    assert engine.mode is ReplayMode.STATIC


def test_aa_masked_matching_and_checksum():
    data = bytes(range(16))
    aa = b"\xAA" + data + bytes(((sum(data) >> 8) & 0xFF, sum(data) & 0xFF))
    mask = "FF FF 00 00"
    values = {"A5": [], "E5_B2": [], "E5_E1": [], "AA": [{"name": "trigger", "expected_aa": "AA 00 FE FE", "aa_mask": mask}]}
    engine = ReplayEngine(profile(values), STATIC)
    engine.start(ReplayMode.STARTUP_REPLAY)
    decision = engine.handle(ParsedFrame(FrameKind.AA, aa, True))
    assert decision.aa_matched is True
    assert decision.response == b"\xCC"
    assert decision.checksum == {"stored": 120, "calculated": 120, "valid": True}


def test_aa_step_can_override_ack_response_and_aliases_are_supported():
    data = bytes(range(16))
    aa = b"\xAA" + data + b"\x55" + bytes((sum(data) & 0xFF,))
    values = {
        "A5": [],
        "E5_B2": [],
        "E5_E1": [],
        "AA": [{"name": "trigger", "expected": "AA 00", "mask": "FF FF", "response": "CC", "repeat_count": 1}],
    }
    engine = ReplayEngine(profile(values), STATIC)
    engine.start(ReplayMode.STARTUP_REPLAY)
    assert engine.handle(ParsedFrame(FrameKind.AA, aa, True)).response == b"\xCC"


def test_invalid_profile_checksum_warns_but_is_not_changed():
    invalid = bytearray(B0)
    invalid[-1] ^= 1
    values = {"A5": [], "E5_B2": [{"name": "captured", "response": invalid.hex(" ")}], "E5_E1": [], "AA": []}
    loaded = profile(values)
    assert loaded.warnings
    assert loaded.sequences[RequestType.E5_B2][0].response == bytes(invalid)


def test_state_and_profile_json_persistence(tmp_path):
    profile_path = tmp_path / "profile.json"
    original = profile()
    original.save(profile_path)
    loaded = ReplayProfile.load(profile_path)
    assert loaded.to_dict() == original.to_dict()

    engine = ReplayEngine(loaded, STATIC)
    engine.start(ReplayMode.STARTUP_REPLAY)
    engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2"))
    state_path = tmp_path / "state.json"
    engine.save_state(state_path)
    restored = ReplayEngine(loaded, STATIC)
    restored.load_state(state_path)
    assert restored.indices == engine.indices
    assert restored.mode == engine.mode
    assert json.loads(state_path.read_text())["phase"] == engine.phase


def test_capture_learning_and_fragmented_requests():
    engine = ReplayEngine(profile(), STATIC)
    engine.start(ReplayMode.CAPTURE_LEARN)
    parser = StreamParser()
    assert parser.feed(b"\xE5") == []
    frames = parser.feed(b"\xB2\xA5")
    for frame in frames:
        assert engine.handle(frame).response is None
    assert [entry["request_type"] for entry in engine.capture_trace] == ["E5_B2", "A5"]


def test_timeouts_are_independent_per_dataset(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("protocol.replay.time.monotonic", lambda: clock[0])
    values = {
        "A5": [],
        "E5_B2": [
            {"name": "B0", "response": B0.hex(" ")},
            {"name": "B1", "response": B1.hex(" "), "timeout_ms": 100},
        ],
        "E5_E1": [{"name": "C0", "response": C0.hex(" "), "timeout_ms": 100}],
        "AA": [],
    }
    engine = ReplayEngine(profile(values), STATIC)
    engine.start(ReplayMode.STARTUP_REPLAY)
    clock[0] = 0.05
    engine.handle(request(FrameKind.E5_B2, b"\xE5\xB2"))
    clock[0] = 0.11
    decision = engine.handle(request(FrameKind.E5_E1, b"\xE5\xE1"))
    assert decision.timeout_exceeded
