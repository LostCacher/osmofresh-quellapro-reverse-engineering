from protocol.parser import FrameKind, StreamParser
from protocol.checksum import sum16_bytes


def kinds(frames):
    return [frame.kind for frame in frames]


def test_recognizes_requests():
    parser = StreamParser()
    assert kinds(parser.feed(b"\xA5")) == [FrameKind.A5]
    assert kinds(parser.feed(b"\xE5\xB2")) == [FrameKind.E5_B2]
    assert kinds(parser.feed(b"\xE5\xE1")) == [FrameKind.E5_E1]


def test_fragmented_e5_request():
    parser = StreamParser()
    assert parser.feed(b"\xE5") == []
    frames = parser.feed(b"\xB2")
    assert kinds(frames) == [FrameKind.E5_B2]
    assert frames[0].raw == b"\xE5\xB2"


def test_aa_parsing_fragmented_and_checksum():
    data = bytes(range(16))
    raw = b"\xAA" + data + bytes(sum16_bytes(data))
    parser = StreamParser()
    assert parser.feed(raw[:7]) == []
    frames = parser.feed(raw[7:])
    assert kinds(frames) == [FrameKind.AA]
    assert frames[0].checksum_valid is True


def test_multiple_frames_in_one_read():
    parser = StreamParser()
    frames = parser.feed(b"\xA5\xE5\xB2\xE5\xE1\x42")
    assert kinds(frames) == [
        FrameKind.A5,
        FrameKind.E5_B2,
        FrameKind.E5_E1,
        FrameKind.UNKNOWN,
    ]
    assert frames[-1].raw == b"\x42"


def test_unknown_e5_is_not_discarded():
    parser = StreamParser()
    frames = parser.feed(b"\xE5\x44")
    assert [frame.raw for frame in frames] == [b"\xE5", b"\x44"]
