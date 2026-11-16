"""Non-blocking GUI serial transport implemented in a dedicated QThread."""

from __future__ import annotations

import queue
import struct
import time

from PySide6.QtCore import QThread, Signal

from protocol.emulator import EmulatorCore
from protocol.display_role import (
    DisplayRequest,
    DisplayRoleConfig,
    DisplayRoleController,
    DisplayRoleEvent,
    EmulatorRole,
    direction_labels,
)
from protocol.frames import Dataset, RequestResponseSet
from protocol.checksum import validate_aa_frame, validate_response
from protocol.mega_bridge import (
    BridgeDirection,
    DispenseOverrideEvent,
    DispenseSessionOrigin,
    MegaBridgeLink,
    MegaConfig,
    MegaMessageType,
    MegaRule,
    TankConfig,
    TankEvent,
    classify_main_frame,
    decode_dispense_override_event,
    decode_dispense_override_status,
    decode_manual_command_event,
    decode_replace_event,
    decode_rule_status,
    decode_rx_payload,
    decode_tank_event,
    decode_tank_status,
)
from protocol.parser import StreamParser
from protocol.replay import ReplayEngine, ReplayMode, ReplayProfile
from protocol.tds_mapping import TdsMappingSequencer


class _MegaInjectionPort:
    """write()-compatible adapter used by the existing controllers."""

    def __init__(self, serial_port, link: MegaBridgeLink, worker: "SerialWorker") -> None:
        self.serial_port = serial_port
        self.link = link
        self.worker = worker
        self.ready = False

    def inject(self, raw: bytes, direction: BridgeDirection) -> int:
        if not self.ready:
            self.worker.bridge_event.emit(
                {"event": "ERROR", "detail": "Mega handshake not complete; injection rejected"}
            )
            return 0
        try:
            sequence, packet = self.link.inject(direction, raw)
        except ValueError as exc:
            self.worker.bridge_event.emit({"event": "ERROR", "detail": str(exc)})
            return 0
        self.serial_port.write(packet)
        self.worker.bridge_event.emit(
            {
                "event": "INJECT_REQUESTED",
                "sequence": sequence,
                "direction": direction.name,
                "raw": bytes(raw).hex(" ").upper(),
            }
        )
        return sequence

    def write(self, raw: bytes) -> int:
        direction = (
            BridgeDirection.MAIN_TO_DISPLAY
            if self.worker._emulator_role is EmulatorRole.MAIN_MCU
            else BridgeDirection.DISPLAY_TO_MAIN
        )
        self.inject(bytes(raw), direction)
        return len(raw)

    def flush(self) -> None:
        self.serial_port.flush()

    def configure(self, config: MegaConfig) -> None:
        self.worker.mega_config = config
        if not self.ready:
            return
        ignored, packet = self.link.set_config(config)
        self.serial_port.write(packet)

    def set_rule(self, rule: MegaRule) -> None:
        if not self.ready:
            self.worker.bridge_event.emit({"event": "RULE_ERROR", "detail": "Mega handshake not complete"})
            return
        ignored, packet = self.link.set_rule(rule)
        self.serial_port.write(packet)

    def rule_command(self, command: MegaMessageType) -> None:
        if not self.ready:
            self.worker.bridge_event.emit({"event": "RULE_ERROR", "detail": "Mega handshake not complete"})
            return
        ignored, packet = self.link.rule_command(command)
        self.serial_port.write(packet)

    def command_dispense(self, volume_ml: int, feed_pump_parameter: int) -> None:
        if not self.ready:
            self.worker.bridge_event.emit({"event": "ERROR", "detail": "Mega handshake not complete"})
            return
        try:
            ignored, packet = self.link.command_dispense(volume_ml, feed_pump_parameter)
        except ValueError as exc:
            self.worker.bridge_event.emit({"event": "ERROR", "detail": str(exc)})
            return
        self.serial_port.write(packet)

    def stop_remote_dispense(self) -> None:
        if not self.ready:
            self.worker.bridge_event.emit({"event": "ERROR", "detail": "Mega handshake not complete"})
            return
        ignored, packet = self.link.stop_remote_dispense()
        self.serial_port.write(packet)

    def configure_tank(self, config: TankConfig) -> None:
        if not self.ready:
            self.worker.bridge_event.emit({"event": "ERROR", "detail": "Mega handshake not complete"})
            return
        ignored, packet = self.link.set_tank_config(config)
        self.serial_port.write(packet)

    def get_tank_status(self) -> None:
        if not self.ready:
            return
        ignored, packet = self.link.get_tank_status()
        self.serial_port.write(packet)


class SerialWorker(QThread):
    connected = Signal()
    disconnected = Signal()
    failure = Signal(str)
    raw_received = Signal(bytes, float)
    frame_received = Signal(str, bytes, object, float)
    bytes_sent = Signal(str, bytes, float, float)
    statistics = Signal(dict)
    replay_event = Signal(dict)
    replay_state = Signal(dict)
    protocol_frame = Signal(str, str, bytes, float)
    display_event = Signal(dict)
    display_statistics = Signal(dict)
    display_state = Signal(dict)
    bridge_event = Signal(dict)
    tds_test_state = Signal(dict)

    def __init__(
        self,
        port: str,
        baud: int,
        responses: dict[Dataset, bytes | RequestResponseSet],
        response_delay_ms: float,
        emulation_active: bool,
        auto_respond: bool,
        replay_profile: ReplayProfile | None = None,
        emulator_role: EmulatorRole = EmulatorRole.MAIN_MCU,
        display_config: DisplayRoleConfig | None = None,
        transport_mode: str = "direct_uart",
        mega_config: MegaConfig | None = None,
    ) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self.transport_mode = transport_mode
        self.mega_config = mega_config or MegaConfig()
        self._commands: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        self._responses = {
            dataset: value.clone() if isinstance(value, RequestResponseSet) else bytes(value)
            for dataset, value in responses.items()
        }
        self._delay_ms = response_delay_ms
        self._emulation_active = emulation_active
        self._auto_respond = auto_respond
        self._emulator_role = EmulatorRole(emulator_role)
        self._stop_requested = False
        self._replay_profile = replay_profile or ReplayProfile.from_dict(
            {"name": "Empty", "sequences": {}}
        )
        self._replay_engine = ReplayEngine(self._replay_profile, self._responses)
        self._display_controller = DisplayRoleController(display_config)
        self._manual_followup_tx_end_us: int | None = None
        self._tds_test = TdsMappingSequencer()

    def request_stop(self) -> None:
        self._commands.put(("stop", None))

    def set_emulation(self, active: bool) -> None:
        self._commands.put(("emulation", active))

    def set_auto_respond(self, enabled: bool) -> None:
        self._commands.put(("auto", enabled))

    def set_delay(self, delay_ms: float) -> None:
        self._commands.put(("delay", delay_ms))

    def set_response(self, dataset: Dataset, frame: bytes) -> None:
        self._commands.put(("response", (dataset, frame)))

    def set_response_set(self, dataset: Dataset, response_set: RequestResponseSet) -> None:
        self._commands.put(("response_set", (dataset, response_set.to_dict())))

    def send_once(
        self, payload: bytes, direction: BridgeDirection | None = None
    ) -> None:
        self._commands.put(("send", (bytes(payload), direction)))

    def configure_mega(self, config: MegaConfig) -> None:
        self._commands.put(("mega_config", config))

    def inject_mega(self, direction: BridgeDirection, payload: bytes) -> None:
        self._commands.put(("mega_inject", (direction, bytes(payload))))

    def set_mega_rule(self, rule: MegaRule) -> None:
        self._commands.put(("mega_set_rule", rule))

    def mega_rule_command(self, command: MegaMessageType) -> None:
        self._commands.put(("mega_rule_command", command))

    def mega_command_dispense(self, volume_ml: int, feed_pump_parameter: int) -> None:
        self._commands.put(
            ("mega_command_dispense", (int(volume_ml), int(feed_pump_parameter)))
        )

    def mega_stop_remote_dispense(self) -> None:
        self._commands.put(("mega_stop_remote_dispense", None))

    def configure_mega_tank(self, config: TankConfig) -> None:
        self._commands.put(("mega_tank_config", config))

    def queue_tds_test_value(self, raw_value: int) -> None:
        self._commands.put(("tds_test_value", int(raw_value)))

    def disable_tds_test(self) -> None:
        self._commands.put(("tds_test_disable", None))

    def configure_display_role(self, config: DisplayRoleConfig) -> None:
        self._commands.put(("display_config", config.to_dict()))

    def start_display_polling(self) -> None:
        self._commands.put(("display_start", None))

    def stop_display_polling(self) -> None:
        self._commands.put(("display_stop", None))

    def step_display_request(self) -> None:
        self._commands.put(("display_step", None))

    def send_display_request(self, request: DisplayRequest) -> None:
        self._commands.put(("display_send", request.value))

    def send_display_aa_preset(self, preset_index: int) -> None:
        self._commands.put(("display_send_aa_preset", preset_index))

    def abort_water_process(self) -> None:
        self._commands.put(("display_abort_water_process", None))

    def configure_replay(self, profile: ReplayProfile) -> None:
        self._commands.put(("replay_profile", profile.to_dict()))

    def start_replay(self, mode: ReplayMode) -> None:
        self._commands.put(("replay_start", mode.value))

    def reset_replay(self) -> None:
        self._commands.put(("replay_reset", None))

    def pause_replay(self, paused: bool) -> None:
        self._commands.put(("replay_pause", paused))

    def next_replay_step(self) -> None:
        self._commands.put(("replay_next", None))

    def switch_replay_to_static(self) -> None:
        self._commands.put(("replay_static", None))

    def _process_commands(self, serial_port) -> None:
        while True:
            try:
                command, value = self._commands.get_nowait()
            except queue.Empty:
                return
            if command == "stop":
                self._stop_requested = True
            elif command == "emulation":
                self._emulation_active = bool(value)
            elif command == "auto":
                self._auto_respond = bool(value)
            elif command == "delay":
                self._delay_ms = max(0.0, float(value))
            elif command == "response":
                dataset, frame = value
                self._responses[dataset] = bytes(frame)
                self._replay_engine.update_static_response(dataset, bytes(frame))
            elif command == "response_set":
                dataset, config = value
                response_set = RequestResponseSet.from_config(dataset, config)
                self._responses[dataset] = response_set
                self._replay_engine.update_static_response_set(dataset, response_set)
            elif command == "send":
                payload, direction = value
                sent_at = time.time()
                if direction is not None and hasattr(serial_port, "inject"):
                    serial_port.inject(payload, BridgeDirection(direction))
                else:
                    serial_port.write(payload)
                serial_port.flush()
                self.bytes_sent.emit("MANUAL", payload, sent_at, -1.0)
                ignored_rx, tx_direction = direction_labels(self._emulator_role)
                self.protocol_frame.emit(tx_direction, "MANUAL", payload, sent_at)
            elif command == "mega_config" and hasattr(serial_port, "configure"):
                self.mega_config = value
                serial_port.configure(value)
            elif command == "mega_inject" and hasattr(serial_port, "inject"):
                direction, payload = value
                serial_port.inject(payload, BridgeDirection(direction))
            elif command == "mega_set_rule" and hasattr(serial_port, "set_rule"):
                serial_port.set_rule(value)
            elif command == "mega_rule_command" and hasattr(serial_port, "rule_command"):
                serial_port.rule_command(MegaMessageType(value))
            elif command == "mega_command_dispense" and hasattr(serial_port, "command_dispense"):
                volume_ml, feed_pump_parameter = value
                serial_port.command_dispense(int(volume_ml), int(feed_pump_parameter))
            elif command == "mega_stop_remote_dispense" and hasattr(serial_port, "stop_remote_dispense"):
                serial_port.stop_remote_dispense()
            elif command == "mega_tank_config" and hasattr(serial_port, "configure_tank"):
                serial_port.configure_tank(value)
            elif command == "tds_test_value":
                self._tds_test.queue(int(value))
                self.tds_test_state.emit(self._tds_test.snapshot())
            elif command == "tds_test_disable":
                self._tds_test.disable()
                self.tds_test_state.emit(self._tds_test.snapshot())
            elif command == "display_config":
                self._display_controller.reconfigure(DisplayRoleConfig.from_dict(value))
            elif command == "display_start":
                self._emulation_active = True
                self._display_controller.start(time.monotonic())
                self.display_event.emit({"event": "polling", "status": "POLLING STARTED"})
            elif command == "display_stop":
                self._emulation_active = False
                self._display_controller.stop()
                self.display_event.emit({"event": "polling", "status": "POLLING STOPPED"})
            elif command == "display_step":
                self._handle_display_events(
                    [self._display_controller.step(time.monotonic())], serial_port
                )
            elif command == "display_send":
                request = DisplayRequest(str(value))
                if request is DisplayRequest.AA:
                    event = self._display_controller.queue_manual_aa_preset(
                        self._display_controller.config.aa_hold_index
                    )
                else:
                    event = self._display_controller.send_request(
                        request, time.monotonic()
                    )
                self._handle_display_events([event], serial_port)
            elif command == "display_send_aa_preset":
                self._handle_display_events(
                    [
                        self._display_controller.queue_manual_aa_preset(int(value))
                    ],
                    serial_port,
                )
            elif command == "display_abort_water_process":
                self._handle_display_events(
                    [self._display_controller.queue_abort_water_process()],
                    serial_port,
                )
            elif command == "replay_profile":
                self._replay_profile = ReplayProfile.from_dict(value)
                self._replay_engine = ReplayEngine(self._replay_profile, self._responses)
                self._delay_ms = self._replay_profile.response_delay_ms
                self.replay_event.emit(
                    {"elapsed_s": 0.0, "event": "profile", "text": f"PROFILE LOADED: {self._replay_profile.name}"}
                )
                self.replay_state.emit(self._replay_engine.snapshot())
            elif command == "replay_start":
                mode = ReplayMode(value)
                self._replay_engine.start(mode)
                self.replay_event.emit(
                    {"elapsed_s": 0.0, "event": "start", "text": f"{mode.value} STARTED"}
                )
                self.replay_state.emit(self._replay_engine.snapshot())
            elif command == "replay_reset":
                mode = self._replay_engine.mode
                self._replay_engine.reset()
                self._replay_engine.mode = mode
                self.replay_event.emit(
                    {
                        "elapsed_s": 0.0,
                        "event": "reset",
                        "text": "STARTUP SEQUENCE RESET — ALL DATASET CURSORS AT FIRST STATE",
                    }
                )
                self.replay_state.emit(self._replay_engine.snapshot())
            elif command == "replay_pause":
                self._replay_engine.set_paused(bool(value))
                self.replay_event.emit(
                    {
                        "elapsed_s": self._replay_engine.elapsed(),
                        "event": "pause",
                        "text": "REPLAY PAUSED" if value else "REPLAY RESUMED",
                    }
                )
                self.replay_state.emit(self._replay_engine.snapshot())
            elif command == "replay_next":
                self._replay_engine.authorize_next()
                self.replay_event.emit(
                    {"elapsed_s": self._replay_engine.elapsed(), "event": "next", "text": "NEXT TRANSITION AUTHORIZED"}
                )
                self.replay_state.emit(self._replay_engine.snapshot())
            elif command == "replay_static":
                self._replay_engine.switch_to_static()
                self.replay_event.emit(
                    {"elapsed_s": self._replay_engine.elapsed(), "event": "static", "text": "SWITCHED TO STATIC"}
                )
                self.replay_state.emit(self._replay_engine.snapshot())

    def _handle_display_events(
        self, events: list[DisplayRoleEvent], serial_port
    ) -> None:
        rx_direction, tx_direction = direction_labels(self._emulator_role)
        for event in events:
            wall_time = time.time()
            request_name = event.request.value if event.request else "UNKNOWN"
            payload = {
                "event": event.event,
                "timestamp": wall_time,
                "direction": (
                    tx_direction
                    if event.event == "tx"
                    else rx_direction if event.event in ("rx", "unexpected") else "—"
                ),
                "request": request_name,
                "raw": event.raw.hex(" ").upper(),
                "status": event.status,
                "latency_ms": event.latency_ms,
                "checksum_valid": event.checksum_valid,
                "source": event.source,
                "reason": event.reason,
                "state": self._display_controller.snapshot(time.monotonic()),
            }
            if event.event == "tx" and event.raw:
                serial_port.write(event.raw)
                serial_port.flush()
                kind = "AA" if event.request is DisplayRequest.AA else request_name
                self.bytes_sent.emit(kind, event.raw, wall_time, -2.0)
                self.protocol_frame.emit(tx_direction, kind, event.raw, wall_time)
            elif event.event == "rx" and event.raw:
                kind = "CC" if event.request is DisplayRequest.AA else request_name
                self.frame_received.emit(kind, event.raw, event.checksum_valid, wall_time)
                self.protocol_frame.emit(rx_direction, kind, event.raw, wall_time)
            elif event.event == "unexpected" and event.raw:
                self.frame_received.emit("UNKNOWN", event.raw, None, wall_time)
                self.protocol_frame.emit(rx_direction, "UNKNOWN", event.raw, wall_time)
            self.display_event.emit(payload)

    def _emit_replay_exchange(self, frame, decision, mode_before: ReplayMode) -> None:
        if mode_before is ReplayMode.STATIC:
            return
        event = {
            "elapsed_s": self._replay_engine.elapsed(),
            "event": "rx",
            "request_type": decision.request_type.value if decision.request_type else frame.kind.value,
            "raw": frame.raw.hex(" ").upper(),
            "source": decision.source,
            "aa_checksum_valid": frame.checksum_valid,
            "aa_matched": decision.aa_matched,
            "timeout_exceeded": decision.timeout_exceeded,
        }
        if frame.kind.value == "AA" and len(frame.raw) == 19:
            event["checksum_stored"] = (frame.raw[17] << 8) | frame.raw[18]
            event["checksum_calculated"] = sum(frame.raw[1:17])
        self.replay_event.emit(event)

    def run(self) -> None:
        try:
            import serial

            serial_port = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.02,
                write_timeout=1.0,
            )
        except Exception as exc:
            self.failure.emit(f"Could not open {self.port}: {exc}")
            self.disconnected.emit()
            return

        if self.transport_mode == "mega_bridge":
            self._run_mega(serial_port)
            return

        parser = StreamParser()
        core = EmulatorCore()
        self.connected.emit()
        if self._emulator_role is EmulatorRole.DISPLAY:
            self.display_event.emit(
                {"event": "passive", "status": "PASSIVE LISTEN ONLY — NO BYTES WILL BE SENT"}
            )
        last_stats = time.monotonic()
        connected_at = last_stats
        traffic_observed = False
        no_traffic_reported = False
        try:
            while not self._stop_requested:
                self._process_commands(serial_port)
                chunk = serial_port.read(serial_port.in_waiting or 1)
                if self._emulator_role is EmulatorRole.DISPLAY:
                    if chunk:
                        traffic_observed = True
                        received_at = time.time()
                        self.raw_received.emit(chunk, received_at)
                        self._handle_display_events(
                            self._display_controller.receive(chunk, time.monotonic()),
                            serial_port,
                        )
                    if (
                        self._emulation_active
                        or self._display_controller.pending is not None
                        or self._display_controller.has_queued_tx
                    ):
                        self._handle_display_events(
                            self._display_controller.tick(time.monotonic()), serial_port
                        )
                    if (
                        not traffic_observed
                        and not no_traffic_reported
                        and time.monotonic() - connected_at >= 1.0
                    ):
                        no_traffic_reported = True
                        self.display_event.emit(
                            {"event": "passive", "status": "NO TRAFFIC OBSERVED"}
                        )
                elif chunk:
                    received_at = time.time()
                    request_clock = time.perf_counter()
                    self.raw_received.emit(chunk, received_at)
                    for frame in parser.feed(chunk):
                        core.register_received(frame)
                        self.frame_received.emit(
                            frame.kind.value, frame.raw, frame.checksum_valid, received_at
                        )
                        self.protocol_frame.emit(
                            "DISPLAY -> EMU", frame.kind.value, frame.raw, received_at
                        )
                        mode_before = self._replay_engine.mode
                        decision = self._replay_engine.handle(
                            frame,
                            transmit_enabled=self._emulation_active and self._auto_respond,
                            timestamp=received_at,
                        )
                        self._emit_replay_exchange(frame, decision, mode_before)
                        response = decision.response
                        if (
                            response is not None
                            and frame.kind.value == "E5 B2"
                            and self._tds_test.enabled
                        ):
                            response = self._tds_test.transform_b2(response)
                            self.tds_test_state.emit(self._tds_test.snapshot())
                        if response is not None:
                            if self._delay_ms:
                                time.sleep(self._delay_ms / 1000.0)
                            serial_port.write(response)
                            serial_port.flush()
                            sent_at = time.time()
                            latency_ms = (time.perf_counter() - request_clock) * 1000.0
                            core.register_response(latency_ms)
                            response_type = {
                                "A5": "A5",
                                "E5_B2": "E5 B2",
                                "E5_E1": "E5 E1",
                                "AA": "AA ACK",
                            }.get(
                                decision.request_type.value if decision.request_type else "",
                                "RESPONSE",
                            )
                            self.bytes_sent.emit(response_type, response, sent_at, latency_ms)
                            self.protocol_frame.emit(
                                "EMU -> DISPLAY", response_type, response, sent_at
                            )
                            if mode_before is not ReplayMode.STATIC:
                                checksum = (
                                    {}
                                    if decision.request_type is not None
                                    and decision.request_type.value == "AA"
                                    else decision.checksum or {}
                                )
                                self.replay_event.emit(
                                    {
                                        "elapsed_s": self._replay_engine.elapsed(),
                                        "event": "tx",
                                        "request_type": decision.request_type.value if decision.request_type else "",
                                        "state": decision.step_name,
                                        "source": decision.source,
                                        "raw": response.hex(" ").upper(),
                                        "checksum_stored": checksum.get("stored"),
                                        "checksum_calculated": checksum.get("calculated"),
                                        "checksum_valid": checksum.get("valid"),
                                        "latency_ms": latency_ms,
                                    }
                                )
                        if decision.switched_to_static:
                            self.replay_event.emit(
                                {
                                    "elapsed_s": self._replay_engine.elapsed(),
                                    "event": "static",
                                    "text": "REPLAY COMPLETE — SWITCHED TO STATIC",
                                }
                            )
                        self.replay_state.emit(self._replay_engine.snapshot())
                if time.monotonic() - last_stats >= 0.25:
                    self.statistics.emit(core.snapshot())
                    if self._emulator_role is EmulatorRole.DISPLAY:
                        display_stats = self._display_controller.statistics()
                        self.display_statistics.emit(display_stats)
                        self.display_state.emit(
                            self._display_controller.snapshot(time.monotonic())
                        )
                    else:
                        self.replay_state.emit(self._replay_engine.snapshot())
                    last_stats = time.monotonic()
        except Exception as exc:
            self.failure.emit(f"Serial connection lost: {exc}")
        finally:
            if self._emulator_role is EmulatorRole.MAIN_MCU:
                pending = parser.reset()
                if pending:
                    self.frame_received.emit("UNKNOWN", pending, None, time.time())
            self.statistics.emit(core.snapshot())
            if self._emulator_role is EmulatorRole.DISPLAY:
                self.display_statistics.emit(self._display_controller.statistics())
                self.display_state.emit(
                    self._display_controller.snapshot(time.monotonic())
                )
            try:
                serial_port.close()
            finally:
                self.disconnected.emit()

    def _run_mega(self, serial_port) -> None:
        link = MegaBridgeLink()
        adapter = _MegaInjectionPort(serial_port, link, self)
        display_parser = StreamParser()
        self._emulation_active = False
        last_stats = time.monotonic()
        opened_at = last_stats
        last_heartbeat = opened_at
        connected_emitted = False
        main_traffic_observed = False
        no_main_reported = False
        try:
            while not self._stop_requested:
                self._process_commands(adapter)
                chunk = serial_port.read(serial_port.in_waiting or 1)
                if chunk:
                    for message in link.parser.feed(chunk):
                        if message.message_type is MegaMessageType.RX_FRAME_MAIN:
                            main_traffic_observed = True
                        self._handle_mega_message(message, adapter, display_parser)
                        if message.message_type is MegaMessageType.HELLO:
                            adapter.ready = True
                            adapter.configure(self.mega_config)
                            ignored, ping = link.message(MegaMessageType.PING, b"workbench")
                            serial_port.write(ping)
                            adapter.rule_command(MegaMessageType.GET_RULES)
                            adapter.get_tank_status()
                            if not connected_emitted:
                                self.connected.emit()
                                connected_emitted = True
                if not adapter.ready and time.monotonic() - opened_at >= 2.0:
                    # Handles boards that did not reset when the port opened and
                    # whose boot HELLO was therefore emitted before connection.
                    adapter.ready = True
                    adapter.configure(self.mega_config)
                    ignored, ping = link.message(MegaMessageType.PING, b"workbench")
                    serial_port.write(ping)
                    adapter.rule_command(MegaMessageType.GET_RULES)
                    adapter.get_tank_status()
                    if not connected_emitted:
                        self.connected.emit()
                        connected_emitted = True
                if adapter.ready and time.monotonic() - last_heartbeat >= 0.5:
                    ignored, heartbeat = link.message(MegaMessageType.PING, b"lease")
                    serial_port.write(heartbeat)
                    last_heartbeat = time.monotonic()
                if (
                    self.mega_config.mode.name == "DISPLAY_ONLY_TEST"
                    and not main_traffic_observed
                    and not no_main_reported
                    and time.monotonic() - opened_at >= 2.0
                ):
                    self.bridge_event.emit(
                        {
                            "event": "MAIN_NO_TRAFFIC",
                            "detail": "Main MCU not connected / no traffic (expected in DISPLAY_ONLY_TEST)",
                        }
                    )
                    no_main_reported = True
                if (
                    self._emulator_role is EmulatorRole.DISPLAY
                    and (
                        self._display_controller.pending is not None
                        or self._display_controller.has_queued_tx
                        or self._emulation_active
                    )
                ):
                    self._handle_display_events(
                        self._display_controller.tick(time.monotonic()), adapter
                    )
                if time.monotonic() - last_stats >= 0.25:
                    if self._emulator_role is EmulatorRole.DISPLAY:
                        self.display_statistics.emit(self._display_controller.statistics())
                        self.display_state.emit(
                            self._display_controller.snapshot(time.monotonic())
                        )
                    last_stats = time.monotonic()
        except Exception as exc:
            self.failure.emit(f"Mega bridge connection lost: {exc}")
        finally:
            serial_port.close()
            self.disconnected.emit()

    def _handle_mega_message(self, message, adapter, display_parser: StreamParser) -> None:
        event = {"event": message.message_type.name, "sequence": message.sequence}
        if message.message_type is MegaMessageType.HELLO:
            name = message.payload[:8].rstrip(b"\0").decode("ascii", "replace")
            baud = struct.unpack_from("<I", message.payload, 8)[0] if len(message.payload) >= 12 else 0
            event["detail"] = f"{name or 'ATmega2560'}; Quella baud {baud}"
        elif message.message_type is MegaMessageType.STATUS and len(message.payload) >= 16:
            mode, sniff, injection_count, capture_count, baud, dropped_bridge, dropped_capture = struct.unpack(
                "<BBBBIII", message.payload[:16]
            )
            event.update(
                detail=("DISPLAY_ONLY_TEST" if mode else "DIRECT_PASS") + f"; sniff={bool(sniff)}; baud={baud}",
                injection_queue=injection_count,
                capture_queue=capture_count,
                dropped_bridge=dropped_bridge,
                dropped_capture=dropped_capture,
            )
        elif message.message_type in (MegaMessageType.RX_FRAME_DISPLAY, MegaMessageType.RX_FRAME_MAIN):
            try:
                timestamp_us, raw = decode_rx_payload(message.payload)
            except ValueError as exc:
                self.bridge_event.emit({"event": "ERROR", "detail": str(exc)})
                return
            received_at = time.time()
            self.raw_received.emit(raw, received_at)
            if message.message_type is MegaMessageType.RX_FRAME_DISPLAY:
                event.update(
                    direction="DISPLAY_TO_MAIN", raw=raw.hex(" ").upper(),
                    timestamp_us=timestamp_us, decoded_type="UNKNOWN",
                    traffic_source="UART_RX",
                    checksum_valid=(validate_aa_frame(raw) if len(raw) == 19 and raw[:1] == b"\xAA" else None),
                )
                if self._manual_followup_tx_end_us is not None:
                    delta_us = (timestamp_us - self._manual_followup_tx_end_us) & 0xFFFFFFFF
                    if delta_us <= 1_000_000:
                        event.update(
                            event="MANUAL_FOLLOWUP_DISPLAY",
                            manual_followup=True,
                            detail=f"DISPLAY_TO_MAIN {delta_us / 1000.0:.1f} ms after manual DISPENSE",
                        )
                    elif delta_us < 0x80000000:
                        self._manual_followup_tx_end_us = None
                for frame in display_parser.feed(raw):
                    event["decoded_type"] = frame.kind.value
                    event["checksum_valid"] = frame.checksum_valid
                    self.frame_received.emit(frame.kind.value, frame.raw, frame.checksum_valid, received_at)
                    self.protocol_frame.emit("DISPLAY_TO_MAIN", frame.kind.value, frame.raw, received_at)
                    if (
                        self._emulator_role is EmulatorRole.MAIN_MCU
                        and self.mega_config.mode.name == "DISPLAY_ONLY_TEST"
                        and self._tds_test.enabled
                    ):
                        request_clock = time.perf_counter()
                        decision = self._replay_engine.handle(
                            frame, transmit_enabled=True, timestamp=received_at
                        )
                        response = decision.response
                        if response is not None:
                            if frame.kind.value == "E5 B2":
                                response = self._tds_test.transform_b2(response)
                                self.tds_test_state.emit(self._tds_test.snapshot())
                            if self._delay_ms:
                                time.sleep(self._delay_ms / 1000.0)
                            adapter.write(response)
                            adapter.flush()
                            sent_at = time.time()
                            latency_ms = (time.perf_counter() - request_clock) * 1000.0
                            response_type = {
                                "A5": "A5", "E5 B2": "E5 B2",
                                "E5 E1": "E5 E1", "AA": "AA ACK",
                            }.get(frame.kind.value, "RESPONSE")
                            self.bytes_sent.emit(response_type, response, sent_at, latency_ms)
                            self.protocol_frame.emit(
                                "MAIN_TO_DISPLAY", response_type, response, sent_at
                            )
            else:
                kind = classify_main_frame(raw)
                valid = validate_response(raw) if len(raw) == 18 else None
                event.update(
                    direction="MAIN_TO_DISPLAY", raw=raw.hex(" ").upper(),
                    timestamp_us=timestamp_us, decoded_type=kind,
                    traffic_source="UART_RX",
                    checksum_valid=valid,
                )
                self.frame_received.emit(kind, raw, valid, received_at)
                self.protocol_frame.emit("MAIN_TO_DISPLAY", kind, raw, received_at)
                if self._emulator_role is EmulatorRole.DISPLAY:
                    self._handle_display_events(
                        self._display_controller.receive(raw, time.monotonic()), adapter
                    )
        elif message.message_type in (MegaMessageType.INJECT_QUEUED, MegaMessageType.INJECT_SENT):
            if len(message.payload) >= 2:
                event["sequence"] = struct.unpack_from("<H", message.payload)[0]
            if len(message.payload) >= 3:
                raw_value = message.payload[2]
                if message.message_type is MegaMessageType.INJECT_SENT and raw_value in (0, 1):
                    event["direction"] = BridgeDirection(raw_value).name
                else:
                    event["detail"] = f"queue depth {raw_value}"
        elif message.message_type is MegaMessageType.ERROR:
            error_code = message.payload[0] if message.payload else None
            event["detail"] = {
                5: "manual DISPENSE rejected: volume must be 1..1500 ml",
                6: "manual DISPENSE rejected: requires DIRECT_PASS and no active rule",
                7: "manual DISPENSE queue/TX could not complete",
                10: "tank delay rejected: value must be 0..60000 ms",
                13: "manual DISPENSE rejected: 25 C feed-pump parameter must be 100..1200 ml/min",
                14: "dispense blocked: internal tank is BELOW LOW",
                15: "dispense blocked: no fresh valid internal MIN sensor state",
            }.get(error_code, f"firmware error {message.payload.hex(' ').upper()}")
        elif message.message_type is MegaMessageType.PONG:
            if message.payload == b"lease":
                return
            event["detail"] = "link alive"
        elif message.message_type is MegaMessageType.RULE_STATUS:
            try:
                status = decode_rule_status(message.payload)
            except (ValueError, struct.error) as exc:
                event = {"event": "RULE_ERROR", "detail": str(exc)}
            else:
                event.update(
                    configured=status.configured,
                    enabled=status.enabled,
                    direction=status.direction.name,
                    action=status.action.name,
                    override_value=status.override_value,
                    match_count=status.match_count,
                    replace_count=status.replace_count,
                    original="" if status.last_original is None else status.last_original.hex(" ").upper(),
                    replacement="" if status.last_replacement is None else status.last_replacement.hex(" ").upper(),
                    detail=("enabled" if status.enabled else "PASS / disabled"),
                )
        elif message.message_type is MegaMessageType.RULE_MATCH and len(message.payload) == 23:
            event.update(
                match_count=struct.unpack_from("<I", message.payload)[0],
                original=message.payload[4:].hex(" ").upper(),
                detail="exact rule match",
            )
        elif message.message_type is MegaMessageType.RULE_REPLACED and len(message.payload) == 42:
            event.update(
                replace_count=struct.unpack_from("<I", message.payload)[0],
                original=message.payload[4:23].hex(" ").upper(),
                replacement=message.payload[23:42].hex(" ").upper(),
                detail="frame replaced; SUM16 recalculated",
            )
        elif message.message_type is MegaMessageType.RULE_ERROR:
            event["detail"] = f"rule error {message.payload.hex(' ').upper()}"
        elif message.message_type in (
            MegaMessageType.DISPENSE_OVERRIDE_STATUS,
            MegaMessageType.DISPENSE_OVERRIDE_EVENT,
        ):
            try:
                if message.message_type is MegaMessageType.DISPENSE_OVERRIDE_EVENT:
                    override_event, override, original_frame = decode_dispense_override_event(message.payload)
                    event["event"] = {
                        DispenseOverrideEvent.START: "DISPENSE_OVERRIDE_START",
                        DispenseOverrideEvent.REFRESH_REPLACED: "DISPENSE_REFRESH_REPLACED",
                        DispenseOverrideEvent.ABORT_SUPPRESSED: "DISPENSE_ABORT_SUPPRESSED",
                        DispenseOverrideEvent.PROGRESS: "DISPENSE_PROGRESS",
                        DispenseOverrideEvent.TARGET_REACHED: "DISPENSE_TARGET_REACHED",
                        DispenseOverrideEvent.END: "DISPENSE_OVERRIDE_END",
                        DispenseOverrideEvent.USER_ABORT: "DISPENSE_USER_ABORT",
                        DispenseOverrideEvent.REMOTE_START: "REMOTE_DISPENSE_START",
                        DispenseOverrideEvent.REMOTE_DISPLAY_IDLE_SUPPRESSED: "REMOTE_DISPLAY_IDLE_SUPPRESSED",
                        DispenseOverrideEvent.REMOTE_REFRESH_REPLACED: "REMOTE_REFRESH_REPLACED",
                        DispenseOverrideEvent.REMOTE_PROGRESS: "REMOTE_DISPENSE_PROGRESS",
                        DispenseOverrideEvent.REMOTE_COMPLETE: "REMOTE_DISPENSE_COMPLETE",
                        DispenseOverrideEvent.REMOTE_ABORT_REQUESTED: "REMOTE_DISPENSE_ABORT_REQUESTED",
                        DispenseOverrideEvent.REMOTE_ABORT_TX: "REMOTE_DISPENSE_ABORT_TX",
                        DispenseOverrideEvent.REMOTE_ABORT_CONFIRMED: "REMOTE_DISPENSE_ABORT_CONFIRMED",
                        DispenseOverrideEvent.REMOTE_END: "REMOTE_DISPENSE_END",
                        DispenseOverrideEvent.REMOTE_ERROR: "REMOTE_DISPENSE_ERROR",
                        DispenseOverrideEvent.REMOTE_PUMP_PARAMETER_REPLACED: "REMOTE_PUMP_PARAMETER_REPLACED",
                    }[override_event]
                    if original_frame is not None:
                        event["original_frame"] = original_frame.hex(" ").upper()
                else:
                    override = decode_dispense_override_status(message.payload)
            except (ValueError, struct.error) as exc:
                event = {"event": "RULE_ERROR", "detail": str(exc)}
            else:
                event.update(
                    override_active=override.active,
                    aborting=override.aborting,
                    session_origin={
                        DispenseSessionOrigin.NONE: "NONE",
                        DispenseSessionOrigin.DISPLAY_OVERRIDE: "DISPLAY_OVERRIDE",
                        DispenseSessionOrigin.MANUAL_REMOTE: "MANUAL/REMOTE",
                    }[override.origin],
                    target_ml=override.target_ml,
                    original_display_ml=override.original_display_ml,
                    start_timestamp_us=override.start_timestamp_us,
                    start_seen=override.start_seen,
                    refresh_count=override.refresh_count,
                    blocked_abort_count=override.blocked_abort_count,
                    current_a5_ml=override.last_progress_ml,
                    main_dispensing=override.main_dispensing,
                    elapsed_us=override.elapsed_us,
                    detail=(
                        f"target={override.target_ml} ml; original={override.original_display_ml} ml; "
                        f"progress={override.last_progress_ml} ml; "
                        f"main={'dispensing' if override.main_dispensing else 'idle'}"
                        + (f"; original_frame={event['original_frame']}" if event.get("original_frame") else "")
                    ),
                )
        elif message.message_type in (MegaMessageType.TANK_STATUS, MegaMessageType.TANK_EVENT):
            try:
                if message.message_type is MegaMessageType.TANK_EVENT:
                    tank_event, timestamp_us, tank = decode_tank_event(message.payload)
                    event["event"] = {
                        TankEvent.EXTERNAL_TANK_LOW: "EXTERNAL_TANK_LOW",
                        TankEvent.EXTERNAL_AVAILABLE_PENDING: "EXTERNAL_AVAILABLE_PENDING",
                        TankEvent.EXTERNAL_AVAILABLE_TIMER_RESET: "EXTERNAL_AVAILABLE_TIMER_RESET",
                        TankEvent.EXTERNAL_AVAILABLE_STABLE: "EXTERNAL_AVAILABLE_STABLE",
                        TankEvent.INTERNAL_TANK_MIN_CHANGED: "INTERNAL_TANK_MIN_CHANGED",
                        TankEvent.INTERNAL_TANK_MAX_CHANGED: "INTERNAL_TANK_MAX_CHANGED",
                        TankEvent.FILL_STATE_CHANGED: "FILL_STATE_CHANGED",
                        TankEvent.EXTERNAL_STOP_LATCHED: "EXTERNAL_STOP_LATCHED",
                        TankEvent.EXTERNAL_RESET_ARMED: "EXTERNAL_RESET_ARMED",
                        TankEvent.EXTERNAL_STOP_CLEARED: "EXTERNAL_STOP_CLEARED",
                    }[tank_event]
                    event["timestamp_us"] = timestamp_us
                else:
                    tank = decode_tank_status(message.payload)
            except (ValueError, struct.error) as exc:
                event = {"event": "TANK_STATUS_ERROR", "detail": str(exc)}
            else:
                event.update(
                    external_state=tank.external_state.name,
                    external_raw_known=tank.raw_known,
                    external_raw_available=tank.raw_available,
                    external_stable_known=tank.stable_known,
                    external_stable_available=tank.stable_available,
                    internal_min_known=tank.internal_min_known,
                    internal_min_active=tank.internal_min_active,
                    internal_max_known=tank.internal_max_known,
                    internal_max_active=tank.internal_max_active,
                    fill_allowed=tank.fill_allowed,
                    fill_active_known=tank.fill_active_known,
                    fill_active=tank.fill_active,
                    delay_enabled=tank.delay_enabled,
                    external_stop_latched=tank.external_stop_latched,
                    external_reset_armed=tank.external_reset_armed,
                    delay_ms=tank.delay_ms,
                    delay_remaining_ms=tank.delay_remaining_ms,
                    last_update_age_ms=tank.last_update_age_ms,
                    raw="" if tank.last_frame is None else tank.last_frame.hex(" ").upper(),
                    detail=(
                        f"raw={'AVAILABLE' if tank.raw_available else 'NOT AVAILABLE'}; "
                        f"state={tank.external_state.name}; remaining={tank.delay_remaining_ms} ms; "
                        f"fill_allowed={'YES' if tank.fill_allowed else 'NO'}"
                    ),
                )
        elif message.message_type in (
            MegaMessageType.REPLACE_BUILD,
            MegaMessageType.REPLACE_TX_BEGIN,
            MegaMessageType.REPLACE_TX_END,
        ):
            try:
                diagnostic = decode_replace_event(message.message_type, message.payload)
            except (ValueError, struct.error) as exc:
                event = {"event": "RULE_ERROR", "detail": str(exc)}
            else:
                direction = diagnostic["direction"]
                event["direction"] = direction.name
                target = diagnostic.get("target_uart")
                target_name = "Serial2 / TX pin 16" if target == 2 else "Serial1 / TX pin 18"
                if message.message_type is MegaMessageType.REPLACE_BUILD:
                    event.update(
                        original_len=diagnostic["original_len"],
                        replacement_len=diagnostic["replacement_len"],
                        original=diagnostic["original"].hex(" ").upper(),
                        replacement=diagnostic["replacement"].hex(" ").upper(),
                        original_checksum=diagnostic["original_checksum"].hex(" ").upper(),
                        replacement_checksum=diagnostic["replacement_checksum"].hex(" ").upper(),
                        detail=(
                            f"original len={diagnostic['original_len']} checksum="
                            f"{diagnostic['original_checksum'].hex(' ').upper()} | replacement "
                            f"len={diagnostic['replacement_len']} checksum="
                            f"{diagnostic['replacement_checksum'].hex(' ').upper()}"
                        ),
                    )
                elif message.message_type is MegaMessageType.REPLACE_TX_BEGIN:
                    gap_us = (diagnostic["begin_us"] - diagnostic["last_rx_us"]) & 0xFFFFFFFF
                    event.update(
                        target_uart=target_name, length=diagnostic["length"],
                        frame=diagnostic["frame"].hex(" ").upper(), gap_us=gap_us,
                        raw=diagnostic["frame"].hex(" ").upper(),
                        timestamp_us=diagnostic["begin_us"], decoded_type="AA",
                        checksum_valid=True, traffic_source="MEGA_LOCAL_REPLACE_TX",
                        detail=f"uart={target_name}; len={diagnostic['length']}; after last RX={gap_us} µs",
                    )
                else:
                    duration_us = (diagnostic["end_us"] - diagnostic["begin_us"]) & 0xFFFFFFFF
                    event.update(
                        target_uart=target_name, length=diagnostic["length"],
                        written=diagnostic["written"], duration_us=duration_us,
                        detail=(f"uart={target_name}; written={diagnostic['written']}/"
                                f"{diagnostic['length']}; physical TX={duration_us} µs"),
                    )
        elif message.message_type in (
            MegaMessageType.MANUAL_CMD_QUEUED,
            MegaMessageType.MANUAL_CMD_TX_BEGIN,
            MegaMessageType.MANUAL_CMD_TX_END,
        ):
            try:
                diagnostic = decode_manual_command_event(message.message_type, message.payload)
            except (ValueError, struct.error) as exc:
                event = {"event": "ERROR", "detail": str(exc)}
            else:
                command_name = "DISPENSE"
                event.update(command=command_name, volume_ml=diagnostic["volume_ml"])
                if message.message_type is MegaMessageType.MANUAL_CMD_QUEUED:
                    event.update(
                        length=diagnostic["length"], frame=diagnostic["frame"].hex(" ").upper(),
                        detail=f"command={command_name}; ml={diagnostic['volume_ml']}; waiting for safe idle gap",
                    )
                elif message.message_type is MegaMessageType.MANUAL_CMD_TX_BEGIN:
                    target_name = "Serial2 / TX pin 16" if diagnostic["target_uart"] == 2 else "UNKNOWN"
                    event.update(
                        target_uart=target_name, length=diagnostic["length"],
                        frame=diagnostic["frame"].hex(" ").upper(),
                        direction="DISPLAY_TO_MAIN",
                        raw=diagnostic["frame"].hex(" ").upper(),
                        timestamp_us=diagnostic["begin_us"], decoded_type="AA",
                        checksum_valid=True, traffic_source="MEGA_LOCAL_MANUAL_TX",
                        detail=f"uart={target_name}; len={diagnostic['length']}; ml={diagnostic['volume_ml']}",
                    )
                else:
                    target_name = "Serial2 / TX pin 16" if diagnostic["target_uart"] == 2 else "UNKNOWN"
                    duration_us = (diagnostic["end_us"] - diagnostic["begin_us"]) & 0xFFFFFFFF
                    self._manual_followup_tx_end_us = diagnostic["end_us"]
                    event.update(
                        target_uart=target_name, length=diagnostic["length"],
                        written=diagnostic["written"], duration_us=duration_us,
                        detail=(f"written={diagnostic['written']}/{diagnostic['length']}; "
                                f"duration={duration_us} µs; observe DISPLAY_TO_MAIN for 1 s"),
                    )
        self.bridge_event.emit(event)
