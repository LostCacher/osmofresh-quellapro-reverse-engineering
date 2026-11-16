"""Payload-class-aware dataset observation and histogram state."""

from __future__ import annotations

from dataclasses import dataclass, field

from .frames import FrameKey, frame_key
from .framing import frame_byte_metadata


@dataclass
class DatasetObservation:
    key: FrameKey
    count: int
    first_seen: float
    last_seen: float
    latest: bytes
    unique_payloads: set[bytes] = field(default_factory=set)
    changed_offsets: set[int] = field(default_factory=set)


class DatasetTracker:
    def __init__(self) -> None:
        self.observations: dict[FrameKey, DatasetObservation] = {}

    def observe(
        self, direction: str, frame_type: str, raw: bytes, timestamp: float
    ) -> DatasetObservation:
        raw = bytes(raw)
        key = frame_key(direction, frame_type, raw)
        observation = self.observations.get(key)
        if observation is None:
            observation = DatasetObservation(
                key, 0, timestamp, timestamp, raw, {raw}, set()
            )
            self.observations[key] = observation
        else:
            observation.changed_offsets.update(
                offset
                for offset, (before, after) in enumerate(zip(observation.latest, raw))
                if before != after
            )
            observation.last_seen = timestamp
            observation.latest = raw
            observation.unique_payloads.add(raw)
        observation.count += 1
        return observation

    def rows(self) -> list[dict]:
        return [
            {
                "direction": item.key.direction,
                "frame_type": item.key.request_type,
                "payload_class": item.key.payload_class,
                "count": item.count,
                "first_seen": item.first_seen,
                "last_seen": item.last_seen,
                "latest": item.latest,
                "unique_payloads": len(item.unique_payloads),
                "changed_offsets": sorted(item.changed_offsets),
                "changed_fields": [
                    {
                        "raw_offset": offset,
                        "logical_offset": frame_byte_metadata(
                            item.key.request_type, len(item.latest), offset
                        ).logical_offset,
                        "byte_role": frame_byte_metadata(
                            item.key.request_type, len(item.latest), offset
                        ).role.value,
                    }
                    for offset in sorted(item.changed_offsets)
                ],
            }
            for item in sorted(
                self.observations.values(),
                key=lambda value: (
                    value.key.direction,
                    value.key.request_type,
                    "" if value.key.payload_class is None else value.key.payload_class,
                ),
            )
        ]
