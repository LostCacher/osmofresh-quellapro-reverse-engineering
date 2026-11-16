"""Binary PC <-> ATmega2560 bridge protocol and testable bridge primitives."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum


MAGIC = b"QB"
PROTOCOL_VERSION = 1
MAX_PAYLOAD = 512
MAX_MANUAL_DISPENSE_ML = 1500
MIN_MANUAL_FEED_PUMP_PARAMETER = 100
MAX_MANUAL_FEED_PUMP_PARAMETER = 1200
_HEADER = struct.Struct("<2sBBBHH")
_CRC = struct.Struct("<H")


class MegaMessageType(IntEnum):
    SET_CONFIG = 0x01
    START_SNIFF = 0x02
    STOP_SNIFF = 0x03
    INJECT_DISPLAY_TO_MAIN = 0x04
    INJECT_MAIN_TO_DISPLAY = 0x05
    SET_BRIDGE_MODE = 0x06
    PING = 0x07
    SET_RULE = 0x08
    CLEAR_RULE = 0x09
    ENABLE_RULE = 0x0A
    DISABLE_RULE = 0x0B
    GET_RULES = 0x0C
    COMMAND_DISPENSE = 0x0D
    STOP_REMOTE_DISPENSE = 0x0E
    SET_TANK_CONFIG = 0x0F
    GET_TANK_STATUS = 0x10

    HELLO = 0x80
    STATUS = 0x81
    RX_FRAME_DISPLAY = 0x82
    RX_FRAME_MAIN = 0x83
    INJECT_QUEUED = 0x84
    INJECT_SENT = 0x85
    ERROR = 0x86
    PONG = 0x87
    RULE_STATUS = 0x88
    RULE_MATCH = 0x89
    RULE_REPLACED = 0x8A
    RULE_ERROR = 0x8B
    REPLACE_BUILD = 0x8C
    REPLACE_TX_BEGIN = 0x8D
    REPLACE_TX_END = 0x8E
    MANUAL_CMD_QUEUED = 0x8F
    MANUAL_CMD_TX_BEGIN = 0x90
    MANUAL_CMD_TX_END = 0x91
    DISPENSE_OVERRIDE_STATUS = 0x92
    DISPENSE_OVERRIDE_EVENT = 0x93
    TANK_STATUS = 0x94
    TANK_EVENT = 0x95


class BridgeMode(IntEnum):
    DIRECT_PASS = 0
    DISPLAY_ONLY_TEST = 1


class BridgeDirection(IntEnum):
    DISPLAY_TO_MAIN = 0
    MAIN_TO_DISPLAY = 1


class FrameAction(IntEnum):
    PASS = 0
    SUPPRESS = 1
    REPLACE = 2


class MegaRuleType(IntEnum):
    DISPENSE_VOLUME = 1


_RULE = struct.Struct("<BBBBBH16s")
_RULE_STATUS = struct.Struct("<BBBBBHII19s19s")
_REPLACE_BUILD = struct.Struct("<BBBBBBB19s19s")
_REPLACE_TX_BEGIN = struct.Struct("<BBBII19s")
_REPLACE_TX_END = struct.Struct("<BBBHII")
_MANUAL_QUEUED = struct.Struct("<BHB19s")
_MANUAL_TX_BEGIN = struct.Struct("<BHBBI19s")
_MANUAL_TX_END = struct.Struct("<BHBBHII")
_DISPENSE_OVERRIDE_STATUS = struct.Struct("<BBBHHIBIIHBI")
_TANK_STATUS = struct.Struct("<BBBIII18s")


class ExternalTankState(IntEnum):
    UNKNOWN = 0
    LOW = 1
    AVAILABLE_PENDING = 2
    AVAILABLE_STABLE = 3


class TankEvent(IntEnum):
    EXTERNAL_TANK_LOW = 1
    EXTERNAL_AVAILABLE_PENDING = 2
    EXTERNAL_AVAILABLE_TIMER_RESET = 3
    EXTERNAL_AVAILABLE_STABLE = 4
    INTERNAL_TANK_MIN_CHANGED = 5
    INTERNAL_TANK_MAX_CHANGED = 6
    FILL_STATE_CHANGED = 7
    EXTERNAL_STOP_LATCHED = 8
    EXTERNAL_RESET_ARMED = 9
    EXTERNAL_STOP_CLEARED = 10


@dataclass(frozen=True)
class TankConfig:
    external_water_delay_enabled: bool = False
    external_water_delay_ms: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.external_water_delay_ms <= 60_000:
            raise ValueError("external water delay must be between 0 and 60000 ms")

    def payload(self) -> bytes:
        return struct.pack("<BI", self.external_water_delay_enabled, self.external_water_delay_ms)

    @classmethod
    def from_payload(cls, payload: bytes) -> "TankConfig":
        if len(payload) != 5:
            raise ValueError("tank config payload must contain 5 bytes")
        enabled, delay_ms = struct.unpack("<BI", payload)
        return cls(bool(enabled), delay_ms)


@dataclass(frozen=True)
class TankStatus:
    external_state: ExternalTankState
    raw_known: bool
    raw_available: bool
    stable_known: bool
    stable_available: bool
    internal_min_known: bool
    internal_min_active: bool
    internal_max_known: bool
    internal_max_active: bool
    fill_allowed: bool
    fill_active_known: bool
    fill_active: bool
    delay_enabled: bool
    external_stop_latched: bool
    external_reset_armed: bool
    delay_ms: int
    delay_remaining_ms: int
    last_update_age_ms: int
    last_frame: bytes | None


def decode_tank_status(payload: bytes) -> TankStatus:
    if len(payload) != _TANK_STATUS.size:
        raise ValueError(f"TANK_STATUS payload must contain {_TANK_STATUS.size} bytes")
    state, flags, flags2, delay_ms, remaining_ms, age_ms, raw = _TANK_STATUS.unpack(payload)
    return TankStatus(
        ExternalTankState(state),
        bool(flags & 0x01), bool(flags & 0x02), bool(flags & 0x04), bool(flags & 0x08),
        bool(flags & 0x10), bool(flags & 0x20), bool(flags & 0x40), bool(flags & 0x80),
        bool(flags2 & 0x01), bool(flags2 & 0x02), bool(flags2 & 0x04), bool(flags2 & 0x08),
        bool(flags2 & 0x10), bool(flags2 & 0x20),
        delay_ms, remaining_ms, age_ms, raw if any(raw) else None,
    )


def decode_tank_event(payload: bytes) -> tuple[TankEvent, int, TankStatus]:
    if len(payload) != 5 + _TANK_STATUS.size:
        raise ValueError("TANK_EVENT payload has an invalid size")
    return TankEvent(payload[0]), struct.unpack_from("<I", payload, 1)[0], decode_tank_status(payload[5:])


class DispenseOverrideEvent(IntEnum):
    START = 1
    REFRESH_REPLACED = 2
    ABORT_SUPPRESSED = 3
    PROGRESS = 4
    TARGET_REACHED = 5
    END = 6
    USER_ABORT = 7
    REMOTE_START = 8
    REMOTE_DISPLAY_IDLE_SUPPRESSED = 9
    REMOTE_REFRESH_REPLACED = 10
    REMOTE_PROGRESS = 11
    REMOTE_COMPLETE = 12
    REMOTE_ABORT_REQUESTED = 13
    REMOTE_ABORT_TX = 14
    REMOTE_ABORT_CONFIRMED = 15
    REMOTE_END = 16
    REMOTE_ERROR = 17
    REMOTE_PUMP_PARAMETER_REPLACED = 21


class DispenseSessionOrigin(IntEnum):
    NONE = 0
    DISPLAY_OVERRIDE = 1
    MANUAL_REMOTE = 2


@dataclass(frozen=True)
class DispenseOverrideStatus:
    active: bool
    origin: DispenseSessionOrigin
    aborting: bool
    target_ml: int
    original_display_ml: int
    start_timestamp_us: int
    start_seen: bool
    refresh_count: int
    blocked_abort_count: int
    last_progress_ml: int
    main_dispensing: bool
    elapsed_us: int


def decode_dispense_override_status(payload: bytes) -> DispenseOverrideStatus:
    if len(payload) != _DISPENSE_OVERRIDE_STATUS.size:
        raise ValueError(
            f"DISPENSE_OVERRIDE_STATUS payload must contain {_DISPENSE_OVERRIDE_STATUS.size} bytes"
        )
    values = _DISPENSE_OVERRIDE_STATUS.unpack(payload)
    return DispenseOverrideStatus(
        bool(values[0]), DispenseSessionOrigin(values[1]), bool(values[2]),
        values[3], values[4], values[5], bool(values[6]), values[7], values[8],
        values[9], bool(values[10]), values[11],
    )


def decode_dispense_override_event(
    payload: bytes,
) -> tuple[DispenseOverrideEvent, DispenseOverrideStatus, bytes | None]:
    if len(payload) != 1 + _DISPENSE_OVERRIDE_STATUS.size + 19:
        raise ValueError("DISPENSE_OVERRIDE_EVENT payload has an invalid size")
    status_end = 1 + _DISPENSE_OVERRIDE_STATUS.size
    original = payload[status_end:status_end + 19]
    return (
        DispenseOverrideEvent(payload[0]),
        decode_dispense_override_status(payload[1:status_end]),
        original if original[:1] == b"\xAA" else None,
    )


@dataclass(frozen=True)
class MegaRule:
    """One exact, direction-bound firmware rule for a known AA00 dispense command."""

    direction: BridgeDirection
    match_payload: bytes
    override_value: int
    action: FrameAction = FrameAction.REPLACE
    enabled: bool = False
    rule_type: MegaRuleType = MegaRuleType.DISPENSE_VOLUME

    def __post_init__(self) -> None:
        if len(self.match_payload) != 16:
            raise ValueError("rule match payload must contain exactly 16 bytes")
        if not 0 <= self.override_value <= 0xFFFF:
            raise ValueError("override value must fit in two bytes")
        if self.rule_type is MegaRuleType.DISPENSE_VOLUME:
            if self.match_payload[1] != 0x01 or self.match_payload[15] != 0x00:
                raise ValueError("dispense rule requires an AA00 payload with Data[1]=01")

    def payload(self) -> bytes:
        return _RULE.pack(
            1,
            int(self.rule_type),
            int(self.direction),
            int(self.action),
            int(self.enabled),
            self.override_value,
            bytes(self.match_payload),
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "MegaRule":
        if len(payload) != _RULE.size:
            raise ValueError(f"SET_RULE payload must contain {_RULE.size} bytes")
        version, rule_type, direction, action, enabled, override, match = _RULE.unpack(payload)
        if version != 1:
            raise ValueError(f"unsupported rule version {version}")
        return cls(
            BridgeDirection(direction), match, override, FrameAction(action),
            bool(enabled), MegaRuleType(rule_type),
        )


@dataclass(frozen=True)
class MegaRuleStatus:
    configured: bool
    enabled: bool
    direction: BridgeDirection
    action: FrameAction
    rule_type: MegaRuleType
    override_value: int
    match_count: int
    replace_count: int
    last_original: bytes | None
    last_replacement: bytes | None


def decode_rule_status(payload: bytes) -> MegaRuleStatus:
    if len(payload) != _RULE_STATUS.size:
        raise ValueError(f"RULE_STATUS payload must contain {_RULE_STATUS.size} bytes")
    configured, enabled, direction, action, rule_type, override, matches, replacements, original, replacement = _RULE_STATUS.unpack(payload)
    return MegaRuleStatus(
        bool(configured), bool(enabled), BridgeDirection(direction), FrameAction(action),
        MegaRuleType(rule_type), override, matches, replacements,
        original if original[:1] == b"\xAA" else None,
        replacement if replacement[:1] == b"\xAA" else None,
    )


def decode_replace_event(message_type: MegaMessageType, payload: bytes) -> dict:
    """Decode detailed firmware-local replacement diagnostics."""
    if message_type is MegaMessageType.REPLACE_BUILD:
        if len(payload) != _REPLACE_BUILD.size:
            raise ValueError("REPLACE_BUILD payload has an invalid size")
        direction, original_len, replacement_len, oh, ol, rh, rl, original, replacement = _REPLACE_BUILD.unpack(payload)
        return {
            "direction": BridgeDirection(direction),
            "original_len": original_len,
            "replacement_len": replacement_len,
            "original_checksum": bytes((oh, ol)),
            "replacement_checksum": bytes((rh, rl)),
            "original": original[:original_len],
            "replacement": replacement[:replacement_len],
        }
    if message_type is MegaMessageType.REPLACE_TX_BEGIN:
        if len(payload) != _REPLACE_TX_BEGIN.size:
            raise ValueError("REPLACE_TX_BEGIN payload has an invalid size")
        direction, target_uart, length, last_rx_us, begin_us, frame = _REPLACE_TX_BEGIN.unpack(payload)
        return {
            "direction": BridgeDirection(direction), "target_uart": target_uart,
            "length": length, "last_rx_us": last_rx_us, "begin_us": begin_us,
            "frame": frame[:length],
        }
    if message_type is MegaMessageType.REPLACE_TX_END:
        if len(payload) != _REPLACE_TX_END.size:
            raise ValueError("REPLACE_TX_END payload has an invalid size")
        direction, target_uart, length, written, begin_us, end_us = _REPLACE_TX_END.unpack(payload)
        return {
            "direction": BridgeDirection(direction), "target_uart": target_uart,
            "length": length, "written": written, "begin_us": begin_us,
            "end_us": end_us,
        }
    raise ValueError(f"{message_type.name} is not a replacement diagnostic")


def decode_manual_command_event(message_type: MegaMessageType, payload: bytes) -> dict:
    """Decode the semantic manual-dispense queue and TX diagnostics."""
    if message_type is MegaMessageType.MANUAL_CMD_QUEUED:
        if len(payload) != _MANUAL_QUEUED.size:
            raise ValueError("MANUAL_CMD_QUEUED payload has an invalid size")
        command, volume_ml, length, frame = _MANUAL_QUEUED.unpack(payload)
        return {"command": command, "volume_ml": volume_ml, "length": length, "frame": frame[:length]}
    if message_type is MegaMessageType.MANUAL_CMD_TX_BEGIN:
        if len(payload) != _MANUAL_TX_BEGIN.size:
            raise ValueError("MANUAL_CMD_TX_BEGIN payload has an invalid size")
        command, volume_ml, target_uart, length, begin_us, frame = _MANUAL_TX_BEGIN.unpack(payload)
        return {
            "command": command, "volume_ml": volume_ml, "target_uart": target_uart,
            "length": length, "begin_us": begin_us, "frame": frame[:length],
        }
    if message_type is MegaMessageType.MANUAL_CMD_TX_END:
        if len(payload) != _MANUAL_TX_END.size:
            raise ValueError("MANUAL_CMD_TX_END payload has an invalid size")
        command, volume_ml, target_uart, length, written, begin_us, end_us = _MANUAL_TX_END.unpack(payload)
        return {
            "command": command, "volume_ml": volume_ml, "target_uart": target_uart,
            "length": length, "written": written, "begin_us": begin_us, "end_us": end_us,
        }
    raise ValueError(f"{message_type.name} is not a manual-command diagnostic")


def apply_rule_reference(
    rule: MegaRule, direction: BridgeDirection, raw: bytes
) -> tuple[FrameAction, bytes, bool]:
    """Pure reference for firmware matching; every uncertainty returns PASS."""
    original = bytes(raw)
    if not rule.enabled or direction != rule.direction:
        return FrameAction.PASS, original, False
    if len(original) != 19 or original[0] != 0xAA:
        return FrameAction.PASS, original, False
    checksum = sum(original[1:17]) & 0xFFFF
    if original[17:] != checksum.to_bytes(2, "big"):
        return FrameAction.PASS, original, False
    if original[1:17] != rule.match_payload:
        return FrameAction.PASS, original, False
    if rule.action is FrameAction.PASS:
        return FrameAction.PASS, original, True
    if rule.action is FrameAction.SUPPRESS:
        return FrameAction.SUPPRESS, b"", True
    replacement = bytearray(original)
    replacement[4:6] = rule.override_value.to_bytes(2, "big")
    replacement[17:19] = (sum(replacement[1:17]) & 0xFFFF).to_bytes(2, "big")
    return FrameAction.REPLACE, bytes(replacement), True


@dataclass
class DispenseOverrideReference:
    """Pure reference model for the firmware's stateful dispense override."""

    rule: MegaRule
    active: bool = False
    origin: DispenseSessionOrigin = DispenseSessionOrigin.NONE
    aborting: bool = False
    target_ml: int = 0
    original_display_ml: int = 0
    refresh_count: int = 0
    blocked_abort_count: int = 0
    last_progress_ml: int = 0
    main_dispensing: bool = False
    control_payload: bytes = b""

    def _family_matches(self, raw: bytes) -> bool:
        payload = raw[1:17]
        reference = (
            self.control_payload
            if self.origin is DispenseSessionOrigin.MANUAL_REMOTE
            else self.rule.match_payload
        )
        if len(reference) != 16 or payload[0] != reference[0] or payload[15] != 0:
            return False
        if (
            self.origin is DispenseSessionOrigin.DISPLAY_OVERRIDE
            and int.from_bytes(payload[3:5], "big") != self.original_display_ml
        ):
            return False
        return all(
            offset in (1, 3, 4) or value == reference[offset]
            for offset, value in enumerate(payload)
        )

    def start_remote(self, target_ml: int) -> bytes:
        payload = bytearray(16)
        payload[1] = 0x01
        payload[2] = 0x19
        payload[3:5] = target_ml.to_bytes(2, "big")
        self.active = True
        self.aborting = False
        self.origin = DispenseSessionOrigin.MANUAL_REMOTE
        self.target_ml = target_ml
        self.original_display_ml = 0
        self.refresh_count = self.blocked_abort_count = self.last_progress_ml = 0
        self.main_dispensing = False
        self.control_payload = bytes(payload)
        return b"\xAA" + bytes(payload) + (sum(payload) & 0xFFFF).to_bytes(2, "big")

    def request_remote_abort(self) -> bytes:
        if not self.active or self.origin is not DispenseSessionOrigin.MANUAL_REMOTE:
            raise ValueError("no active MANUAL_REMOTE session")
        payload = bytearray(16)
        payload[1] = 0x00
        payload[2] = 0x19
        payload[3:5] = (150).to_bytes(2, "big")
        self.active = False
        self.aborting = True
        return b"\xAA" + bytes(payload) + (sum(payload) & 0xFFFF).to_bytes(2, "big")

    def _replace_volume(self, raw: bytes) -> bytes:
        replacement = bytearray(raw)
        replacement[4:6] = self.target_ml.to_bytes(2, "big")
        replacement[17:19] = (sum(replacement[1:17]) & 0xFFFF).to_bytes(2, "big")
        return bytes(replacement)

    def apply_display(self, raw: bytes) -> tuple[FrameAction, bytes, str | None]:
        original = bytes(raw)
        valid = (
            len(original) == 19
            and original[:1] == b"\xAA"
            and original[17:] == (sum(original[1:17]) & 0xFFFF).to_bytes(2, "big")
        )
        if not valid:
            self.active = False
            self.aborting = False
            return FrameAction.PASS, original, "FAIL_SAFE"
        if self.aborting:
            return FrameAction.PASS, original, None
        if self.active and self.origin is DispenseSessionOrigin.MANUAL_REMOTE:
            command = original[2]
            if command == 0x80 and self._family_matches(original):
                self.original_display_ml = self.original_display_ml or int.from_bytes(original[4:6], "big")
                self.refresh_count += 1
                return FrameAction.REPLACE, self._replace_volume(original), "REMOTE_REFRESH_REPLACED"
            if (
                command == 0x00
                and self._family_matches(original)
                and self.main_dispensing
                and self.last_progress_ml < self.target_ml
            ):
                self.original_display_ml = self.original_display_ml or int.from_bytes(original[4:6], "big")
                self.blocked_abort_count += 1
                return FrameAction.SUPPRESS, b"", "REMOTE_DISPLAY_IDLE_SUPPRESSED"
            return FrameAction.PASS, original, None
        if not self.rule.enabled or self.rule.action is not FrameAction.REPLACE:
            action, output, matched = apply_rule_reference(
                self.rule, BridgeDirection.DISPLAY_TO_MAIN, original
            )
            return action, output, "MATCH" if matched else None
        if not self.active:
            if original[1:17] != self.rule.match_payload:
                return FrameAction.PASS, original, None
            self.active = True
            self.origin = DispenseSessionOrigin.DISPLAY_OVERRIDE
            self.target_ml = self.rule.override_value
            self.original_display_ml = int.from_bytes(original[4:6], "big")
            return FrameAction.REPLACE, self._replace_volume(original), "DISPENSE_OVERRIDE_START"
        command = original[2]
        if command == 0x80 and self._family_matches(original):
            self.refresh_count += 1
            return FrameAction.REPLACE, self._replace_volume(original), "DISPENSE_REFRESH_REPLACED"
        if command == 0x00 and self._family_matches(original):
            if self.origin is DispenseSessionOrigin.DISPLAY_OVERRIDE:
                self.active = False
                self.aborting = True
                return FrameAction.PASS, original, "DISPENSE_USER_ABORT"
            if (
                self.origin is DispenseSessionOrigin.MANUAL_REMOTE
                and self.main_dispensing
                and self.last_progress_ml < self.target_ml
            ):
                self.blocked_abort_count += 1
                return FrameAction.SUPPRESS, b"", "DISPENSE_ABORT_SUPPRESSED"
        return FrameAction.PASS, original, None

    def observe_a5(self, raw: bytes) -> str:
        response = bytes(raw)
        valid = (
            len(response) == 18
            and response[16:] == (sum(response[:16]) & 0xFFFF).to_bytes(2, "big")
        )
        if not valid:
            self.active = False
            self.aborting = False
            self.main_dispensing = False
            return "FAIL_SAFE"
        self.main_dispensing = response[1] == 0x01
        self.last_progress_ml = int.from_bytes(response[3:5], "big")
        if not self.main_dispensing:
            was_aborting = self.aborting
            was_remote = self.origin is DispenseSessionOrigin.MANUAL_REMOTE
            self.active = False
            self.aborting = False
            if was_remote:
                return (
                    "REMOTE_DISPENSE_ABORT_CONFIRMED"
                    if was_aborting else (
                        "REMOTE_DISPENSE_COMPLETE"
                        if self.last_progress_ml >= self.target_ml else "FAIL_SAFE"
                    )
                )
            return (
                "DISPENSE_OVERRIDE_END"
                if was_aborting or self.last_progress_ml >= self.target_ml else "FAIL_SAFE"
            )
        if self.origin is DispenseSessionOrigin.MANUAL_REMOTE:
            return "REMOTE_DISPENSE_PROGRESS"
        if self.last_progress_ml >= self.target_ml:
            return "DISPENSE_TARGET_REACHED"
        return "DISPENSE_PROGRESS"


@dataclass
class ExternalWaterDelayReference:
    """Pure reference for the external-empty operator-reset latch."""

    config: TankConfig = TankConfig()
    state: ExternalTankState = ExternalTankState.UNKNOWN
    candidate_since_ms: int | None = None
    last_valid_ms: int | None = None
    timeout_ms: int = 2_000
    raw_available: bool | None = None
    stop_latched: bool = False
    reset_armed: bool = False

    @property
    def stable_available(self) -> bool:
        return self.state is ExternalTankState.AVAILABLE_STABLE

    def process_b2(self, raw: bytes, now_ms: int) -> tuple[bytes, list[TankEvent]]:
        frame = bytes(raw)
        if len(frame) != 18 or frame[15] != 0xB2 or frame[16:] != (sum(frame[:16]) & 0xFFFF).to_bytes(2, "big"):
            self.state = ExternalTankState.LOW if self.stop_latched else ExternalTankState.UNKNOWN
            self.candidate_since_ms = None
            self.raw_available = None
            self.reset_armed = False
            return frame, []
        self.last_valid_ms = now_ms
        available = not bool(frame[4] & 0x01)
        self.raw_available = available
        events: list[TankEvent] = []
        if not available:
            if not self.stop_latched:
                events.extend((TankEvent.EXTERNAL_TANK_LOW, TankEvent.EXTERNAL_STOP_LATCHED))
            self.stop_latched = True
            self.reset_armed = False
            self.state = ExternalTankState.LOW
            self.candidate_since_ms = None
            return frame, events
        if not self.stop_latched:
            if self.state is not ExternalTankState.AVAILABLE_STABLE:
                events.append(TankEvent.EXTERNAL_AVAILABLE_STABLE)
            self.state = ExternalTankState.AVAILABLE_STABLE
            return frame, events
        blocked = bytearray(frame)
        blocked[4] |= 0x01
        blocked[16:] = (sum(blocked[:16]) & 0xFFFF).to_bytes(2, "big")
        return bytes(blocked), events

    def process_control(self, d1: int, *, idle: bool) -> tuple[bool, list[TankEvent]]:
        """Return whether an AA00 control frame may pass to Main."""
        if not self.stop_latched:
            return True, []
        if d1 == 0x01:
            events: list[TankEvent] = []
            if self.raw_available and idle and not self.reset_armed:
                self.reset_armed = True
                events.append(TankEvent.EXTERNAL_RESET_ARMED)
            return False, events
        if d1 == 0x00 and self.raw_available and idle and self.reset_armed:
            self.stop_latched = False
            self.reset_armed = False
            self.state = ExternalTankState.AVAILABLE_STABLE
            return True, [TankEvent.EXTERNAL_STOP_CLEARED, TankEvent.EXTERNAL_AVAILABLE_STABLE]
        return True, []

    def tick(self, now_ms: int) -> list[TankEvent]:
        events: list[TankEvent] = []
        if self.last_valid_ms is not None and now_ms - self.last_valid_ms > self.timeout_ms:
            self.state = ExternalTankState.LOW if self.stop_latched else ExternalTankState.UNKNOWN
            self.candidate_since_ms = None
            self.last_valid_ms = None
            self.raw_available = None
            self.reset_armed = False
        return events


@dataclass(frozen=True)
class MegaMessage:
    message_type: MegaMessageType
    payload: bytes = b""
    sequence: int = 0
    flags: int = 0


@dataclass(frozen=True)
class MegaConfig:
    mode: BridgeMode = BridgeMode.DIRECT_PASS
    quella_baud: int = 9600
    idle_gap_us: int = 4000
    sniff_enabled: bool = True

    def payload(self) -> bytes:
        return struct.pack(
            "<BIIB",
            int(self.mode),
            self.quella_baud,
            self.idle_gap_us,
            int(self.sniff_enabled),
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "MegaConfig":
        if len(payload) != struct.calcsize("<BIIB"):
            raise ValueError("SET_CONFIG payload must contain 10 bytes")
        mode, baud, gap, sniff = struct.unpack("<BIIB", payload)
        return cls(BridgeMode(mode), baud, gap, bool(sniff))


def crc16_ccitt(data: bytes, initial: int = 0xFFFF) -> int:
    crc = initial
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_message(message: MegaMessage) -> bytes:
    payload = bytes(message.payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD} bytes")
    header = _HEADER.pack(
        MAGIC,
        PROTOCOL_VERSION,
        int(message.message_type),
        message.flags & 0xFF,
        message.sequence & 0xFFFF,
        len(payload),
    )
    protected = header[2:] + payload
    return header + payload + _CRC.pack(crc16_ccitt(protected))


class MegaProtocolParser:
    """Incremental CRC-checked decoder with magic-byte resynchronization."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.crc_errors = 0
        self.version_errors = 0
        self.length_errors = 0

    @property
    def pending(self) -> bytes:
        return bytes(self._buffer)

    def feed(self, chunk: bytes | bytearray) -> list[MegaMessage]:
        self._buffer.extend(chunk)
        result: list[MegaMessage] = []
        while True:
            marker = self._buffer.find(MAGIC)
            if marker < 0:
                if self._buffer[-1:] == MAGIC[:1]:
                    del self._buffer[:-1]
                else:
                    self._buffer.clear()
                break
            if marker:
                del self._buffer[:marker]
            if len(self._buffer) < _HEADER.size:
                break
            magic, version, raw_type, flags, sequence, length = _HEADER.unpack_from(
                self._buffer
            )
            if length > MAX_PAYLOAD:
                self.length_errors += 1
                del self._buffer[0]
                continue
            total = _HEADER.size + length + _CRC.size
            if len(self._buffer) < total:
                break
            raw = bytes(self._buffer[:total])
            if version != PROTOCOL_VERSION:
                self.version_errors += 1
                del self._buffer[:total]
                continue
            expected = _CRC.unpack_from(raw, total - _CRC.size)[0]
            if crc16_ccitt(raw[2:-2]) != expected:
                self.crc_errors += 1
                del self._buffer[0]
                continue
            del self._buffer[:total]
            try:
                message_type = MegaMessageType(raw_type)
            except ValueError:
                continue
            result.append(
                MegaMessage(
                    message_type,
                    raw[_HEADER.size : _HEADER.size + length],
                    sequence,
                    flags,
                )
            )
        return result


class MegaBridgeLink:
    """Sequence allocator and encoder used by the serial worker."""

    def __init__(self) -> None:
        self.parser = MegaProtocolParser()
        self._sequence = 0

    def message(self, message_type: MegaMessageType, payload: bytes = b"") -> tuple[int, bytes]:
        self._sequence = (self._sequence + 1) & 0xFFFF
        if self._sequence == 0:
            self._sequence = 1
        message = MegaMessage(message_type, payload, self._sequence)
        return self._sequence, encode_message(message)

    def set_config(self, config: MegaConfig) -> tuple[int, bytes]:
        return self.message(MegaMessageType.SET_CONFIG, config.payload())

    def inject(self, direction: BridgeDirection, raw: bytes) -> tuple[int, bytes]:
        if not 1 <= len(raw) <= 64:
            raise ValueError("Mega injection frames must contain 1..64 bytes")
        message_type = (
            MegaMessageType.INJECT_DISPLAY_TO_MAIN
            if direction is BridgeDirection.DISPLAY_TO_MAIN
            else MegaMessageType.INJECT_MAIN_TO_DISPLAY
        )
        return self.message(message_type, bytes(raw))

    def set_rule(self, rule: MegaRule) -> tuple[int, bytes]:
        return self.message(MegaMessageType.SET_RULE, rule.payload())

    def rule_command(self, command: MegaMessageType) -> tuple[int, bytes]:
        if command not in {
            MegaMessageType.CLEAR_RULE,
            MegaMessageType.ENABLE_RULE,
            MegaMessageType.DISABLE_RULE,
            MegaMessageType.GET_RULES,
        }:
            raise ValueError("not a rule command")
        return self.message(command)

    def command_dispense(
        self, volume_ml: int, feed_pump_parameter: int | None = None
    ) -> tuple[int, bytes]:
        # The original display exposes up to 900 ml; the verified remote path
        # deliberately permits larger setpoints up to 1500 ml.
        if not 1 <= volume_ml <= MAX_MANUAL_DISPENSE_ML:
            raise ValueError(
                f"manual dispense volume must be between 1 and {MAX_MANUAL_DISPENSE_ML} ml"
            )
        payload = struct.pack("<H", volume_ml)
        if feed_pump_parameter is not None:
            if not MIN_MANUAL_FEED_PUMP_PARAMETER <= feed_pump_parameter <= MAX_MANUAL_FEED_PUMP_PARAMETER:
                raise ValueError(
                    "25 C feed-pump parameter must be between "
                    f"{MIN_MANUAL_FEED_PUMP_PARAMETER} and "
                    f"{MAX_MANUAL_FEED_PUMP_PARAMETER} ml/min"
                )
            payload += struct.pack("<H", feed_pump_parameter)
        return self.message(MegaMessageType.COMMAND_DISPENSE, payload)

    def stop_remote_dispense(self) -> tuple[int, bytes]:
        return self.message(MegaMessageType.STOP_REMOTE_DISPENSE)

    def set_tank_config(self, config: TankConfig) -> tuple[int, bytes]:
        return self.message(MegaMessageType.SET_TANK_CONFIG, config.payload())

    def get_tank_status(self) -> tuple[int, bytes]:
        return self.message(MegaMessageType.GET_TANK_STATUS)


def decode_rx_payload(payload: bytes) -> tuple[int, bytes]:
    """Return firmware micros() timestamp and captured bytes."""
    if len(payload) < 6:
        raise ValueError("RX frame payload is truncated")
    timestamp_us, length = struct.unpack_from("<IH", payload)
    raw = payload[6:]
    if len(raw) != length:
        raise ValueError("RX frame byte count does not match payload")
    return timestamp_us, raw


def classify_main_frame(raw: bytes) -> str:
    if raw == b"\xCC":
        return "CC"
    if len(raw) == 18:
        if raw[15] == 0xB2:
            return "E5 B2"
        if raw[15] == 0xE1:
            return "E5 E1"
        return "A5"
    return "UNKNOWN"


class IdleInjectionQueue:
    """Small reference model for deterministic safe-gap injection tests."""

    def __init__(self, idle_gap_us: int = 4000) -> None:
        self.idle_gap_us = idle_gap_us
        self.last_activity_us = {direction: 0 for direction in BridgeDirection}
        self.queue: list[tuple[BridgeDirection, bytes]] = []

    def observe(self, direction: BridgeDirection, raw: bytes, timestamp_us: int) -> bytes:
        if raw:
            self.last_activity_us[direction] = timestamp_us
        return bytes(raw)  # transparent pass-through reference behavior

    def inject(self, direction: BridgeDirection, raw: bytes) -> None:
        if not raw:
            raise ValueError("cannot queue an empty injection")
        self.queue.append((direction, bytes(raw)))

    def due(self, now_us: int) -> tuple[BridgeDirection, bytes] | None:
        if not self.queue:
            return None
        if any(now_us - value < self.idle_gap_us for value in self.last_activity_us.values()):
            return None
        return self.queue.pop(0)
