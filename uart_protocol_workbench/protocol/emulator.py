"""Protocol routing, statistics, and recent-frame history."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from time import perf_counter

from .frames import Dataset, RequestResponseSet
from .parser import FrameKind, ParsedFrame


@dataclass
class Statistics:
    a5_requests: int = 0
    e5_b2_requests: int = 0
    e5_e1_requests: int = 0
    aa_frames: int = 0
    valid_aa_checksums: int = 0
    invalid_aa_checksums: int = 0
    unknown_bytes: int = 0
    responses_sent: int = 0


class EmulatorCore:
    """Thread-confined protocol state; serial transport is supplied by the worker."""

    def __init__(self, history_limit: int = 200) -> None:
        self.stats = Statistics()
        self.latencies_ms: list[float] = []
        self.history: deque[tuple[float, ParsedFrame]] = deque(maxlen=history_limit)

    def register_received(self, frame: ParsedFrame) -> None:
        self.history.append((perf_counter(), frame))
        if frame.kind is FrameKind.A5:
            self.stats.a5_requests += 1
        elif frame.kind is FrameKind.E5_B2:
            self.stats.e5_b2_requests += 1
        elif frame.kind is FrameKind.E5_E1:
            self.stats.e5_e1_requests += 1
        elif frame.kind is FrameKind.AA:
            self.stats.aa_frames += 1
            if frame.checksum_valid:
                self.stats.valid_aa_checksums += 1
            else:
                self.stats.invalid_aa_checksums += 1
        else:
            self.stats.unknown_bytes += len(frame.raw)

    def response_for(
        self,
        frame: ParsedFrame,
        responses: dict[Dataset, bytes | RequestResponseSet],
    ) -> bytes | None:
        mapping = {
            FrameKind.A5: Dataset.A5,
            FrameKind.E5_B2: Dataset.E5_B2,
            FrameKind.E5_E1: Dataset.E5_E1,
        }
        if frame.kind in mapping:
            response = responses[mapping[frame.kind]]
            return response.next_response() if isinstance(response, RequestResponseSet) else response
        if frame.kind is FrameKind.AA and frame.checksum_valid:
            return b"\xCC"
        return None

    def register_response(self, latency_ms: float) -> None:
        self.stats.responses_sent += 1
        self.latencies_ms.append(latency_ms)

    def snapshot(self) -> dict[str, int | float]:
        result: dict[str, int | float] = asdict(self.stats)
        if self.latencies_ms:
            result.update(
                average_latency_ms=sum(self.latencies_ms) / len(self.latencies_ms),
                minimum_latency_ms=min(self.latencies_ms),
                maximum_latency_ms=max(self.latencies_ms),
            )
        else:
            result.update(average_latency_ms=0.0, minimum_latency_ms=0.0, maximum_latency_ms=0.0)
        return result
