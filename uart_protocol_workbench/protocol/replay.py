"""Request-driven startup replay profiles and state machine."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .checksum import calculate_sum16, validate_aa_frame, validate_response
from .frames import Dataset, RequestResponseSet, parse_hex_bytes
from .parser import FrameKind, ParsedFrame


class ReplayMode(str, Enum):
    STATIC = "STATIC"
    STARTUP_REPLAY = "STARTUP_REPLAY"
    MANUAL_STEP = "MANUAL_STEP"
    CAPTURE_LEARN = "CAPTURE_LEARN"


class RequestType(str, Enum):
    A5 = "A5"
    E5_B2 = "E5_B2"
    E5_E1 = "E5_E1"
    AA = "AA"


REQUEST_TO_DATASET = {
    RequestType.A5: Dataset.A5,
    RequestType.E5_B2: Dataset.E5_B2,
    RequestType.E5_E1: Dataset.E5_E1,
}
FRAME_TO_REQUEST = {
    FrameKind.A5: RequestType.A5,
    FrameKind.E5_B2: RequestType.E5_B2,
    FrameKind.E5_E1: RequestType.E5_E1,
    FrameKind.AA: RequestType.AA,
}


@dataclass
class ReplayStep:
    name: str
    request_type: RequestType
    response: bytes | None = None
    expected_aa: bytes | None = None
    aa_mask: bytes | None = None
    comment: str = ""
    timeout_ms: float | None = None
    repeat: int = 1
    hold: bool = False
    transition: str | dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, request_type: RequestType, values: dict) -> "ReplayStep":
        response = parse_hex_bytes(str(values["response"])) if values.get("response") else None
        expected_value = values.get("expected_aa", values.get("expected"))
        mask_value = values.get("aa_mask", values.get("mask"))
        expected = parse_hex_bytes(str(expected_value)) if expected_value else None
        mask = parse_hex_bytes(str(mask_value)) if mask_value else None
        if mask is not None and expected is None:
            raise ValueError("aa_mask requires expected_aa")
        if mask is not None and len(mask) != len(expected):
            raise ValueError("aa_mask and expected_aa must have equal length")
        repeat = int(values.get("repeat", values.get("repeat_count", 1)))
        if repeat < 1:
            raise ValueError("repeat must be at least 1")
        timeout = values.get("timeout_ms", values.get("timeout"))
        return cls(
            name=str(values.get("name", f"{request_type.value} state")),
            request_type=request_type,
            response=response,
            expected_aa=expected,
            aa_mask=mask,
            comment=str(values.get("comment", "")),
            timeout_ms=float(timeout) if timeout is not None else None,
            repeat=repeat,
            hold=bool(values.get("hold", False)),
            transition=values.get("transition", values.get("transition_rule")),
        )

    def to_dict(self) -> dict:
        result: dict[str, Any] = {"name": self.name}
        if self.response is not None:
            result["response"] = self.response.hex(" ").upper()
        if self.expected_aa is not None:
            result["expected_aa"] = self.expected_aa.hex(" ").upper()
        if self.aa_mask is not None:
            result["aa_mask"] = self.aa_mask.hex(" ").upper()
        if self.comment:
            result["comment"] = self.comment
        if self.timeout_ms is not None:
            result["timeout_ms"] = self.timeout_ms
        if self.repeat != 1:
            result["repeat"] = self.repeat
        if self.hold:
            result["hold"] = True
        if self.transition is not None:
            result["transition"] = self.transition
        return result

    def matches_aa(self, received: bytes) -> bool:
        if self.expected_aa is None:
            return True
        if len(received) < len(self.expected_aa):
            return False
        mask = self.aa_mask or bytes([0xFF]) * len(self.expected_aa)
        return all(
            (received_byte & mask_byte) == (expected_byte & mask_byte)
            for received_byte, expected_byte, mask_byte in zip(
                received, self.expected_aa, mask
            )
        )

    def checksum_status(self) -> dict[str, int | bool | None]:
        if self.response is None or len(self.response) != 18:
            return {"stored": self.response[-1] if self.response else None, "calculated": None, "valid": False}
        return {
            "stored": (self.response[16] << 8) | self.response[17],
            "calculated": calculate_sum16(self.response[:16]),
            "valid": validate_response(self.response),
        }


@dataclass
class ReplayProfile:
    name: str
    description: str = ""
    uart: dict[str, Any] = field(
        default_factory=lambda: {"baud": 9600, "data_bits": 8, "parity": "N", "stop_bits": 1}
    )
    response_delay_ms: float = 1.0
    sequences: dict[RequestType, list[ReplayStep]] = field(default_factory=dict)
    aa_response: bytes = b"\xCC"
    on_complete: str = "switch_to_static"
    warnings: list[str] = field(default_factory=list, repr=False)

    @classmethod
    def from_dict(cls, values: dict) -> "ReplayProfile":
        if not isinstance(values, dict):
            raise ValueError("replay profile must be a JSON object")
        raw_sequences = values.get("sequences", {})
        sequences: dict[RequestType, list[ReplayStep]] = {}
        warnings: list[str] = []
        for request_type in RequestType:
            items = raw_sequences.get(request_type.value, [])
            if not isinstance(items, list):
                raise ValueError(f"sequences.{request_type.value} must be a list")
            steps = [ReplayStep.from_dict(request_type, item) for item in items]
            sequences[request_type] = steps
            for index, step in enumerate(steps):
                if step.response is not None and request_type is not RequestType.AA:
                    status = step.checksum_status()
                    if len(step.response) != 18:
                        warnings.append(
                            f"{request_type.value}[{index}] {step.name}: expected 18 response bytes, got {len(step.response)}"
                        )
                    elif not status["valid"]:
                        warnings.append(
                            f"{request_type.value}[{index}] {step.name}: SUM16 mismatch: "
                            f"received {step.response[16]:02X} {step.response[17]:02X}, "
                            f"expected {(int(status['calculated']) >> 8) & 0xFF:02X} "
                            f"{int(status['calculated']) & 0xFF:02X}"
                        )
        aa_response = parse_hex_bytes(str(values.get("aa_response", "CC")))
        profile = cls(
            name=str(values.get("name", "Unnamed replay profile")),
            description=str(values.get("description", "")),
            uart=dict(values.get("uart", {})),
            response_delay_ms=float(values.get("response_delay_ms", 1.0)),
            sequences=sequences,
            aa_response=aa_response,
            on_complete=str(values.get("on_complete", "switch_to_static")),
            warnings=warnings,
        )
        if profile.on_complete not in ("switch_to_static", "hold_final"):
            raise ValueError("on_complete must be switch_to_static or hold_final")
        return profile

    @classmethod
    def load(cls, path: Path | str) -> "ReplayProfile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "uart": self.uart,
            "response_delay_ms": self.response_delay_ms,
            "sequences": {
                request_type.value: [step.to_dict() for step in self.sequences.get(request_type, [])]
                for request_type in RequestType
            },
            "aa_response": self.aa_response.hex(" ").upper(),
            "on_complete": self.on_complete,
        }

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")


@dataclass
class ReplayDecision:
    response: bytes | None = None
    request_type: RequestType | None = None
    step_name: str = ""
    source: str = "none"
    checksum: dict[str, int | bool | None] | None = None
    aa_matched: bool | None = None
    timeout_exceeded: bool = False
    switched_to_static: bool = False


class ReplayEngine:
    """Independent request queues with optional manual and AA transitions."""

    def __init__(
        self,
        profile: ReplayProfile,
        static_responses: dict[Dataset, bytes | RequestResponseSet],
    ) -> None:
        self.profile = profile
        self.static_responses = {
            dataset: value.clone() if isinstance(value, RequestResponseSet) else bytes(value)
            for dataset, value in static_responses.items()
        }
        self.mode = ReplayMode.STATIC
        self.reset()

    def reset(self) -> None:
        self.indices = {request_type: 0 for request_type in RequestType}
        self.repeat_counts = {request_type: 0 for request_type in RequestType}
        self.completed: set[RequestType] = set()
        self.phase = 0
        self.paused = False
        self.manual_tokens = 0
        self.aa_frames_received = 0
        self.last_aa: bytes | None = None
        self.capture_trace: list[dict[str, Any]] = []
        for value in self.static_responses.values():
            if isinstance(value, RequestResponseSet):
                value.reset()
        self.current_responses = {
            dataset: self._peek_static_response(dataset)
            for dataset in self.static_responses
        }
        self.started_epoch: float | None = None
        self.started_monotonic: float | None = None
        now = time.monotonic()
        self.step_entered_monotonic = {request_type: now for request_type in RequestType}

    def start(self, mode: ReplayMode) -> None:
        self.reset()
        self.mode = mode
        self.started_epoch = time.time()
        self.started_monotonic = time.monotonic()

    def switch_to_static(self) -> None:
        self.mode = ReplayMode.STATIC
        self.paused = False

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def authorize_next(self) -> None:
        self.manual_tokens += 1

    def update_static_response(self, dataset: Dataset, response: bytes) -> None:
        self.static_responses[dataset] = bytes(response)
        if self.mode is ReplayMode.STATIC or dataset not in self.current_responses:
            self.current_responses[dataset] = bytes(response)

    def update_static_response_set(
        self, dataset: Dataset, response_set: RequestResponseSet
    ) -> None:
        self.static_responses[dataset] = response_set.clone()
        response = self._peek_static_response(dataset)
        if self.mode is ReplayMode.STATIC or dataset not in self.current_responses:
            self.current_responses[dataset] = response

    def _peek_static_response(self, dataset: Dataset) -> bytes:
        value = self.static_responses[dataset]
        return value.selected_response() if isinstance(value, RequestResponseSet) else value

    def _next_static_response(self, dataset: Dataset) -> bytes:
        value = self.static_responses[dataset]
        response = value.next_response() if isinstance(value, RequestResponseSet) else value
        self.current_responses[dataset] = response
        return response

    def elapsed(self) -> float:
        return 0.0 if self.started_monotonic is None else time.monotonic() - self.started_monotonic

    def handle(
        self,
        frame: ParsedFrame,
        *,
        transmit_enabled: bool = True,
        timestamp: float | None = None,
    ) -> ReplayDecision:
        request_type = FRAME_TO_REQUEST.get(frame.kind)
        decision = ReplayDecision(request_type=request_type)
        if request_type is None:
            return decision
        if request_type is RequestType.AA:
            self.aa_frames_received += 1
            self.last_aa = frame.raw
        if self.mode is ReplayMode.CAPTURE_LEARN:
            self.capture_trace.append(
                {
                    "timestamp": timestamp if timestamp is not None else time.time(),
                    "relative_time_s": self.elapsed(),
                    "request_type": request_type.value,
                    "request": frame.raw.hex(" ").upper(),
                    "response": None,
                    "aa_checksum_valid": frame.checksum_valid,
                }
            )
            decision.source = "capture"
            return decision
        if not transmit_enabled or self.paused:
            decision.source = "paused" if self.paused else "disabled"
            return decision
        if self.mode is ReplayMode.STATIC:
            return self._static_decision(frame, request_type)
        if request_type is RequestType.AA:
            return self._handle_aa(frame)
        return self._handle_dataset(request_type)

    def _static_decision(self, frame: ParsedFrame, request_type: RequestType) -> ReplayDecision:
        if request_type is RequestType.AA:
            response = self.profile.aa_response if frame.checksum_valid else None
            return ReplayDecision(response, request_type, source="static-aa")
        dataset = REQUEST_TO_DATASET[request_type]
        response = self._next_static_response(dataset)
        return ReplayDecision(
            response,
            request_type,
            source="static",
            checksum=self._response_checksum(response),
        )

    def _handle_dataset(self, request_type: RequestType) -> ReplayDecision:
        dataset = REQUEST_TO_DATASET[request_type]
        steps = self.profile.sequences.get(request_type, [])
        if not steps:
            response = self._next_static_response(dataset)
            return ReplayDecision(
                response, request_type, source="static-fallback", checksum=self._response_checksum(response)
            )
        if request_type in self.completed and not steps[-1].hold:
            if self.mode is ReplayMode.MANUAL_STEP:
                step = steps[-1]
                response = step.response if step.response is not None else self._next_static_response(dataset)
                decision = ReplayDecision(
                    response,
                    request_type,
                    step.name,
                    source="replay-final",
                    checksum=self._response_checksum(response),
                )
                decision.switched_to_static = self._finish_if_complete()
                return decision
            response = self._next_static_response(dataset)
            return ReplayDecision(
                response, request_type, source="static-fallback", checksum=self._response_checksum(response)
            )
        step = steps[min(self.indices[request_type], len(steps) - 1)]
        response = step.response if step.response is not None else self._next_static_response(dataset)
        self.current_responses[dataset] = response
        decision = ReplayDecision(
            response=response,
            request_type=request_type,
            step_name=step.name,
            source="replay",
            checksum=self._response_checksum(response),
            timeout_exceeded=self._timed_out(request_type, step),
        )
        self._consume_step(request_type, step, len(steps))
        decision.switched_to_static = self._finish_if_complete()
        return decision

    def _handle_aa(self, frame: ParsedFrame) -> ReplayDecision:
        steps = self.profile.sequences.get(RequestType.AA, [])
        matched: bool | None = None
        step_name = ""
        timeout = False
        step_response: bytes | None = None
        if steps and RequestType.AA not in self.completed:
            step = steps[min(self.indices[RequestType.AA], len(steps) - 1)]
            step_name = step.name
            matched = step.matches_aa(frame.raw)
            timeout = self._timed_out(RequestType.AA, step)
            if matched:
                step_response = step.response
                self._consume_step(RequestType.AA, step, len(steps))
        response = (step_response or self.profile.aa_response) if frame.checksum_valid else None
        decision = ReplayDecision(
            response=response,
            request_type=RequestType.AA,
            step_name=step_name,
            source="replay-aa",
            checksum=self._aa_checksum(frame.raw),
            aa_matched=matched,
            timeout_exceeded=timeout,
        )
        decision.switched_to_static = self._finish_if_complete()
        return decision

    def _consume_step(self, request_type: RequestType, step: ReplayStep, length: int) -> None:
        self.repeat_counts[request_type] = min(
            self.repeat_counts[request_type] + 1, step.repeat
        )
        if self.repeat_counts[request_type] < step.repeat:
            return
        # HOLD is absolute: it neither advances nor counts as completion.
        if step.hold:
            return
        is_last = self.indices[request_type] >= length - 1
        if is_last:
            if request_type not in self.completed:
                self.completed.add(request_type)
                self._apply_transition(step)
            return
        if self.mode is ReplayMode.MANUAL_STEP:
            if self.manual_tokens < 1:
                return
            self.manual_tokens -= 1
        self.repeat_counts[request_type] = 0
        self.indices[request_type] += 1
        self._apply_transition(step)
        self.step_entered_monotonic[request_type] = time.monotonic()

    def _apply_transition(self, step: ReplayStep) -> None:
        if isinstance(step.transition, dict) and isinstance(step.transition.get("phase"), int):
            self.phase = int(step.transition["phase"])
        elif step.transition != "no_phase_change":
            self.phase += 1

    def _active_sequences(self) -> set[RequestType]:
        return {
            request_type
            for request_type, steps in self.profile.sequences.items()
            if steps
        }

    def _finish_if_complete(self) -> bool:
        active = self._active_sequences()
        if active and active.issubset(self.completed) and self.profile.on_complete == "switch_to_static":
            if self.mode is ReplayMode.MANUAL_STEP:
                if self.manual_tokens < 1:
                    return False
                self.manual_tokens -= 1
            self.switch_to_static()
            return True
        return False

    def _timed_out(self, request_type: RequestType, step: ReplayStep) -> bool:
        return (
            step.timeout_ms is not None
            and (time.monotonic() - self.step_entered_monotonic[request_type]) * 1000.0
            > step.timeout_ms
        )

    @staticmethod
    def _response_checksum(response: bytes) -> dict[str, int | bool | None]:
        if len(response) != 18:
            return {"stored": response[-1] if response else None, "calculated": None, "valid": False}
        return {
            "stored": (response[16] << 8) | response[17],
            "calculated": calculate_sum16(response[:16]),
            "valid": validate_response(response),
        }

    @staticmethod
    def _aa_checksum(raw: bytes) -> dict[str, int | bool | None]:
        if len(raw) != 19:
            return {"stored": raw[-1] if raw else None, "calculated": None, "valid": False}
        return {
            "stored": (raw[17] << 8) | raw[18],
            "calculated": calculate_sum16(raw[1:17]),
            "valid": validate_aa_frame(raw),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "paused": self.paused,
            "phase": self.phase,
            "indices": {request.value: index for request, index in self.indices.items()},
            "repeat_counts": {request.value: count for request, count in self.repeat_counts.items()},
            "sequence_status": self._sequence_status(),
            "completed": sorted(request.value for request in self.completed),
            "aa_frames_received": self.aa_frames_received,
            "last_aa": self.last_aa.hex(" ").upper() if self.last_aa else None,
            "started_epoch": self.started_epoch,
            "elapsed_s": self.elapsed(),
            "manual_tokens": self.manual_tokens,
            "current_responses": {
                dataset.value: response.hex(" ").upper()
                for dataset, response in self.current_responses.items()
            },
            "capture_trace": list(self.capture_trace),
        }

    def _sequence_status(self) -> dict[str, dict[str, Any]]:
        status: dict[str, dict[str, Any]] = {}
        for request_type in RequestType:
            steps = self.profile.sequences.get(request_type, [])
            if not steps:
                status[request_type.value] = {
                    "name": "STATIC",
                    "index": 0,
                    "repetition": 0,
                    "repeat": 0,
                    "hold": False,
                    "completed": False,
                }
                continue
            index = min(self.indices[request_type], len(steps) - 1)
            step = steps[index]
            status[request_type.value] = {
                "name": step.name,
                "index": index,
                "repetition": self.repeat_counts[request_type],
                "repeat": step.repeat,
                "hold": step.hold,
                "completed": request_type in self.completed,
            }
        return status

    def restore(self, state: dict[str, Any]) -> None:
        self.mode = ReplayMode(state["mode"])
        self.paused = bool(state.get("paused", False))
        self.phase = int(state.get("phase", 0))
        self.indices.update(
            {RequestType(key): int(value) for key, value in state.get("indices", {}).items()}
        )
        self.repeat_counts.update(
            {RequestType(key): int(value) for key, value in state.get("repeat_counts", {}).items()}
        )
        self.completed = {RequestType(value) for value in state.get("completed", [])}
        self.aa_frames_received = int(state.get("aa_frames_received", 0))
        self.last_aa = parse_hex_bytes(state["last_aa"]) if state.get("last_aa") else None
        self.started_epoch = state.get("started_epoch")
        elapsed = float(state.get("elapsed_s", 0.0))
        self.started_monotonic = time.monotonic() - elapsed if self.started_epoch else None
        self.manual_tokens = int(state.get("manual_tokens", 0))
        self.capture_trace = list(state.get("capture_trace", []))
        for dataset_name, response in state.get("current_responses", {}).items():
            self.current_responses[Dataset(dataset_name)] = parse_hex_bytes(response)

    def save_state(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.snapshot(), indent=2) + "\n", encoding="utf-8")

    def load_state(self, path: Path | str) -> None:
        self.restore(json.loads(Path(path).read_text(encoding="utf-8")))
