# ESP8266 gateway protocol (`QG` v1)

Serial3 is a dedicated binary link at 115200 baud. It is independent of the
USB `QB` protocol. Control/status remains semantic; message `0x87` transports
batched exact Quella frames for the separate telemetry plane.

All integers are little-endian. The envelope is:

| Field | Bytes |
|---|---:|
| Magic `QG` | 2 |
| Version (`1`) | 1 |
| Message type | 1 |
| Flags | 1 |
| Sequence | 2 |
| Payload length | 2 |
| Payload | 0..192 |
| CRC16-CCITT | 2 |

CRC uses polynomial `0x1021`, initial value `0xFFFF`, and covers version
through the final payload byte. Magic is excluded to allow stream resync.

## Messages

| Value | ESP → Mega | Payload |
|---:|---|---|
| `01` | PING | optional opaque bytes |
| `02` | GET_STATUS | empty |
| `03` | DISPENSE | `volume_ml:u16, temperature_c:u8` |
| `04` | STOP_DISPENSE | empty |
| `05` | SET_PRESET | `id:u8, volume_ml:u16[, temperature_c:u8]` |
| `06` | SET_TANK_DELAY | `enabled:u8, delay_ms:u32` |
| `07` | GET_CONFIG | empty |

| Value | Mega → ESP | Purpose |
|---:|---|---|
| `80` | HELLO | firmware/protocol identity |
| `81` | PONG | sequence-matched link check |
| `82` | STATUS_SNAPSHOT | semantic status v1 |
| `83` | EVENT | `domain:u8, event:u8, uptime_ms:u32` |
| `84` | COMMAND_RESULT | `command:u8, result:u8` |
| `85` | CONFIG_RESULT | tank delay, limits, presets |
| `86` | ERROR | transport/internal error |
| `87` | RAW_BATCH | compact batch of exact observed Quella frames |

Results are `OK`, `BUSY`, `TANK_EMPTY`, `EXTERNAL_TANK_NOT_READY`,
`TEMP_OUT_OF_RANGE`, `INVALID_VOLUME`, `COMMUNICATION_ERROR`, `FILL_ACTIVE`,
`FLUSH_ACTIVE`, `INVALID_REQUEST`, `NOT_ACTIVE`, `QUEUE_FULL`, or
`INTERNAL_TANK_BELOW_LOW`, or `UNSUPPORTED`. Only the Mega produces the
authoritative result.

## Status snapshot v1

The payload is 46 bytes. Byte 0 is the snapshot version, bytes 1..4 Mega
uptime, and bytes 5..8 a known-field mask. Remaining fixed fields carry mode,
machine flags, session origin, error bits, target/current/remaining volume,
derived flow and progress, temperatures, tank/fill flags, delay remaining,
AC mains voltage, total/current counters, feed parameter, requested volume,
and the latched E5 B2 TDS measurement in the final byte.

The known mask is mandatory: a zero value in an unknown field is not a valid
measurement. Known-mask bit 29 marks byte 45 as the TDS measurement value.
The Mega retains Data[3] during the D13.bit0 measurement phase and commits it
when the D11.bit4 update strobe ends, so idle B2 frames cannot erase the last
measurement. The WebSocket status publishes it as `water.tds_raw`; legacy
`water.tds` remains a raw-value alias. `water.tds_calculated` uses the captured
20-range original-display threshold table and `water.tds_mapping` identifies
it as `CAPTURED_DISPLAY_THRESHOLDS_V1`. No external DC voltage field is published
because no such protocol value has been established.

## Raw telemetry plane

Every complete frame observed by the Mega is additionally queued in a fixed
ring and emitted in QG `RAW_BATCH` (`0x87`). Reporting is best-effort: a full
ring drops the newest telemetry record and increments a counter; it can never
delay or alter transparent UART forwarding. The ESP validates this message
without interpreting the appliance protocol and forwards it as binary `QT`
UDP. Raw frames are not sent through WebSocket or JSON. The exact payload and
UDP formats are defined in [Telemetry_Protocol.md](Telemetry_Protocol.md).

At 9600 baud, normal traffic remains comfortably below the dedicated
115200-baud Mega-to-ESP link capacity. The stream assigns no new semantics;
unknown bytes and every bit stay available for external long-term correlation.
It is enabled in the main firmware by default and can be compiled out with
`QUELLA_GATEWAY_PROTOCOL_STREAM=0` if a future stress test requires an exact
comparison without gateway reporting load.

The desktop GUI consumes this plane directly: start `python main.py`, choose
**Network UDP telemetry (passive)**, keep the bind address at
`udp://0.0.0.0:42042`, and connect. Frame Editor pages can then enable **Live
values** to overlay the latest response without modifying configured frames.

The Mega sends a snapshot every 200 ms. Events are immediate. Serial3 failure,
ESP reset, WiFi loss, or an HTTP timeout does not alter transparent MITM
forwarding. Gateway commands call the existing Mega state machines and never
bypass their validation or fail-safe paths.

Normal remote `DISPENSE` accepts arbitrary integer values from 1 through
1500 ml. The original Display's captured regular setpoints stop at 900 ml;
the extended range is exposed deliberately for remote operation.
## Product configuration v2

The v2 config result advertises the semantic override API. The added commands
are `GET_OVERRIDES` (`0x08`), `SET_OVERRIDE` (`0x09`), `DELETE_OVERRIDE`
(`0x0A`) and `SAVE_CONFIG` (`0x0B`). Only `VOLUME_PRESET` is supported. A rule
contains an id `0..7`, a source volume, target volume and enabled flag. Both
Target volumes are validated against the Mega's reported `1..1500 ml`
capability. Override source values remain limited to the original Display's
`1..900 ml` range.

The ESP exposes these only as `/api/overrides` and
`/api/overrides/delete`. It never implements matching itself. `SET`/`DELETE`
is followed by explicit `SAVE_CONFIG`, so EEPROM is not written by telemetry
or normal UART traffic.

Status known-bit 0 is retained on the wire for compatibility, but its audited
name is `A5_D11_BIT4_CYCLIC`. It is not a readiness signal. Mode derivation
returns `IDLE` when no confirmed operation/error is active.
