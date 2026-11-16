"""Live byte/bit change detection for recurring UART protocol frames."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .frames import frame_payload_class
from .framing import FrameByteRole, frame_byte_metadata


@dataclass(frozen=True)
class BitTransition:
    bit: int
    before: int
    after: int


@dataclass(frozen=True)
class ByteChange:
    raw_offset: int
    logical_offset: str
    byte_role: FrameByteRole
    payload_offset: int | None
    before: int
    after: int
    xor: int
    bits: tuple[BitTransition, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_offset": self.raw_offset,
            "logical_offset": self.logical_offset,
            "byte_role": self.byte_role.value,
            "payload_offset": self.payload_offset,
            "before_hex": f"{self.before:02X}",
            "before_decimal": self.before,
            "before_binary": f"{self.before:08b}",
            "after_hex": f"{self.after:02X}",
            "after_decimal": self.after,
            "after_binary": f"{self.after:08b}",
            "xor_hex": f"{self.xor:02X}",
            "xor_decimal": self.xor,
            "xor_binary": f"{self.xor:08b}",
            "bit_changes": [
                {"bit": item.bit, "before": item.before, "after": item.after}
                for item in self.bits
            ],
        }


@dataclass
class LiveFrameDifference:
    timestamp: float
    direction: str
    frame_type: str
    payload_class: str | None
    reference: bytes
    current: bytes
    changes: tuple[ByteChange, ...]
    marker: str = ""
    marker_delta_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "direction": self.direction,
            "frame_type": self.frame_type,
            "payload_class": self.payload_class,
            "reference": self.reference.hex(" ").upper(),
            "current": self.current.hex(" ").upper(),
            "marker": self.marker,
            "marker_delta_ms": self.marker_delta_ms,
            "changes": [change.to_dict() for change in self.changes],
        }


class LiveChangeAnalyzer:
    """Compare each frame only with frames of identical direction/type/length."""

    def __init__(self, reference_mode: str = "previous", marker_window_s: float = 3.0) -> None:
        self.reference_mode = "previous"
        self.set_reference_mode(reference_mode)
        self.marker_window_s = marker_window_s
        self.previous: dict[tuple[str, str, str | None, int], bytes] = {}
        self.baselines: dict[tuple[str, str, str | None, int], bytes] = {}
        self.baseline_variants: dict[tuple[str, str, str | None, int], tuple[bytes, ...]] = {}
        self.baseline_previous: dict[
            tuple[tuple[str, str, str | None, int], bytes], bytes
        ] = {}
        self.recent_variants: dict[tuple[str, str, str | None, int], tuple[bytes, ...]] = {}
        self.baseline_capture_active = False
        self.baseline_capture_variants: dict[
            tuple[str, str, str | None, int], list[bytes]
        ] = {}
        self.last_frames: dict[tuple[str, str, str | None, int], tuple[float, bytes]] = {}
        self.differences: list[LiveFrameDifference] = []
        self.active_marker = ""
        self.marker_timestamp: float | None = None
        self.frames_observed = 0
        self.frames_changed = 0
        self.bytes_changed = 0

    def set_reference_mode(self, mode: str) -> None:
        if mode not in ("previous", "baseline"):
            raise ValueError("reference mode must be previous or baseline")
        self.reference_mode = mode

    def mark(self, label: str, timestamp: float | None = None) -> None:
        label = label.strip()
        if not label:
            raise ValueError("marker label must not be empty")
        self.active_marker = label
        self.marker_timestamp = time.time() if timestamp is None else timestamp

    def clear_marker(self) -> None:
        self.active_marker = ""
        self.marker_timestamp = None

    def set_baseline(self) -> None:
        """Keep backward-compatible immediate baseline behavior."""
        self.begin_baseline_capture()
        self.finish_baseline_capture()

    def begin_baseline_capture(self) -> None:
        """Start collecting every unique frame variant until explicitly finished."""
        self.baseline_capture_active = True
        self.baseline_capture_variants = {
            key: [raw] for key, raw in self.previous.items()
        }

    def finish_baseline_capture(self) -> None:
        captured = {
            key: tuple(variants)
            for key, variants in self.baseline_capture_variants.items()
            if variants
        }
        self.baseline_variants = captured
        self.baselines = {
            key: variants[-1] for key, variants in captured.items()
        }
        self.baseline_previous = {
            (key, variant): variant
            for key, variants in self.baseline_variants.items()
            for variant in variants
        }
        self.baseline_capture_active = False
        self.baseline_capture_variants = {}
        self.reference_mode = "baseline"

    def cancel_baseline_capture(self) -> None:
        self.baseline_capture_active = False
        self.baseline_capture_variants = {}

    def is_baseline_transition(
        self,
        direction: str,
        frame_type: str,
        payload_class: str | None,
        reference: bytes,
        current: bytes,
    ) -> bool:
        key = (direction, frame_type, payload_class, len(current))
        variants = self.baseline_variants.get(key, ())
        return reference in variants and current in variants

    def discard_baseline_transitions(self) -> None:
        self.differences = [
            difference
            for difference in self.differences
            if not self.is_baseline_transition(
                difference.direction,
                difference.frame_type,
                difference.payload_class,
                difference.reference,
                difference.current,
            )
        ]
        self.frames_changed = len(self.differences)
        self.bytes_changed = sum(
            len(difference.changes) for difference in self.differences
        )

    @property
    def baseline_variant_count(self) -> int:
        return sum(len(variants) for variants in self.baseline_variants.values())

    def clear_events(self) -> None:
        self.differences.clear()
        self.frames_changed = 0
        self.bytes_changed = 0

    def reset(self) -> None:
        self.previous.clear()
        self.baselines.clear()
        self.baseline_variants.clear()
        self.baseline_previous.clear()
        self.cancel_baseline_capture()
        self.recent_variants.clear()
        self.last_frames.clear()
        self.differences.clear()
        self.clear_marker()
        self.frames_observed = 0
        self.frames_changed = 0
        self.bytes_changed = 0

    def observe(
        self,
        direction: str,
        frame_type: str,
        raw: bytes,
        timestamp: float | None = None,
    ) -> LiveFrameDifference | None:
        timestamp = time.time() if timestamp is None else timestamp
        raw = bytes(raw)
        payload_class = frame_payload_class(frame_type, raw)
        key = (direction, frame_type, payload_class, len(raw))
        previous_for_key = self.previous.get(key)
        self.frames_observed += 1
        if self.baseline_capture_active:
            variants = self.baseline_capture_variants.setdefault(key, [])
            if raw not in variants:
                variants.append(raw)
            self.previous[key] = raw
            self._remember_variant(key, raw)
            self.last_frames[key] = (timestamp, raw)
            return None
        if self.reference_mode == "baseline":
            variants = self.baseline_variants.get(key, ())
            baseline = min(variants, key=lambda item: self._distance(item, raw)) if variants else None
            if baseline is None:
                baseline = self.baselines.get(key)
            if raw in variants:
                baseline = raw
                # Suppress ordinary switching between captured baseline states,
                # but preserve the falling edge when an action/non-baseline
                # state returns to a captured normal state.
                reference = (
                    raw
                    if previous_for_key is None or previous_for_key in variants
                    else previous_for_key
                )
                self.baseline_previous[(key, raw)] = raw
            else:
                reference = self.baseline_previous.get((key, baseline), baseline) if baseline else None
            if baseline is not None and raw not in variants:
                self.baseline_previous[(key, baseline)] = raw
        else:
            reference = self.previous.get(key)
        if reference is None:
            reference = self.previous.get(key)
        self.previous[key] = raw
        self._remember_variant(key, raw)
        self.baselines.setdefault(key, raw)
        self.baseline_variants.setdefault(key, (raw,))
        initial_baseline = self.baseline_variants[key][0]
        self.baseline_previous.setdefault((key, initial_baseline), raw)
        self.last_frames[key] = (timestamp, raw)
        if reference is None:
            return None
        changes = tuple(
            self._byte_change(direction, frame_type, offset, before, after, len(raw))
            for offset, (before, after) in enumerate(zip(reference, raw))
            if before != after
        )
        marker = ""
        marker_delta_ms: float | None = None
        if self.active_marker and self.marker_timestamp is not None:
            delta = timestamp - self.marker_timestamp
            if 0 <= delta <= self.marker_window_s:
                marker = self.active_marker
                marker_delta_ms = delta * 1000.0
        difference = LiveFrameDifference(
            timestamp,
            direction,
            frame_type,
            payload_class,
            reference,
            raw,
            changes,
            marker,
            marker_delta_ms,
        )
        if changes:
            self.differences.append(difference)
            self.frames_changed += 1
            self.bytes_changed += len(changes)
        return difference

    def _remember_variant(
        self, key: tuple[str, str, str | None, int], raw: bytes
    ) -> None:
        """Keep the latest baseline candidate for every real payload class."""
        limit = 1
        variants = [item for item in self.recent_variants.get(key, ()) if item != raw]
        variants.append(raw)
        self.recent_variants[key] = tuple(variants[-limit:])

    @staticmethod
    def _distance(reference: bytes, current: bytes) -> int:
        return sum((before ^ after).bit_count() for before, after in zip(reference, current))

    @staticmethod
    def _byte_change(
        direction: str,
        frame_type: str,
        offset: int,
        before: int,
        after: int,
        length: int,
    ) -> ByteChange:
        xor = before ^ after
        bits = tuple(
            BitTransition(bit, (before >> bit) & 1, (after >> bit) & 1)
            for bit in range(7, -1, -1)
            if xor & (1 << bit)
        )
        metadata = frame_byte_metadata(frame_type, length, offset)
        return ByteChange(
            offset,
            metadata.logical_offset,
            metadata.role,
            metadata.payload_offset,
            before,
            after,
            xor,
            bits,
        )


def logical_offset(direction: str, frame_type: str, offset: int, length: int) -> str:
    """Compatibility wrapper; layout is intentionally independent of direction."""
    return frame_byte_metadata(frame_type, length, offset).logical_offset
