# OsmoFresh Quella Pro UART protocol

> Status: reverse-engineered working specification, 2026-11-16  
> Physical link: 9600 baud, 8N1, idle HIGH, approximately 5 V  
> Scope: Display MCU ↔ Main MCU application protocol

This document consolidates the currently reproducible findings. It deliberately
separates observed bytes, implemented safety policy, and hypotheses. It is not
manufacturer documentation.

## Confidence vocabulary

| Level | Meaning |
|---|---|
| `CONFIRMED` | Reproduced with a targeted hardware experiment. |
| `STRONG` | Repeated correlation plus an independent physical measurement. |
| `HIGH` | Repeated captures agree, but one independent confirmation is missing. |
| `MEDIUM` / `LOW` | Useful working interpretation only. |
| `UNKNOWN` | No stable semantic assignment. Preserve the byte unchanged. |
| `DERIVED` | Calculated by the workbench, not transmitted directly. |

## Participants and direction

- The **Display MCU** sends one- or two-byte read requests and 19-byte `AA`
  control frames.
- The **Main MCU** answers read requests with an 18-byte response and accepts a
  valid `AA` frame with the single byte `CC`.
- `A5 E5 B2 E5 E1` is not one long header. Each item is a separate
  transaction with its response in between.

```text
Display                              Main MCU
   | -- A5 ----------------------------> |
   | <------- 16 Data + SUM16 (18 B) --- |
   | -- E5 B2 --------------------------> |
   | <------- 16 Data + SUM16 (18 B) --- |
   | -- E5 E1 --------------------------> |
   | <------- 16 Data + SUM16 (18 B) --- |
   | -- AA + 16 Data + SUM16 (19 B) ---> |
   | <----------------------------- CC -- |
```

## Frame boundaries and checksum

### Main MCU response

```text
raw[0..15] = Data[0..15]
raw[16]    = SUM16_H
raw[17]    = SUM16_L
```

### Display `AA` control frame

```text
raw[0]     = AA header
raw[1..16] = Data[0..15]
raw[17]    = SUM16_H
raw[18]    = SUM16_L
```

The `AA` header is not included in the checksum.

```text
S       = sum(Data[0..15])
SUM16_H = (S >> 8) & 0xFF
SUM16_L = S & 0xFF
```

The final two bytes are always checksum bytes. The former interpretation of
the penultimate byte as a Meta/page/dataset ID was wrong. A receiver must never
use checksum bytes to select a payload state.

The Main MCU sends `CC` only for an accepted `AA` transaction. Captures with an
invalid checksum must be preserved for analysis but must not be treated as a
valid command.

## Requests

| Request | Bytes | Response | Purpose |
|---|---:|---:|---|
| `A5` | 1 | 18 | Main operating state, dispensing, temperatures, errors, voltage |
| `E5 B2` | 2 | 18 | Tanks, filtering/fill, TDS and booster-pump power |
| `E5 E1` | 2 | 18 | Feed-pump control parameter and unknown state |
| `AA …` | 19 | `CC` | Display command/desired state |

The display polls continuously. A captured representative cycle is roughly
400–450 ms and contains two `A5` requests plus one `B2`, one `E1` and the
relevant `AA` control state. Exact timing is behavioral, not part of checksum
or framing. A robust implementation waits for a response or a short timeout
without turning the timeout into a multi-second pacing delay.

## `A5` response map

All offsets below are logical `Data[]` offsets.

| Offset | Meaning | Encoding | Confidence |
|---:|---|---|---|
| 0 | Unknown | byte | `UNKNOWN` |
| 1 | Dispensing state | bit 0: `1` active, `0` inactive | `LOW` for exact bit semantics; strong correlation |
| 2 | Outlet temperature | unsigned °C; meaningful during active hot dispensing | `CONFIRMED` |
| 3..4 | Current dispensed volume | uint16 big-endian, ml | `CONFIRMED` |
| 5..8 | Total dispensed amount | uint32 big-endian, ml | `MEDIUM..STRONG` |
| 9..10 | Error bitfield | mapping below | `STRONG` |
| 11 | Operating-state bitfield | mapping below | mixed |
| 12 | Inlet temperature | unsigned °C | `CONFIRMED` |
| 13 | Heating/control level | low nibble; exact scaling incomplete | `HIGH` |
| 14 | AC mains voltage | unsigned volts | `CONFIRMED` |
| 15 | Unknown | often `00` | `UNKNOWN` |

### `A5 Data[9]` error bits

| Bit | Display error | Confidence |
|---:|---|---|
| 6 | E0 | `STRONG`, cause unknown |
| 5 | E6 | `STRONG`, cause unknown |
| 0 | E8 | `STRONG`, cause unknown |
| 1,2,3,4,7 | Unknown | `UNKNOWN` |

### `A5 Data[10]` error bits

| Bit | Display error | Confidence |
|---:|---|---|
| 7 | E9 | `STRONG`, cause unknown |
| 6 | E7 | `STRONG`, cause unknown |
| 5 | E2 / inlet-temperature fault candidate | `STRONG` |
| 4 | E5 | `STRONG`, cause unknown |
| 3 | E4 | `STRONG`, cause unknown |
| 2 | EA | `STRONG`, cause unknown |
| 1 | Unknown | `UNKNOWN` |
| 0 | E3 / outlet-temperature fault | `CONFIRMED` |

### `A5 Data[11]` state bits

| Bit | Meaning | Active level | Confidence |
|---:|---|---:|---|
| 7 | Base/operating flag | 1 | `LOW` |
| 6 | 900 W heater control | 1 | `MEDIUM` |
| 5 | 1200 W heater control | 1 | `MEDIUM` |
| 4 | Cyclic/ready-like flag; not a stable process mode | varies | `UNKNOWN` |
| 3 | Unknown | — | `UNKNOWN` |
| 2 | Internal tank high mirror | `0` = at/above HIGH (full) | `CONFIRMED` |
| 1 | Flush process active | 1 | `STRONG` |
| 0 | Internal tank low mirror | `1` = below LOW | `CONFIRMED` |

At 25 °C the Main MCU does not continuously transmit a meaningful outlet
temperature. The UI therefore displays no value until a hot dispensing process
is active rather than presenting a stale byte as temperature.

## `E5 B2` response map

| Offset | Meaning | Encoding | Confidence |
|---:|---|---|---|
| 0..2 | Unknown | bytes | `UNKNOWN` |
| 3 | TDS/conductivity raw value | uint8; converted using captured thresholds | `CONFIRMED` raw mapping |
| 4 | Tank/status bitfield | mapping below | mixed |
| 5..10 | Unknown | bytes | `UNKNOWN` |
| 11 | Process/status bitfield | mapping below | mixed |
| 12 | Unknown | byte | `UNKNOWN` |
| 13 | TDS measurement state | bit 0 candidate | `HIGH` |
| 14 | Booster-pump electrical power | approximately 1 digit = 1 W | `STRONG` |
| 15 | Observed request-family marker `B2` | invariant in known replies; semantic role unproven | `HIGH` observation |

### `E5 B2 Data[4]` tank inputs

| Bit | Physical state | Wire polarity | Confidence |
|---:|---|---|---|
| 5 | Internal tank HIGH/MAX | `0` = at/above HIGH/full; `1` = below HIGH | `CONFIRMED` |
| 3 | Internal tank LOW/MIN | `1` = below LOW; `0` = above LOW | `CONFIRMED` |
| 0 | External tank | `1` = empty/unavailable; `0` = available | `CONFIRMED` |
| 1,2,4,6,7 | Unknown | — | `UNKNOWN` |

The HIGH display wording must be based on the physical state, not the raw bit:
raw `0` means **FULL / AT HIGH**, not “below high”.

Internal LOW is an input to the process controller, not a direct “start
filter” or “abort filter” command. Captures show LOW can change while FILTER
continues. New normal dispensing is blocked below LOW, but an already active
filter/fill process continues until its own HIGH/end condition.

### `E5 B2 Data[11]` process bits

| Bit | Meaning | Confidence |
|---:|---|---|
| 4 | TDS update/latch strobe used by current decoder | `HIGH` |
| 3 | Periodic scheduler/refresh pulse (about 20 s), not the numeric TDS value | `HIGH` |
| 2 | Booster/filter/fill process active | `CONFIRMED` |
| 1 | Wastewater flush valve energized/open | `CONFIRMED` |
| 0,5,6,7 | Unknown | `UNKNOWN` |

The current decoder commits `Data[3]` when the update strobe falls and also
uses `Data[13].bit0` as a measurement-valid indication. A previous note placed
the strobe in a different bit; new captures should retain all involved bytes so
this timing qualifier can be independently reconfirmed. The numeric threshold
table itself is documented in `uart_protocol_workbench/Documentation/TDS_Mapping.md`.

### Booster-pump power at `E5 B2 Data[14]`

`Data[14]` follows the booster pump and is approximately watt-scaled. A
three-day VictoriaMetrics comparison with an independent Shelly mains meter
gave a total correlation of about `r=0.95`. During a stable run the UART value
was mainly 31–38 while the Shelly reported about 31–33 W. Short UART start
peaks occur earlier and higher because the Shelly output is slower/averaged.

The value is **not total appliance power**:

- only the booster-pump measurement is represented;
- feed-pump power is not included;
- 900/1200 W heater power is not included;
- the one-byte field cannot represent the heater range.

The exported metric is `osmofresh_quella_booster_pump_power_watt`. The older
`osmofresh_quella_b2_d14_candidate_raw` remains only as a deprecated dashboard
compatibility alias.

## `E5 E1` response map

| Offset | Meaning | Encoding | Confidence |
|---:|---|---|---|
| 0 | Unknown | byte | `UNKNOWN` |
| 1..2 | Feed-pump control parameter | uint16 big-endian, interpreted as ml/min | `MEDIUM` |
| 3..14 | Unknown | bytes | `UNKNOWN` |
| 15 | Observed request-family marker `E1` | invariant in known replies; semantic role unproven | `HIGH` observation |

Observed control values include 750, 1000 and 1200. This is a control
parameter, not proof of an electrical feed-pump measurement.

## `AA` display commands

`AA` is one framing header followed by 16 payload bytes and SUM16. Known
payload families are distinguished by `Data[15]`. This is an observed payload
classification, not the removed checksum/Meta theory.

### `AA00` — dispense/setpoint family (`Data[15] = 00`)

| Offset | Meaning | Encoding | Confidence |
|---:|---|---|---|
| 0 | Unknown; observed `00` | byte | `UNKNOWN` |
| 1 | Dispense command | `01` start, `80` active refresh/idle family, `00` abort/stop | `CONFIRMED` |
| 2 | Requested temperature | unsigned °C; display 100 °C uses 95 (`0x5F`) | `CONFIRMED` |
| 3..4 | Requested volume | uint16 big-endian, ml | `CONFIRMED` |
| 5..14 | Unknown/reserved in known commands | preserve unchanged | `UNKNOWN` |
| 15 | Family marker | `00` | observed |

Examples:

```text
150 ml / 25 °C start payload:
00 01 19 00 96 00 00 00 00 00 00 00 00 00 00 00

10 ml / 25 °C start frame:
AA 00 01 19 00 0A 00 00 00 00 00 00 00 00 00 00 00 00 24
```

For a display-initiated override, later `D1=80` refreshes may be rewritten to
the selected target. A valid `D1=00` is a user abort and must pass immediately;
it must never be suppressed merely to finish the override target.

### `AA A3` — process family (`Data[15] = A3`)

| Offset | Meaning | Values | Confidence |
|---:|---|---|---|
| 4 | Requested process mode | `68` BASE, `78` FILTER, `7C` FLUSH | `HIGH` working map |
| 0..3, 5..14 | Unknown/process parameters | preserve captured values | `UNKNOWN` |
| 15 | Family marker | `A3` | observed |

`Data[4]` is the primary commanded mode. `Data[5]=02` is not a universal
start/stop condition: real startup captures show valid `68 → 78` transitions
with `Data[5]=00`.

Confirmed BASE/abort payload used by the safety controller:

```text
00 00 00 00 68 02 00 00 00 00 00 00 00 00 00 A3
```

### `AA E1` — feed-pump parameter family (`Data[15] = E1`)

| Offset | Meaning | Encoding | Confidence |
|---:|---|---|---|
| 2..3 | Requested feed-pump parameter | uint16 big-endian, ml/min | `CONFIRMED` command location |
| 0..1, 4..14 | Unknown | preserve unchanged | `UNKNOWN` |
| 15 | Family marker | `E1` | observed |

## Process and safety observations

These behaviors combine original-display observations with explicit bridge
safety policy. They are not additional UART fields.

- `BASE=0x68`, `FILTER=0x78`, `FLUSH=0x7C` are explicit process states.
- Internal HIGH is level-sensitive. FILTER must stop or be inhibited if HIGH is
  already active; safety must not rely only on a 0→1 edge.
- External empty inhibits new FILTER/FLUSH/dispense activity. The bridge latches
  the first empty observation so contact chatter cannot repeatedly start fill.
- Existing processes may finish where the configured policy permits, but other
  safety stops retain priority.
- Internal LOW does not by itself abort an active FILTER.
- When powered on below LOW, the display can request initial FILTER while
  dispensing remains locked. Crossing LOW unlocks dispensing but does not by
  itself stop FILTER.
- Normal manual injection does not disable safety. An explicitly labelled
  unsafe experimental override is separate and defaults OFF.

The external-empty acknowledgement sequence implemented by the bridge is:

1. latch the first valid external-empty level;
2. remain stopped once idle;
3. after water is restored, observe a START attempt while idle;
4. observe the user's following ABORT/idle transition;
5. clear the latch and permit fill again.

## Startup/default examples

These are captured bytes, not normalized templates:

```text
A5:
00 00 19 00 96 00 03 B8 7A 00 00 94 19 00 F9 00 03 8A

E5 B2:
00 00 00 00 20 0C 00 00 00 00 00 00 00 00 00 B2 00 DE

E5 E1:
00 02 EE 35 00 00 00 00 00 00 00 00 00 08 CA E1 02 D8
```

Startup replay maintains independent cursors for A5, E5 B2 and E5 E1.
`repeat: N` applies only to N matching requests; another request family must
not advance that cursor. `hold: true` retains the state indefinitely. Unknown
captured bytes and both checksum bytes are preserved exactly unless the user
explicitly edits data and asks for checksum recalculation.

## Remaining unknowns

The following are intentionally not assigned semantics:

- A5 Data[0], Data[15], most of Data[1], Data[9..10] unmapped bits,
  Data[11].bits3/4 exact meanings, and Data[13] upper bits;
- E5 B2 Data[0..2], Data[5..10], Data[12], most unused state bits, and the
  general role of Data[15];
- E5 E1 Data[0], Data[3..14], and the general role of Data[15];
- unused bytes in every AA family;
- causes of error codes E0, E4–E9 and EA unless named above;
- a direct feed-pump electrical measurement on UART;
- direct heater current/power on UART;
- whether every short booster-pump power transient is electrically calibrated
  or an internal control/ADC intermediate value.

Unknown bytes must remain visible in raw telemetry and must not be silently
replaced with defaults. Long-term analysis uses:

```text
osmofresh_quella_payload_byte_value{direction,frame,offset}
osmofresh_quella_payload_bit_value{direction,frame,offset,bit}
```

## Implementation references

- Authoritative checksum/layout: `uart_protocol_workbench/protocol/framing.py`
- Machine-readable meanings: `uart_protocol_workbench/config/protocol_knowledge.json`
- Live decoder: `uart_protocol_workbench/protocol/live_state.py`
- TDS thresholds: `uart_protocol_workbench/Documentation/TDS_Mapping.md`
- Bridge protocol: `uart_protocol_workbench/Documentation/Mega_Bridge_Protocol.md`
- ESP telemetry: `uart_protocol_workbench/Documentation/Telemetry_Protocol.md`

For reverse-engineering evidence, retain original timestamps, direction, raw
bytes and checksum validity. Derived UI values must never overwrite the raw
frame that produced them.
