"""Display-emulator role, request scheduler, response framing, and timing stats."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .checksum import sum16_bytes, validate_response


class EmulatorRole(str, Enum):
    MAIN_MCU = "main_mcu_emulator"
    DISPLAY = "display_emulator"


class DisplayRequest(str, Enum):
    A5 = "A5"
    E5_B2 = "E5 B2"
    E5_E1 = "E5 E1"
    AA = "AA"


class AutoAAMode(str, Enum):
    OFF = "OFF"
    AUTO = "AUTO"
    AUTO_MANUAL_INJECT = "AUTO + MANUAL INJECT"
    MANUAL_OVERRIDE = "MANUAL OVERRIDE"


class SafetyAction(str, Enum):
    INHIBIT_DISPENSE = "INHIBIT_DISPENSE"
    ABORT_DISPENSE = "ABORT_DISPENSE"
    INHIBIT_FILTER = "INHIBIT_FILTER"
    ABORT_FILTER = "ABORT_FILTER"
    INHIBIT_FLUSH = "INHIBIT_FLUSH"
    ABORT_FLUSH = "ABORT_FLUSH"
    START_REFILL = "START_REFILL"


@dataclass(frozen=True)
class SafetyRule:
    name: str
    action: SafetyAction
    enabled: bool = False
    experimental: bool = True
    required_mapping: str = ""


@dataclass
class MainState:
    """Latest checksum-valid Main-MCU payloads plus confirmed decoded fields."""

    raw_a5: bytes | None = None
    raw_b2: bytes | None = None
    raw_e1: bytes | None = None
    a5_timestamp: float | None = None
    b2_timestamp: float | None = None
    e1_timestamp: float | None = None
    a5_valid: bool = False
    b2_valid: bool = False
    e1_valid: bool = False

    def update(self, request: DisplayRequest, payload: bytes, timestamp: float) -> None:
        if len(payload) != 16:
            raise ValueError("MainState requires exactly 16 payload bytes")
        raw = bytes(payload)
        if request is DisplayRequest.A5:
            self.raw_a5, self.a5_timestamp, self.a5_valid = raw, timestamp, True
        elif request is DisplayRequest.E5_B2:
            self.raw_b2, self.b2_timestamp, self.b2_valid = raw, timestamp, True
        elif request is DisplayRequest.E5_E1:
            self.raw_e1, self.e1_timestamp, self.e1_valid = raw, timestamp, True

    @property
    def external_tank_available(self) -> bool | None:
        return None if self.raw_b2 is None else bool(self.raw_b2[4] & 0x01)

    @property
    def internal_low_sensor(self) -> bool | None:
        """B2 D4.bit3: True means the water level is BELOW LOW."""
        return None if self.raw_b2 is None else bool(self.raw_b2[4] & 0x08)

    @property
    def a5_internal_below_low(self) -> bool | None:
        """A5 D11.bit0: True means the water level is BELOW LOW."""
        return None if self.raw_a5 is None else bool(self.raw_a5[11] & 0x01)

    @property
    def internal_below_low(self) -> bool | None:
        """Newest supported representation of the same physical LOW sensor."""
        candidates = []
        if self.raw_b2 is not None and self.b2_timestamp is not None:
            candidates.append((self.b2_timestamp, self.internal_low_sensor))
        if self.raw_a5 is not None and self.a5_timestamp is not None:
            candidates.append((self.a5_timestamp, self.a5_internal_below_low))
        return max(candidates, default=(0.0, None), key=lambda item: item[0])[1]

    @property
    def internal_high_sensor(self) -> bool | None:
        return None if self.raw_b2 is None else bool(self.raw_b2[4] & 0x20)

    @property
    def internal_low(self) -> bool | None:
        """Compatibility alias: True means BELOW LOW."""
        return self.internal_below_low

    @property
    def internal_high(self) -> bool | None:
        """Compatibility alias; FILTER-stop policy remains experimental."""
        return self.internal_high_sensor

    @property
    def current_dispense_volume(self) -> int | None:
        return None if self.raw_a5 is None else int.from_bytes(self.raw_a5[3:5], "big")

    @property
    def outlet_temperature(self) -> int | None:
        return None if self.raw_a5 is None else self.raw_a5[2]

    @property
    def inlet_temperature(self) -> int | None:
        return None if self.raw_a5 is None else self.raw_a5[12]

    @property
    def e1_flow_parameter(self) -> int | None:
        return None if self.raw_e1 is None else int.from_bytes(self.raw_e1[1:3], "big")

    def snapshot(self, now: float) -> dict[str, Any]:
        def entry(raw: bytes | None, timestamp: float | None, valid: bool) -> dict:
            return {
                "valid": valid,
                "age_ms": None if timestamp is None else max(0.0, (now - timestamp) * 1000.0),
                "raw_payload": "" if raw is None else raw.hex(" ").upper(),
            }

        return {
            "A5": entry(self.raw_a5, self.a5_timestamp, self.a5_valid),
            "E5 B2": entry(self.raw_b2, self.b2_timestamp, self.b2_valid),
            "E5 E1": entry(self.raw_e1, self.e1_timestamp, self.e1_valid),
            "decoded": {
                "external_tank_available": self.external_tank_available,
                "internal_low_sensor": self.internal_low_sensor,
                "a5_internal_below_low": self.a5_internal_below_low,
                "internal_below_low": self.internal_below_low,
                "internal_high_sensor": self.internal_high_sensor,
                "current_dispense_volume": self.current_dispense_volume,
                "outlet_temperature": self.outlet_temperature,
                "inlet_temperature": self.inlet_temperature,
                "e1_flow_parameter": self.e1_flow_parameter,
            },
        }


@dataclass
class DisplayState:
    temperature: int = 25
    volume_ml: int = 150
    dispense_command: int = 0x80
    process_mode: int = 0x68
    requested_process_mode: int | None = None
    a3_phase: int = 0x00
    e1_flow_parameter: int = 750
    initial_fill_active: bool = False
    dispense_allowed: bool = True

    def __post_init__(self) -> None:
        if self.requested_process_mode is None:
            self.requested_process_mode = self.process_mode

    def aa00_payload(self) -> bytes:
        payload = bytearray(16)
        payload[1] = self.dispense_command
        payload[2] = self.temperature
        payload[3:5] = self.volume_ml.to_bytes(2, "big")
        return bytes(payload)

    def a3_payload(self) -> bytes:
        payload = bytearray(16)
        payload[4] = int(self.requested_process_mode)
        payload[5] = self.a3_phase
        payload[15] = 0xA3
        return bytes(payload)

    @property
    def a3_mode(self) -> int:
        return int(self.requested_process_mode)

    @a3_mode.setter
    def a3_mode(self, value: int) -> None:
        self.requested_process_mode = value

    def e1_payload(self) -> bytes:
        payload = bytearray(16)
        payload[0] = 0x02
        payload[2:4] = self.e1_flow_parameter.to_bytes(2, "big")
        payload[15] = 0xE1
        return bytes(payload)


@dataclass(frozen=True)
class AutoDecision:
    request: DisplayRequest
    raw: bytes
    label: str
    reason: str
    delay_after_ms: int


class DisplayAutoController:
    """Pure Display-side cadence/state model; it does not perform UART I/O."""

    _CADENCE = (
        ("AA00", 80),
        ("A5", 80),
        ("E5 B2", 80),
        ("E5 E1", 130),
        ("A5", 30),
        ("A3", 0),
    )

    def __init__(self, config: "DisplayRoleConfig") -> None:
        self.running = False
        self.phase = 0
        self.next_due = 0.0
        self.last_decision_timestamp: float | None = None
        self.last_decision_reason = ""
        self.last_safety_decision = ""
        self.cycle_number = 0
        self.scheduler_state = "STOPPED"
        self.late_by_ms = 0.0
        self.safety_rules = [
            SafetyRule(
                "External tank empty safety policy",
                SafetyAction.ABORT_DISPENSE,
                enabled=True,
                experimental=False,
                required_mapping="E5 B2 Data[4] bit0",
            ),
            SafetyRule(
                "Internal HIGH process-stop policy",
                SafetyAction.ABORT_FILTER,
                enabled=True,
                experimental=False,
                required_mapping="E5 B2 Data[4] bit5",
            ),
        ]
        self.configure(config)

    def configure(self, config: "DisplayRoleConfig") -> None:
        self.enabled = config.auto_aa_enabled
        self.mode = config.auto_aa_mode
        if not hasattr(self, "state"):
            self.state = DisplayState(
                temperature=config.auto_temperature,
                volume_ml=config.auto_volume_ml,
                dispense_command=config.auto_dispense_command,
                process_mode=0x68,
                requested_process_mode=config.auto_a3_mode,
                a3_phase=config.auto_a3_phase,
                e1_flow_parameter=config.auto_e1_flow_parameter,
            )
        else:
            self.state.temperature = config.auto_temperature
            self.state.volume_ml = config.auto_volume_ml
            self.state.dispense_command = config.auto_dispense_command
            self.state.requested_process_mode = config.auto_a3_mode
            self.state.a3_phase = config.auto_a3_phase
            self.state.e1_flow_parameter = config.auto_e1_flow_parameter

    @property
    def automatic_active(self) -> bool:
        return self.enabled and self.mode in (
            AutoAAMode.AUTO,
            AutoAAMode.AUTO_MANUAL_INJECT,
        )

    @property
    def manual_override(self) -> bool:
        return self.enabled and self.mode is AutoAAMode.MANUAL_OVERRIDE

    def start(self, now: float) -> None:
        self.running = True
        self.next_due = now
        self.scheduler_state = "SEND_AA00"

    def stop(self) -> None:
        self.running = False
        self.scheduler_state = "STOPPED"

    def due(self, now: float) -> AutoDecision | None:
        if not self.running or not self.automatic_active or now < self.next_due:
            return None
        kind, delay_ms = self._CADENCE[self.phase]
        self.scheduler_state = f"SEND_{kind.replace(' ', '_')}"
        self.late_by_ms = max(0.0, (now - self.next_due) * 1000.0)
        if kind == "AA00":
            return AutoDecision(
                DisplayRequest.AA,
                build_aa_frame(self.state.aa00_payload()),
                "AUTO AA00",
                "periodic display state",
                delay_ms,
            )
        if kind == "A3":
            mode_name = {0x68: "BASE", 0x78: "FILTER", 0x7C: "FLUSH"}.get(
                self.state.a3_mode, f"0x{self.state.a3_mode:02X}"
            )
            return AutoDecision(
                DisplayRequest.AA,
                build_aa_frame(self.state.a3_payload()),
                f"AUTO A3 {mode_name}",
                "periodic display state",
                delay_ms,
            )
        request = {
            "A5": DisplayRequest.A5,
            "E5 B2": DisplayRequest.E5_B2,
            "E5 E1": DisplayRequest.E5_E1,
        }[kind]
        return AutoDecision(
            request,
            REQUEST_BYTES[request],
            f"POLL {request.value}",
            "real-display cadence",
            delay_ms,
        )

    def sent(self, decision: AutoDecision, now: float) -> None:
        self.phase = (self.phase + 1) % len(self._CADENCE)
        if self.phase == 0:
            self.cycle_number += 1
        self.next_due = now + decision.delay_after_ms / 1000.0
        next_kind = self._CADENCE[self.phase][0].replace(" ", "_")
        self.scheduler_state = f"WAIT_SLOT_{next_kind}"
        self.last_decision_timestamp = now
        self.last_decision_reason = decision.reason


REQUEST_BYTES = {
    DisplayRequest.A5: b"\xa5",
    DisplayRequest.E5_B2: b"\xe5\xb2",
    DisplayRequest.E5_E1: b"\xe5\xe1",
}
REQUEST_ORDER = tuple(DisplayRequest)
DEFAULT_AA_COMMAND_PRESETS_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "aa_command_presets.json"
)


def direction_labels(role: EmulatorRole) -> tuple[str, str]:
    """Return RX and TX labels for the selected physical role."""
    if role is EmulatorRole.DISPLAY:
        return "MAIN -> EMU(DISPLAY)", "EMU(DISPLAY) -> MAIN"
    return "DISPLAY -> EMU", "EMU -> DISPLAY"


def build_aa_frame(payload: bytes | bytearray) -> bytes:
    if len(payload) != 16:
        raise ValueError(f"AA requires 16 payload bytes, got {len(payload)}")
    return b"\xaa" + bytes(payload) + bytes(sum16_bytes(payload))


@dataclass
class AAPayloadPreset:
    name: str
    payload: bytes
    enabled: bool = True
    knowledge_class: str | None = None
    auto_checksum: bool = True
    checksum_high: int | None = None
    checksum_low: int | None = None
    description: str = ""

    def __post_init__(self) -> None:
        self.payload = bytes(self.payload)
        if len(self.payload) != 16:
            raise ValueError("AA payload preset must contain exactly 16 bytes")
        computed_high, computed_low = sum16_bytes(self.payload)
        if self.checksum_high is None:
            self.checksum_high = computed_high
        if self.checksum_low is None:
            self.checksum_low = computed_low
        if not 0 <= self.checksum_high <= 0xFF or not 0 <= self.checksum_low <= 0xFF:
            raise ValueError("AA checksum bytes must be in range 00..FF")

    def checksum(self) -> tuple[int, int]:
        if self.auto_checksum:
            return sum16_bytes(self.payload)
        return self.checksum_high, self.checksum_low

    def frame(self) -> bytes:
        return b"\xaa" + self.payload + bytes(self.checksum())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "payload": self.payload.hex(" ").upper(),
            "description": self.description,
            "auto_checksum": self.auto_checksum,
            "checksum": bytes((self.checksum_high, self.checksum_low)).hex(" ").upper(),
            **(
                {"knowledge_class": self.knowledge_class}
                if self.knowledge_class
                else {}
            ),
        }


def default_aa_presets() -> list[AAPayloadPreset]:
    return [
        AAPayloadPreset(
            "AA Control / Setpoint 00",
            bytes(16),
            True,
            "control_setpoint_00",
            description="AA control/setpoint command with Data[15]=00",
        ),
        AAPayloadPreset(
            "AA System / Process A3",
            bytes(15) + b"\xa3",
            True,
            "system_process_a3",
            description="AA system/process command with Data[15]=A3",
        ),
        AAPayloadPreset(
            "AA System / Process E1",
            bytes(15) + b"\xe1",
            True,
            "system_process_e1",
            description="AA system/process command with Data[15]=E1",
        ),
    ]


def default_live_value_fields() -> list[dict[str, Any]]:
    return [
        {"field": "a5_outlet_temperature", "format": "Decimal", "unit": "°C"},
        {"field": "a5_inlet_temperature", "format": "Decimal", "unit": "°C"},
        {"field": "a5_ac_mains_voltage", "format": "Decimal", "unit": "V"},
        {"field": "a5_current_dispensed_volume", "format": "Decimal", "unit": "ml"},
    ]


@dataclass
class DisplayRoleConfig:
    requests: dict[DisplayRequest, dict[str, int | bool]] = field(
        default_factory=lambda: {
            DisplayRequest.A5: {"enabled": True, "delay_ms": 80},
            DisplayRequest.E5_B2: {"enabled": True, "delay_ms": 80},
            DisplayRequest.E5_E1: {"enabled": True, "delay_ms": 130},
            DisplayRequest.AA: {"enabled": False, "delay_ms": 0},
        }
    )
    aa_presets: list[AAPayloadPreset] = field(default_factory=default_aa_presets)
    aa_round_robin: bool = True
    aa_hold_index: int = 0
    allow_aa_control: bool = False
    retry_aa: bool = False
    ack_timeout_ms: int = 100
    response_timeout_ms: int = 100
    auto_aa_enabled: bool = False
    auto_aa_mode: AutoAAMode = AutoAAMode.OFF
    auto_temperature: int = 25
    auto_volume_ml: int = 150
    auto_dispense_command: int = 0x80
    auto_a3_mode: int = 0x68
    auto_a3_phase: int = 0x00
    auto_e1_flow_parameter: int = 750
    live_value_fields: list[dict[str, Any]] = field(
        default_factory=default_live_value_fields
    )
    unsafe_process_safety_bypass: bool = False
    b2_freshness_ms: int = 1000

    @classmethod
    def from_dict(
        cls, values: object, *, safe_load: bool = False
    ) -> "DisplayRoleConfig":
        if not isinstance(values, dict):
            values = {}
        config = cls()
        raw_requests = values.get("requests", {})
        if isinstance(raw_requests, dict):
            for request in DisplayRequest:
                raw = raw_requests.get(
                    request.value, raw_requests.get(request.name, {})
                )
                if not isinstance(raw, dict):
                    continue
                config.requests[request] = {
                    "enabled": bool(
                        raw.get("enabled", config.requests[request]["enabled"])
                    ),
                    "delay_ms": max(
                        0,
                        int(raw.get("delay_ms", config.requests[request]["delay_ms"])),
                    ),
                }
        raw_presets = values.get("aa_payload_sequence", values.get("aa_presets", []))
        if isinstance(raw_presets, list) and raw_presets:
            presets = []
            for index, raw in enumerate(raw_presets):
                if not isinstance(raw, dict):
                    continue
                try:
                    payload = bytes.fromhex(str(raw.get("payload", "")))
                    checksum = bytes.fromhex(str(raw.get("checksum", "")))
                    checksum_high = raw.get("checksum_high")
                    checksum_low = raw.get("checksum_low")
                    if len(checksum) == 2:
                        checksum_high, checksum_low = checksum
                    knowledge_class = raw.get("knowledge_class")
                    if not knowledge_class and len(payload) == 16:
                        knowledge_class = {
                            0x00: "control_setpoint_00",
                            0xA3: "system_process_a3",
                            0xE1: "system_process_e1",
                        }.get(payload[15])
                    presets.append(
                        AAPayloadPreset(
                            str(raw.get("name", f"AA Payload {index + 1}")),
                            payload,
                            bool(raw.get("enabled", True)),
                            knowledge_class,
                            bool(raw.get("auto_checksum", True)),
                            None if checksum_high is None else int(checksum_high),
                            None if checksum_low is None else int(checksum_low),
                            str(raw.get("description", "")),
                        )
                    )
                except ValueError:
                    continue
            if presets:
                config.aa_presets = presets
        config.aa_round_robin = bool(values.get("aa_round_robin", True))
        config.aa_hold_index = max(0, int(values.get("aa_hold_index", 0)))
        config.allow_aa_control = (
            False if safe_load else bool(values.get("allow_aa_control", False))
        )
        config.retry_aa = bool(values.get("retry_aa", False))
        config.ack_timeout_ms = max(1, int(values.get("ack_timeout_ms", 100)))
        config.response_timeout_ms = max(1, int(values.get("response_timeout_ms", 100)))
        try:
            config.auto_aa_mode = AutoAAMode(
                str(values.get("auto_aa_mode", AutoAAMode.OFF.value))
            )
        except ValueError:
            config.auto_aa_mode = AutoAAMode.OFF
        config.auto_aa_enabled = bool(values.get("auto_aa_enabled", False))
        config.auto_temperature = min(255, max(0, int(values.get("auto_temperature", 25))))
        config.auto_volume_ml = min(65535, max(0, int(values.get("auto_volume_ml", 150))))
        config.auto_dispense_command = int(values.get("auto_dispense_command", 0x80)) & 0xFF
        config.auto_a3_mode = int(values.get("auto_a3_mode", 0x68)) & 0xFF
        config.auto_a3_phase = int(values.get("auto_a3_phase", 0)) & 0xFF
        config.auto_e1_flow_parameter = min(
            65535, max(0, int(values.get("auto_e1_flow_parameter", 750)))
        )
        raw_live_fields = values.get("live_value_fields")
        if isinstance(raw_live_fields, list):
            config.live_value_fields = [
                {
                    "field": str(entry.get("field", "")),
                    "format": str(entry.get("format", "Decimal")),
                    "unit": str(entry.get("unit", "")),
                }
                for entry in raw_live_fields
                if isinstance(entry, dict) and entry.get("field")
            ]
        config.unsafe_process_safety_bypass = bool(
            values.get("unsafe_process_safety_bypass", False)
        )
        config.b2_freshness_ms = min(
            10000, max(100, int(values.get("b2_freshness_ms", 1000)))
        )
        if safe_load:
            config.requests[DisplayRequest.AA]["enabled"] = False
            config.auto_aa_enabled = False
            config.auto_aa_mode = AutoAAMode.OFF
            config.unsafe_process_safety_bypass = False
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": {
                request.value: dict(settings)
                for request, settings in self.requests.items()
            },
            "aa_payload_sequence": [preset.to_dict() for preset in self.aa_presets],
            "aa_round_robin": self.aa_round_robin,
            "aa_hold_index": self.aa_hold_index,
            "allow_aa_control": self.allow_aa_control,
            "retry_aa": self.retry_aa,
            "ack_timeout_ms": self.ack_timeout_ms,
            "response_timeout_ms": self.response_timeout_ms,
            "auto_aa_enabled": self.auto_aa_enabled,
            "auto_aa_mode": self.auto_aa_mode.value,
            "auto_temperature": self.auto_temperature,
            "auto_volume_ml": self.auto_volume_ml,
            "auto_dispense_command": self.auto_dispense_command,
            "auto_a3_mode": self.auto_a3_mode,
            "auto_a3_phase": self.auto_a3_phase,
            "auto_e1_flow_parameter": self.auto_e1_flow_parameter,
            "live_value_fields": [dict(entry) for entry in self.live_value_fields],
            "unsafe_process_safety_bypass": self.unsafe_process_safety_bypass,
            "b2_freshness_ms": self.b2_freshness_ms,
            "auto_polling": False,
        }


def save_aa_command_presets(
    presets: list[AAPayloadPreset],
    path: Path | str = DEFAULT_AA_COMMAND_PRESETS_PATH,
) -> None:
    """Atomically persist the reusable AA command library."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    document = {
        "schema_version": 1,
        "description": "Reusable Display Emulator AA command presets",
        "aa_commands": [preset.to_dict() for preset in presets],
    }
    temporary.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def load_aa_command_presets(
    path: Path | str = DEFAULT_AA_COMMAND_PRESETS_PATH,
) -> list[AAPayloadPreset]:
    """Load the AA command library, creating it with defaults when absent."""
    target = Path(path)
    if not target.exists():
        presets = default_aa_presets()
        save_aa_command_presets(presets, target)
        return presets
    document = json.loads(target.read_text(encoding="utf-8"))
    raw_presets = (
        document.get("aa_commands", [])
        if isinstance(document, dict)
        else document
    )
    if not isinstance(raw_presets, list):
        raise ValueError("AA command preset JSON must contain an aa_commands list")
    config = DisplayRoleConfig.from_dict({"aa_payload_sequence": raw_presets})
    return config.aa_presets


class DisplayRequestScheduler:
    """Configurable ordered scheduler; delays apply after the sent request."""

    def __init__(self, config: DisplayRoleConfig) -> None:
        self.config = config
        self.running = False
        self.index = 0
        self.next_due = 0.0

    def start(self, now: float) -> None:
        self.running = True
        self.next_due = now

    def stop(self) -> None:
        self.running = False

    def _next_enabled(self, allow_aa: bool) -> DisplayRequest | None:
        for _ in REQUEST_ORDER:
            request = REQUEST_ORDER[self.index % len(REQUEST_ORDER)]
            self.index = (self.index + 1) % len(REQUEST_ORDER)
            settings = self.config.requests[request]
            if not bool(settings["enabled"]):
                continue
            if request is DisplayRequest.AA and not allow_aa:
                continue
            return request
        return None

    def due(self, now: float, allow_aa: bool) -> DisplayRequest | None:
        if not self.running or now < self.next_due:
            return None
        return self._next_enabled(allow_aa)

    def step(self, allow_aa: bool) -> DisplayRequest | None:
        return self._next_enabled(allow_aa)

    def sent(self, request: DisplayRequest, now: float) -> None:
        delay_ms = int(self.config.requests[request]["delay_ms"])
        self.next_due = now + delay_ms / 1000.0


@dataclass(frozen=True)
class DisplayRoleEvent:
    event: str
    request: DisplayRequest | None = None
    raw: bytes = b""
    status: str = ""
    latency_ms: float | None = None
    checksum_valid: bool | None = None
    source: str = ""
    reason: str = ""


@dataclass
class _PendingRequest:
    request: DisplayRequest
    sent_at: float
    transmitted: bytes = b""
    buffer: bytearray = field(default_factory=bytearray)
    source: str = ""
    reason: str = ""
    process_mode_target: int | None = None


@dataclass(frozen=True)
class _QueuedAA:
    raw: bytes
    source: str
    status: str
    reason: str


class DisplayRoleController:
    """Pure role state used by the serial worker and unit tests."""

    def __init__(self, config: DisplayRoleConfig | None = None) -> None:
        self.config = config or DisplayRoleConfig()
        self.scheduler = DisplayRequestScheduler(self.config)
        self.main_state = MainState()
        self.auto_controller = DisplayAutoController(self.config)
        self.pending: _PendingRequest | None = None
        self.manual_aa_queue: list[_QueuedAA] = []
        self.emergency_aa_queue: list[_QueuedAA] = []
        self.last_tx_at: float | None = None
        self.tx_intervals_ms: list[float] = []
        self.actual_inter_frame_gap_ms: float | None = None
        self.display_powered_on = False
        self.display_powered_on_at: float | None = None
        self.initial_fill_evaluated = False
        self.aa_cursor = 0
        self.latencies: dict[DisplayRequest, list[float]] = {
            request: [] for request in DisplayRequest
        }
        self.timeouts: dict[DisplayRequest, int] = {
            request: 0 for request in DisplayRequest
        }

    def reconfigure(self, config: DisplayRoleConfig) -> None:
        was_running = self.scheduler.running
        index = self.scheduler.index
        next_due = self.scheduler.next_due
        self.config = config
        self.scheduler = DisplayRequestScheduler(config)
        self.scheduler.running = was_running
        self.scheduler.index = index
        self.scheduler.next_due = next_due
        self.auto_controller.configure(config)

    def start(self, now: float) -> None:
        self.scheduler.start(now)
        self.auto_controller.start(now)
        self.display_powered_on = True
        self.display_powered_on_at = now
        self.initial_fill_evaluated = False
        self.auto_controller.state.initial_fill_active = False
        self.auto_controller.state.dispense_allowed = True
        self._evaluate_initial_fill(now)

    def stop(self) -> None:
        self.scheduler.stop()
        self.auto_controller.stop()
        self.display_powered_on = False
        self.display_powered_on_at = None

    @property
    def has_queued_tx(self) -> bool:
        return bool(self.emergency_aa_queue or self.manual_aa_queue)

    def _aa_frame(self) -> bytes | None:
        enabled = [preset for preset in self.config.aa_presets if preset.enabled]
        if not enabled:
            return None
        if self.config.aa_round_robin:
            preset = enabled[self.aa_cursor % len(enabled)]
            self.aa_cursor = (self.aa_cursor + 1) % len(enabled)
        else:
            preset = self.config.aa_presets[
                min(self.config.aa_hold_index, len(self.config.aa_presets) - 1)
            ]
            if not preset.enabled:
                return None
        return preset.frame()

    @staticmethod
    def _aa_process_mode(raw: bytes) -> int | None:
        """Decode A3 process mode from D4; D5 is not part of mode detection."""
        if len(raw) == 19 and raw[0] == 0xAA and raw[16] == 0xA3:
            mode = raw[5]
            if mode in (0x68, 0x78, 0x7C):
                return mode
        return None

    @staticmethod
    def _aa_requests_dispense(raw: bytes) -> bool:
        return (
            len(raw) == 19
            and raw[0] == 0xAA
            and raw[2] == 0x01
            and raw[16] == 0x00
        )

    def _queue_initial_fill_filter(self) -> None:
        if any(item.source == "initial fill" for item in self.emergency_aa_queue):
            return
        if self.pending is not None and self.pending.source == "initial fill":
            return
        payload = bytearray(16)
        payload[4] = 0x78
        payload[5] = 0x00
        payload[15] = 0xA3
        self.emergency_aa_queue.append(
            _QueuedAA(
                build_aa_frame(payload),
                "initial fill",
                "INITIAL FILL FILTER -> SENT",
                "Internal Tank BELOW LOW at display power-on",
            )
        )

    def _evaluate_initial_fill(self, now: float) -> None:
        if not self.display_powered_on:
            return
        below_low = self.main_state.internal_below_low
        if below_low is None:
            return
        sensor_timestamp = max(
            timestamp
            for timestamp in (
                self.main_state.a5_timestamp,
                self.main_state.b2_timestamp,
            )
            if timestamp is not None
        )
        if (
            not self.initial_fill_evaluated
            and self.display_powered_on_at is not None
            and sensor_timestamp < self.display_powered_on_at
        ):
            return
        state = self.auto_controller.state
        if not self.initial_fill_evaluated:
            self.initial_fill_evaluated = True
            state.initial_fill_active = below_low
        state.dispense_allowed = not (state.initial_fill_active and below_low)
        if not state.initial_fill_active:
            return
        state.requested_process_mode = 0x78
        self.config.auto_a3_mode = 0x78
        if (
            state.process_mode == 0x68
            and self.config.allow_aa_control
            and self._start_inhibit_reason(0x78, now) is None
        ):
            self._queue_initial_fill_filter()

    def _b2_is_fresh(self, now: float) -> bool:
        timestamp = self.main_state.b2_timestamp
        return bool(
            self.main_state.b2_valid
            and timestamp is not None
            and (now - timestamp) * 1000.0 <= self.config.b2_freshness_ms
        )

    def _start_inhibit_reason(self, mode: int, now: float) -> str | None:
        if self.config.unsafe_process_safety_bypass:
            return None
        if not self._b2_is_fresh(now):
            return "B2 stale or unavailable"
        if not self.main_state.external_tank_available:
            return "External tank empty"
        if mode == 0x78 and self.main_state.internal_high_sensor:
            return "Internal Tank HIGH"
        return None

    def filter_start_allowed(self, now: float) -> bool:
        return self._start_inhibit_reason(0x78, now) is None

    def flush_start_allowed(self, now: float) -> bool:
        return self._start_inhibit_reason(0x7C, now) is None

    def _queue_process_safety_abort(self, reason: str) -> None:
        if self.config.unsafe_process_safety_bypass:
            return
        if any(item.source == "safety" for item in self.emergency_aa_queue):
            return
        if self.pending is not None and self.pending.source == "safety":
            return
        payload = bytearray(16)
        payload[4] = 0x68
        payload[5] = 0x02
        payload[15] = 0xA3
        mode_name = {0x78: "FILTER", 0x7C: "FLUSH"}.get(
            self.auto_controller.state.process_mode, "PROCESS"
        )
        self.emergency_aa_queue.append(
            _QueuedAA(
                build_aa_frame(payload),
                "safety",
                f"SAFETY {mode_name} STOP -> SENT",
                reason,
            )
        )
        self.auto_controller.last_safety_decision = (
            f"{mode_name} -> BASE: {reason}"
        )

    def _evaluate_process_safety(self, now: float) -> None:
        if self.config.unsafe_process_safety_bypass or not self._b2_is_fresh(now):
            return
        mode = self.auto_controller.state.process_mode
        if mode == 0x78:
            if self.main_state.internal_high_sensor:
                self._queue_process_safety_abort("Internal Tank HIGH")
            elif not self.main_state.external_tank_available:
                self._queue_process_safety_abort("External tank empty")
        elif mode == 0x7C and not self.main_state.external_tank_available:
            self._queue_process_safety_abort("External tank empty")

    def _record_tx(self, now: float) -> None:
        if self.last_tx_at is not None:
            interval_ms = max(0.0, (now - self.last_tx_at) * 1000.0)
            self.actual_inter_frame_gap_ms = interval_ms
            self.tx_intervals_ms.append(interval_ms)
            if len(self.tx_intervals_ms) > 10000:
                del self.tx_intervals_ms[:1000]
        self.last_tx_at = now

    def _begin_transaction(
        self,
        request: DisplayRequest,
        raw: bytes,
        now: float,
        status: str,
        source: str,
        reason: str = "",
        *,
        update_poll_scheduler: bool = True,
    ) -> DisplayRoleEvent:
        if (
            self._aa_requests_dispense(raw)
            and not self.auto_controller.state.dispense_allowed
        ):
            return DisplayRoleEvent(
                "blocked",
                request,
                raw,
                "DISPENSE INHIBITED — INITIAL FILL",
                source=source,
                reason="Internal Tank BELOW LOW",
            )
        self._record_tx(now)
        process_mode_target = self._aa_process_mode(raw)
        self.pending = _PendingRequest(
            request,
            now,
            raw,
            source=source,
            reason=reason,
            process_mode_target=process_mode_target,
        )
        if self.auto_controller.automatic_active and source.startswith("auto"):
            if request is DisplayRequest.AA:
                wait_name = "AA00" if "AA00" in status else "A3"
            else:
                wait_name = request.value.replace(" ", "_")
            self.auto_controller.scheduler_state = f"WAIT_{wait_name}_RESPONSE"
        if process_mode_target in (0x78, 0x7C):
            self.auto_controller.state.process_mode = process_mode_target
            self._evaluate_process_safety(now)
        if update_poll_scheduler:
            self.scheduler.sent(request, now)
        return DisplayRoleEvent(
            "tx", request, raw, status, source=source, reason=reason
        )

    def send_request(
        self,
        request: DisplayRequest,
        now: float,
        source: str = "manual",
        reason: str = "",
    ) -> DisplayRoleEvent:
        if self.pending is not None:
            return DisplayRoleEvent(
                "busy", request, status="WAITING FOR RESPONSE", source=source
            )
        if request is DisplayRequest.AA:
            if not self.config.allow_aa_control:
                return DisplayRoleEvent(
                    "blocked", request, status="AA SAFETY BLOCKED", source=source
                )
            raw = self._aa_frame()
            if raw is None:
                return DisplayRoleEvent(
                    "blocked", request, status="NO ENABLED AA PAYLOAD", source=source
                )
        else:
            raw = REQUEST_BYTES[request]
        process_mode = self._aa_process_mode(raw)
        if process_mode in (0x78, 0x7C) and source in ("scheduler", "step"):
            inhibit_reason = self._start_inhibit_reason(process_mode, now)
            if inhibit_reason is not None:
                mode_name = "FILTER" if process_mode == 0x78 else "FLUSH"
                self.auto_controller.last_safety_decision = (
                    f"{mode_name} start inhibited: {inhibit_reason}"
                )
                return DisplayRoleEvent(
                    "blocked",
                    request,
                    raw,
                    f"{mode_name} START INHIBITED",
                    source=source,
                    reason=inhibit_reason,
                )
        return self._begin_transaction(
            request, raw, now, "SENT", source, reason
        )

    def send_aa_preset(
        self, preset_index: int, now: float, source: str = "manual selected"
    ) -> DisplayRoleEvent:
        """Send exactly one selected AA preset, independent of scheduler settings."""
        if self.pending is not None:
            return DisplayRoleEvent(
                "busy",
                DisplayRequest.AA,
                status="WAITING FOR RESPONSE",
                source=source,
            )
        if not self.config.allow_aa_control:
            return DisplayRoleEvent(
                "blocked",
                DisplayRequest.AA,
                status="AA SAFETY BLOCKED",
                source=source,
            )
        if not 0 <= preset_index < len(self.config.aa_presets):
            return DisplayRoleEvent(
                "blocked",
                DisplayRequest.AA,
                status="INVALID AA PAYLOAD SELECTION",
                source=source,
            )
        raw = self.config.aa_presets[preset_index].frame()
        return self._begin_transaction(
            DisplayRequest.AA,
            raw,
            now,
            "SENT SELECTED ONCE",
            source,
            "manual one-shot",
        )

    def queue_manual_aa_preset(self, preset_index: int) -> DisplayRoleEvent:
        if not self.config.allow_aa_control:
            return DisplayRoleEvent(
                "blocked",
                DisplayRequest.AA,
                status="AA SAFETY BLOCKED",
                source="manual",
            )
        if not 0 <= preset_index < len(self.config.aa_presets):
            return DisplayRoleEvent(
                "blocked",
                DisplayRequest.AA,
                status="INVALID AA PAYLOAD SELECTION",
                source="manual",
            )
        if (
            self.auto_controller.enabled
            and self.auto_controller.mode is AutoAAMode.AUTO
        ):
            return DisplayRoleEvent(
                "blocked",
                DisplayRequest.AA,
                status="MANUAL AA BLOCKED IN AUTO MODE",
                source="manual",
                reason="select AUTO + MANUAL INJECT or MANUAL OVERRIDE",
            )
        preset = self.config.aa_presets[preset_index]
        self.manual_aa_queue.append(
            _QueuedAA(
                preset.frame(),
                "manual",
                "MANUAL AA -> SENT",
                preset.name,
            )
        )
        return DisplayRoleEvent(
            "queued",
            DisplayRequest.AA,
            preset.frame(),
            "MANUAL AA -> QUEUED",
            source="manual",
            reason=preset.name,
        )

    def queue_abort_water_process(self) -> DisplayRoleEvent:
        if not self.config.allow_aa_control:
            return DisplayRoleEvent(
                "blocked",
                DisplayRequest.AA,
                status="SAFETY ABORT BLOCKED — ENABLE AA CONTROL FIRST",
                source="safety",
                reason="AA hardware safety gate is locked",
            )
        payload = bytearray(16)
        payload[4] = 0x68
        payload[5] = 0x02
        payload[15] = 0xA3
        raw = build_aa_frame(payload)
        self.auto_controller.state.a3_mode = 0x68
        self.auto_controller.state.a3_phase = 0x00
        self.config.auto_a3_mode = 0x68
        self.config.auto_a3_phase = 0x00
        self.emergency_aa_queue.clear()
        self.emergency_aa_queue.append(
            _QueuedAA(
                raw,
                "safety",
                "SAFETY A3 BASE -> SENT",
                "operator ABORT WATER PROCESS",
            )
        )
        return DisplayRoleEvent(
            "queued",
            DisplayRequest.AA,
            raw,
            "SAFETY ABORT -> QUEUED",
            source="safety",
            reason="operator ABORT WATER PROCESS",
        )

    def _send_queued_aa(self, queued: _QueuedAA, now: float) -> DisplayRoleEvent:
        if not self.config.allow_aa_control:
            return DisplayRoleEvent(
                "blocked",
                DisplayRequest.AA,
                queued.raw,
                "QUEUED AA SAFETY BLOCKED",
                source=queued.source,
                reason=queued.reason,
            )
        return self._begin_transaction(
            DisplayRequest.AA,
            queued.raw,
            now,
            queued.status,
            queued.source,
            queued.reason,
        )

    def step(self, now: float) -> DisplayRoleEvent:
        request = self.scheduler.step(self.config.allow_aa_control)
        if request is None:
            return DisplayRoleEvent("idle", status="NO ENABLED REQUEST")
        return self.send_request(request, now, "step")

    def tick(self, now: float) -> list[DisplayRoleEvent]:
        events: list[DisplayRoleEvent] = []
        if self.pending is not None:
            timeout_ms = (
                self.config.ack_timeout_ms
                if self.pending.request is DisplayRequest.AA
                else self.config.response_timeout_ms
            )
            if (now - self.pending.sent_at) * 1000.0 >= timeout_ms:
                request = self.pending.request
                transmitted = self.pending.transmitted
                source = self.pending.source
                reason = self.pending.reason
                process_mode_target = self.pending.process_mode_target
                partial = bytes(self.pending.buffer)
                self.pending = None
                if self.auto_controller.automatic_active:
                    next_kind = self.auto_controller._CADENCE[
                        self.auto_controller.phase
                    ][0].replace(" ", "_")
                    self.auto_controller.scheduler_state = f"WAIT_SLOT_{next_kind}"
                self.timeouts[request] += 1
                if request is not DisplayRequest.AA and partial:
                    events.append(
                        DisplayRoleEvent(
                            "timeout",
                            request,
                            partial,
                            "UNEXPECTED LENGTH",
                        )
                    )
                else:
                    events.append(
                        DisplayRoleEvent(
                            "timeout",
                            request,
                            status=(
                                "ACK TIMEOUT"
                                if request is DisplayRequest.AA
                                else "TIMEOUT"
                            ),
                            source=source,
                            reason=reason,
                        )
                    )
                if (
                    request is DisplayRequest.AA
                    and self.config.retry_aa
                    and self.config.allow_aa_control
                ):
                    self._record_tx(now)
                    self.pending = _PendingRequest(
                        DisplayRequest.AA,
                        now,
                        transmitted,
                        source="retry",
                        reason=reason,
                        process_mode_target=process_mode_target,
                    )
                    self.scheduler.sent(DisplayRequest.AA, now)
                    events.append(
                        DisplayRoleEvent(
                            "tx",
                            DisplayRequest.AA,
                            transmitted,
                            "RETRY SENT",
                            source="retry",
                            reason=reason,
                        )
                    )
            return events
        self._evaluate_initial_fill(now)
        self._evaluate_process_safety(now)
        if self.emergency_aa_queue:
            events.append(self._send_queued_aa(self.emergency_aa_queue.pop(0), now))
            return events
        if self.manual_aa_queue:
            events.append(self._send_queued_aa(self.manual_aa_queue.pop(0), now))
            return events

        decision = self.auto_controller.due(now)
        if decision is not None:
            self.auto_controller.sent(decision, now)
            if decision.request is DisplayRequest.AA:
                if not self.config.allow_aa_control:
                    events.append(
                        DisplayRoleEvent(
                            "blocked",
                            DisplayRequest.AA,
                            decision.raw,
                            "AUTO AA SAFETY BLOCKED",
                            source="auto",
                            reason=decision.reason,
                        )
                    )
                else:
                    process_mode = self._aa_process_mode(decision.raw)
                    inhibit_reason = (
                        self._start_inhibit_reason(process_mode, now)
                        if process_mode in (0x78, 0x7C)
                        else None
                    )
                    if inhibit_reason is not None:
                        mode_name = "FILTER" if process_mode == 0x78 else "FLUSH"
                        self.auto_controller.last_safety_decision = (
                            f"{mode_name} start inhibited: {inhibit_reason}"
                        )
                        events.append(
                            DisplayRoleEvent(
                                "blocked",
                                DisplayRequest.AA,
                                decision.raw,
                                f"AUTO {mode_name} START INHIBITED",
                                source="auto",
                                reason=inhibit_reason,
                            )
                        )
                        return events
                    events.append(
                        self._begin_transaction(
                            DisplayRequest.AA,
                            decision.raw,
                            now,
                            f"{decision.label} -> SENT",
                            "auto",
                            decision.reason,
                            update_poll_scheduler=False,
                        )
                    )
            else:
                events.append(
                    self._begin_transaction(
                        decision.request,
                        decision.raw,
                        now,
                        f"{decision.label} -> SENT",
                        "auto cadence",
                        decision.reason,
                        update_poll_scheduler=False,
                    )
                )
            return events

        if not self.auto_controller.automatic_active:
            request = self.scheduler.due(
                now,
                self.config.allow_aa_control
                and not self.auto_controller.manual_override,
            )
            if request is not None:
                events.append(self.send_request(request, now, "scheduler"))
        return events

    def receive(self, chunk: bytes, now: float) -> list[DisplayRoleEvent]:
        raw = bytes(chunk)
        if not raw:
            return []
        if self.pending is None:
            return [DisplayRoleEvent("unexpected", raw=raw, status="UNKNOWN RESPONSE")]
        pending = self.pending
        if pending.request is DisplayRequest.AA:
            self.pending = None
            if self.auto_controller.automatic_active:
                next_kind = self.auto_controller._CADENCE[
                    self.auto_controller.phase
                ][0].replace(" ", "_")
                self.auto_controller.scheduler_state = f"WAIT_SLOT_{next_kind}"
            latency = (now - pending.sent_at) * 1000.0
            if raw[:1] == b"\xcc":
                self.latencies[DisplayRequest.AA].append(latency)
                if pending.process_mode_target == 0x68:
                    self.auto_controller.state.process_mode = 0x68
                    self.auto_controller.state.requested_process_mode = 0x68
                    self.config.auto_a3_mode = 0x68
                    if pending.reason != "External tank empty":
                        self.auto_controller.state.initial_fill_active = False
                    self.auto_controller.state.dispense_allowed = not bool(
                        self.auto_controller.state.initial_fill_active
                        and self.main_state.internal_below_low
                    )
                self._evaluate_process_safety(now)
                events = [
                    DisplayRoleEvent(
                        "rx",
                        DisplayRequest.AA,
                        b"\xcc",
                        "ACK RECEIVED",
                        latency,
                        source=pending.source,
                        reason=(pending.reason + "; CC received").strip("; "),
                    )
                ]
                if len(raw) > 1:
                    events.append(
                        DisplayRoleEvent(
                            "unexpected", raw=raw[1:], status="UNEXPECTED RESPONSE"
                        )
                    )
                return events
            return [
                DisplayRoleEvent(
                    "unexpected", DisplayRequest.AA, raw, "UNEXPECTED RESPONSE", latency
                )
            ]
        pending.buffer.extend(raw)
        if len(pending.buffer) < 18:
            return []
        response = bytes(pending.buffer[:18])
        remainder = bytes(pending.buffer[18:])
        self.pending = None
        if self.auto_controller.automatic_active:
            next_kind = self.auto_controller._CADENCE[
                self.auto_controller.phase
            ][0].replace(" ", "_")
            self.auto_controller.scheduler_state = f"WAIT_SLOT_{next_kind}"
        latency = (now - pending.sent_at) * 1000.0
        valid = validate_response(response)
        if valid:
            self.main_state.update(pending.request, response[:16], now)
            if pending.request in (DisplayRequest.A5, DisplayRequest.E5_B2):
                self._evaluate_initial_fill(now)
            if pending.request is DisplayRequest.E5_B2:
                self._evaluate_process_safety(now)
        self.latencies[pending.request].append(latency)
        status = "VALID SUM16" if valid else "INVALID SUM16"
        events = [
            DisplayRoleEvent(
                "rx",
                pending.request,
                response,
                status,
                latency,
                valid,
                pending.source,
                pending.reason,
            )
        ]
        if remainder:
            events.append(
                DisplayRoleEvent(
                    "unexpected", raw=remainder, status="UNEXPECTED LENGTH"
                )
            )
        return events

    def snapshot(self, now: float) -> dict[str, Any]:
        state = self.auto_controller.state
        next_tx_ms = None
        if self.auto_controller.running and self.auto_controller.automatic_active:
            next_tx_ms = max(0.0, (self.auto_controller.next_due - now) * 1000.0)
        return {
            "main_state": self.main_state.snapshot(now),
            "auto_aa": {
                "enabled": self.auto_controller.enabled,
                "mode": self.auto_controller.mode.value,
                "running": self.auto_controller.running,
                "aa00_state": {
                    "temperature": state.temperature,
                    "volume_ml": state.volume_ml,
                    "dispense_command": f"0x{state.dispense_command:02X}",
                },
                "a3_mode": f"0x{state.a3_mode:02X}",
                "process_mode": f"0x{state.process_mode:02X}",
                "initial_fill_active": state.initial_fill_active,
                "dispense_allowed": state.dispense_allowed,
                "a3_phase": f"0x{state.a3_phase:02X}",
                "e1_flow_parameter": state.e1_flow_parameter,
                "pending_manual_aa": len(self.manual_aa_queue),
                "pending_safety_action": len(self.emergency_aa_queue),
                "last_decision_timestamp": self.auto_controller.last_decision_timestamp,
                "last_decision_reason": self.auto_controller.last_decision_reason,
                "last_safety_decision": self.auto_controller.last_safety_decision,
                "unsafe_process_safety_bypass": self.config.unsafe_process_safety_bypass,
                "filter_start_allowed": self.filter_start_allowed(now),
                "flush_start_allowed": self.flush_start_allowed(now),
                "scheduler_state": self.auto_controller.scheduler_state,
                "cycle_number": self.auto_controller.cycle_number,
                "expected_next_tx_ms": next_tx_ms,
                "actual_inter_frame_gap_ms": self.actual_inter_frame_gap_ms,
                "late_by_ms": self.auto_controller.late_by_ms,
                "safety_rules": [
                    {
                        "name": rule.name,
                        "action": rule.action.value,
                        "enabled": rule.enabled,
                        "experimental": rule.experimental,
                        "required_mapping": rule.required_mapping,
                    }
                    for rule in self.auto_controller.safety_rules
                ],
            },
        }

    def statistics(self) -> dict[str, dict[str, float | int]]:
        result = {}
        for request in DisplayRequest:
            values = self.latencies[request]
            result[request.value] = {
                "count": len(values),
                "timeout_count": self.timeouts[request],
                "minimum_ms": min(values) if values else 0.0,
                "maximum_ms": max(values) if values else 0.0,
                "average_ms": statistics.fmean(values) if values else 0.0,
                "median_ms": statistics.median(values) if values else 0.0,
                "jitter_ms": statistics.pstdev(values) if len(values) > 1 else 0.0,
            }
        intervals = self.tx_intervals_ms
        result["TX-to-TX"] = {
            "count": len(intervals),
            "timeout_count": sum(value > 750.0 for value in intervals),
            "minimum_ms": min(intervals) if intervals else 0.0,
            "maximum_ms": max(intervals) if intervals else 0.0,
            "average_ms": statistics.fmean(intervals) if intervals else 0.0,
            "median_ms": statistics.median(intervals) if intervals else 0.0,
            "jitter_ms": statistics.pstdev(intervals) if len(intervals) > 1 else 0.0,
        }
        return result
