# ATmega2560 UART bridge protocol

The bridge uses `Serial` for the CH340G/PC control link, `Serial1` for the
display MCU, `Serial2` for the Main MCU, and `Serial3` at 115200 baud for the
ESP8266 semantic gateway. Serial3 uses the separate CRC-protected `QG`
protocol documented in [Gateway_Protocol.md](Gateway_Protocol.md); it is not a
raw mirror of either the Quella UART or the USB `QB` stream.

## Pins

| Side | Mega RX | Mega TX |
|---|---:|---:|
| Display MCU / Serial1 | 19 | 18 |
| Main MCU / Serial2 | 17 | 16 |
| ESP8266 semantic gateway / Serial3 | 15 | 14 |

All grounds must be common. The measured Quella UART is normal 5 V, idle-HIGH
UART. Never connect two TX outputs together.

## Binary USB envelope

All multi-byte integers are little-endian.

| Field | Size |
|---|---:|
| Magic `QB` | 2 |
| Version (`1`) | 1 |
| Message type | 1 |
| Flags | 1 |
| Sequence | 2 |
| Payload length | 2 |
| Payload | 0..512 |
| CRC16-CCITT | 2 |

CRC covers Version through the final payload byte, using polynomial `0x1021`
and initial value `0xFFFF`. Magic is excluded so the decoder can resynchronize.

PC messages: `SET_CONFIG`, `START_SNIFF`, `STOP_SNIFF`,
`INJECT_DISPLAY_TO_MAIN`, `INJECT_MAIN_TO_DISPLAY`, `SET_BRIDGE_MODE`, `PING`,
`SET_RULE`, `CLEAR_RULE`, `ENABLE_RULE`, `DISABLE_RULE`, `GET_RULES`,
`COMMAND_DISPENSE`.
Firmware messages: `HELLO`, `STATUS`, `RX_FRAME_DISPLAY`, `RX_FRAME_MAIN`,
`INJECT_QUEUED`, `INJECT_SENT`, `ERROR`, `PONG`, `RULE_STATUS`, `RULE_MATCH`,
`RULE_REPLACED`, `RULE_ERROR`, `REPLACE_BUILD`, `REPLACE_TX_BEGIN`, and
`REPLACE_TX_END`.
Manual-command messages are `MANUAL_CMD_QUEUED`, `MANUAL_CMD_TX_BEGIN`, and
`MANUAL_CMD_TX_END`.

`SET_CONFIG` payload is `<mode:u8, quella_baud:u32, idle_gap_us:u32,
sniff_enabled:u8>`. Mode `0` is transparent `DIRECT_PASS`; mode `1` is
`DISPLAY_ONLY_TEST`, where Serial1 is captured but not forwarded to the absent
Serial2 peer. Main-to-display traffic, if present, is still forwarded.

RX payloads contain `<micros_timestamp:u32, byte_count:u16, bytes...>`.
Injection requests use the envelope sequence as queue ID. The firmware answers
with `INJECT_QUEUED`, then `INJECT_SENT` after the complete frame has entered
the target UART TX ring.

The current firmware limits an individual injection/capture record to 64 bytes.
The USB control link starts at `115200` baud (`PC_BAUD` in the sketch); the GUI
keeps this PC baud separate from the dynamically configurable Quella baud.

## Safe injection

Known display frames have lengths 1 (`A5`), 2 (`E5 B2`, `E5 E1`) and 19
(`AA + Data[0..15] + SUM16`). Expected Main responses are 18 bytes for A5/E5
and one byte (`CC`) for AA. Unknown/partial traffic is closed by the configured
idle gap. Injection is eligible only when both directions have been idle for
that gap and neither tracker contains a partial frame. The entire injected
frame must fit in the target TX ring before its first byte is queued.

## Selective rule interception

The first supported rule is deliberately narrow: a direction-bound, exact
16-byte match for a known `AA Data[15]=00` dispense-start payload
(`Data[1]=01`). `SET_RULE` is:

`<version:u8=1, type:u8=1, direction:u8, action:u8, enabled:u8,
override_ml:u16, exact_payload[16]>`.

Actions are `PASS=0`, `SUPPRESS=1`, and `REPLACE=2`. For `REPLACE`, only
payload `Data[3..4]` is changed to the big-endian volume; the firmware then
recalculates `SUM16_H/SUM16_L` over `Data[0..15]`. The original checksum is
validated before matching. Wrong direction, unknown/partial/ambiguous frames,
bad checksums, and non-exact payloads always take the byte-exact PASS path.

Only a possible AA frame in the configured direction is held. All unrelated
bytes remain on the streaming direct-pass path. At 9600 baud a complete
19-byte AA candidate is delayed by approximately 19.8 ms before forwarding.
This complete-frame delay is the principal timing risk and must be checked on
the real appliance. Invalid and nonmatching candidates are still forwarded
unchanged after validation.

An enabled rule has a 2-second PC lease. The GUI sends a framed heartbeat every
500 ms. Firmware boot, `DISABLE_RULE`, `CLEAR_RULE`, a crashed GUI, a stopped
heartbeat, or a disconnected USB link all leave/return the bridge in PASS.
Normal UART forwarding does not require the PC connection.

For a held AA candidate, PASS and REPLACE now use the same local completed-frame
transmitter. `DISPLAY_TO_MAIN` writes directly to `Serial2` (TX pin 16), while
`MAIN_TO_DISPLAY` writes to `Serial1` (TX pin 18). It drains older direct-pass
bytes first, calls `HardwareSerial.write(buffer, length)`, records its return
value, and calls `flush()` so `REPLACE_TX_END` represents physical completion
instead of mere admission to a software ring. No injection queue and no
PC round-trip participates in this path.

The expected 150-to-151 ml example is:

```text
original:    AA | 00 01 19 00 96 00 00 00 00 00 00 00 00 00 00 00 | 00 B0
replacement: AA | 00 01 19 00 97 00 00 00 00 00 00 00 00 00 00 00 | 00 B1
```

`Data[3..4]` is the big-endian volume, so 150 is `00 96` and 151 is
`00 97`. The header is excluded from SUM16. The final two bytes are SUM16,
therefore `B0 + 1 = B1` is correct.

`RULE_STATUS` returns configured/enabled/direction/action/type, override,
32-bit match and replacement counters, and the last original/replacement
frames. `RULE_MATCH` and `RULE_REPLACED` are also emitted at the event itself;
all are normal CRC-protected `QB` messages—raw Quella bytes are never written
unframed to the PC control UART.

Replacement diagnostics provide the original/replacement lengths and frames,
both checksum pairs, destination UART, the final-original-byte timestamp,
TX-begin timestamp, `write()` byte count, and post-`flush()` TX-end timestamp.
At 9600 8N1, a 19-byte physical transmission is expected to take about
`19 × 10 / 9600 = 19.79 ms`; `REPLACE_TX_BEGIN` also reports the locally
measured delay from the final received source byte to the start of TX.

### Safe first test

1. Flash the firmware and connect with manipulation disabled. Compare captures
   against the previously validated transparent bridge.
2. Select `[AA 00] Dispense`, direction `DISPLAY_TO_MAIN`, action `REPLACE`, and
   an unmistakable but safe test volume.
3. Press **Set rule**, then **Enable rule**. Confirm `RULE_STATUS` says enabled.
4. Trigger exactly that dispense preset. Confirm one match/replacement event,
   the expected `Data[3..4]`, and a valid recalculated SUM16.
5. Trigger another AA class or alter any matched payload byte. It must be passed
   unchanged and must not increment the match counter.
6. Disconnect USB or close the GUI for more than two seconds. The bridge must
   continue direct traffic with the rule disabled.

## Semantic manual DISPENSE test

The Mega Bridge tab's **SEND DISPENSE** button does not send arbitrary UART
bytes from the PC. It sends `COMMAND_DISPENSE` with one little-endian `u16`
volume. The remote/manual range is `1..1500 ml`. The original Display itself
was captured only up to 900 ml; values above that are intentionally available
through the remote path. The command requires `DIRECT_PASS` and is rejected
while a manipulation rule is active.

Firmware builds the already documented single AA00 command locally:

```text
AA | 00 01 19 volume_H volume_L 00 00 00 00 00 00 00 00 00 00 00 | SUM16_H SUM16_L
```

Temperature remains the known preset value `0x19` (25 °C). No additional
command sequence is currently documented, so none is invented. For 10 ml the
complete frame is:

```text
AA 00 01 19 00 0A 00 00 00 00 00 00 00 00 00 00 00 00 24
```

The semantic command enters a dedicated fixed-size queue. It is transmitted
only after both RX trackers are at frame boundaries and both UART directions
have been idle for the configured safe gap. There is no `delay()` and no use of
the generic PC raw-injection button. The destination is always Serial2 / TX pin
16 (`DISPLAY_TO_MAIN`). Firmware reports the full built frame, physical TX
duration, and `write()` count through `MANUAL_CMD_QUEUED`,
`MANUAL_CMD_TX_BEGIN`, and `MANUAL_CMD_TX_END`.

Display traffic is not suppressed. For one second after physical manual TX
completion, captured Display-to-Main frames are additionally marked
`MANUAL_FOLLOWUP_DISPLAY` in the GUI while continuing through the normal parser,
history, and forwarding paths. This distinguishes a successful isolated manual
command from a command immediately superseded by the real display.

The workbench cannot yet prove the appliance's complete READY state from a
confirmed passive field alone. The operator must therefore verify READY before
pressing the button. Firmware enforces the currently provable local conditions:
DIRECT_PASS, no active manipulation rule, valid bounded volume, complete frame
boundaries, and a safe inter-frame gap.

Normal dispensing is locked whenever a fresh E5 B2 reports Internal Tank
`BELOW LOW` (`D4.bit3=1`). New USB, gateway, and physical Display AA00 START
commands are rejected/suppressed. If a remote manual dispense is already
active when B2 changes to `BELOW LOW`, the Mega immediately sends the confirmed
AA00 abort. A stale or unknown Internal MIN state also rejects new remote
dispense commands. A3 process traffic is not classified as normal dispensing
and is not changed by this lock.

For manual 25 °C dispensing the workbench also accepts a feed-pump parameter
from 100 through 1200 ml/min. `1000` is the observed normal value and `1200`
the captured maximum. While that manual session is active, recurring AA E1
requests are replaced at raw offsets 3..4 and SUM16 is recalculated. Other E1
bytes are preserved. When the session ends, E1 returns to byte-exact PASS.

## Dispense Sequence Capture

The Mega Bridge tab can record a configurable 5–120 second evidence window for
four labelled experiments. The default is 20 seconds so a 150 ml run and its
stop transition fit into one capture:

- A — normal Display dispense, 150 ml
- B — normal Display dispense with AA00 suppressed
- C — manual Mega dispense, 10 ml
- D — Display 150 ml replaced with 10 ml

With auto-start enabled, a complete checksum-valid Display-to-Main AA00 frame
with `Data[1]=01` emits `DISPENSE_SEQUENCE_START` and becomes frame 1. Scenario
C starts immediately before **SEND DISPENSE**. No additional frames are
suppressed or replaced by the capture feature.

Every received frame in both directions is stored with the firmware
`timestamp_us`, unsigned `delta_us`, direction, raw bytes, decoded type,
checksum result, known protocol fields, semantic tags, and exact equality to
the preceding captured frame. Local manual and replacement transmissions are
also included as `MEGA_LOCAL_MANUAL_TX` or `MEGA_LOCAL_REPLACE_TX`, so C and D
show the frame which actually entered Serial2 rather than only surrounding RX
traffic.

Completed captures are automatically written as JSON Lines to:

```text
evidence/runtime/dispense_sequence_<scenario>_<timestamp>.jsonl
```

The GUI also provides explicit JSONL and CSV export. JSONL retains marker
records (`DISPENSE_SEQUENCE_START` / `DISPENSE_SEQUENCE_END`), nested known
fields, active known bits, and flags for volume, temperature, flow, and status.
CSV is flattened for spreadsheets.

After A, C, and D have been captured, create the requested first-pass comparison
table with:

```bash
python compare_dispense_captures.py \
  --a evidence/runtime/dispense_sequence_A_....jsonl \
  --c evidence/runtime/dispense_sequence_C_....jsonl \
  --d evidence/runtime/dispense_sequence_D_....jsonl \
  --output evidence/runtime/dispense_comparison.csv
```

The table contains frame number, per-capture deltas, direction, all three raw
frames, known meaning, and a deliberately conservative “Likely required?”
candidate column. Final required-state-machine conclusions must be based on the
real captures, not inferred by the exporter.

## Stateful dispense override

The confirmed dispense REPLACE path is session-based. An exact valid AA00
`Data[1]=01` START activates the session and replaces its volume. While active,
every valid AA00 `Data[1]=80` refresh from the same control family and original
Display volume is rebuilt with the same target and a freshly calculated SUM16.

The session records its origin. The currently implemented REPLACE path is
`DISPLAY_OVERRIDE`: the user initiated it on the real Display, so any later
valid same-family `Data[1]=00` is an authoritative user abort. Firmware marks
the override inactive before transmission, forwards the original frame
unchanged to Serial2, emits `DISPENSE_USER_ABORT` only after physical TX, and
observes the following CC and A5-idle transition while in an aborting cleanup
state. No later `Data[1]=80` is replaced after the abort was received.

`MANUAL/REMOTE` is a complete, distinct session controller. `SEND DISPENSE`
builds and transmits the confirmed AA00 START with the selected target, records
the exact AA00 control family, and emits `REMOTE_DISPENSE_START`. While A5 is
fresh and reports an active dispense below the target, same-family Display
`Data[1]=00` idle frames are suppressed (`REMOTE_DISPLAY_IDLE_SUPPRESSED`) and
same-family `Data[1]=80` refreshes are rebuilt with the remote target and a new
SUM16 (`REMOTE_REFRESH_REPLACED`). This policy never applies to
`DISPLAY_OVERRIDE`; its user abort continues to pass immediately and unchanged.

A5 is authoritative for remote progress and completion. Active frames emit
`REMOTE_DISPENSE_PROGRESS`. An A5 idle state at the target emits
`REMOTE_DISPENSE_COMPLETE` followed by `REMOTE_DISPENSE_END`. The explicit GUI
button `STOP REMOTE DISPENSE` first moves the controller into aborting state,
disables replacement/suppression, then sends the confirmed abort frame:

```text
AA 00 00 19 00 96 00 00 00 00 00 00 00 00 00 00 00 00 AF
```

The abort path emits `REMOTE_DISPENSE_ABORT_REQUESTED` before TX,
`REMOTE_DISPENSE_ABORT_TX` after a complete Serial2 write, observes CC, and
waits for A5 idle before `REMOTE_DISPENSE_ABORT_CONFIRMED` and
`REMOTE_DISPENSE_END`. Once aborting begins, all Display traffic is PASS.

Remote fail-safe handling uses the same confirmed abort when Main is believed
to be dispensing. It is triggered by invalid AA/A5 input, missing/freshness
timeout for A5, an expired PC heartbeat, TX failure, or a conflicting bridge or
rule mutation. If abort transmission cannot be completed, manipulation is
disabled, traffic returns to PASS, and `REMOTE_DISPENSE_ERROR` plus
`REMOTE_DISPENSE_END` are reported. The normal generic PASS, REPLACE and
SUPPRESS paths remain unchanged.

A5 remains byte-exact and authoritative. Its `Data[1]` and big-endian
`Data[3..4]` update Main state and progress. Main reporting idle ends the
session immediately; subsequent AA traffic passes normally. Invalid AA/A5
checksums, incomplete expected A5 responses, an A5 gap over 1.5 seconds, TX
failure, or expiry of the existing two-second PC lease fail open by disabling
the override and returning to PASS.

The Mega Bridge GUI shows active state, session origin, target/original amount,
refresh and blocked-idle counters, current A5 amount, Main dispensing state,
elapsed time, and the remote state `IDLE`, `STARTING`, `ACTIVE`, or `ABORTING`.
Firmware events for Display Override are `DISPENSE_OVERRIDE_START`,
`DISPENSE_REFRESH_REPLACED`, `DISPENSE_ABORT_SUPPRESSED`, `DISPENSE_PROGRESS`,
`DISPENSE_TARGET_REACHED`, `DISPENSE_USER_ABORT`, and
`DISPENSE_OVERRIDE_END`. The user-abort event includes the original AA frame,
target amount, and current A5 amount.

The first hardware validation deliberately does not inject an overrun abort.
Crossing the target while A5 still says dispensing is reported as
`DISPENSE_TARGET_REACHED`; this keeps the newly confirmed automatic Main cutoff
observable before enabling another active safety transmission.

## Tank / fill state and latched external-empty stop

The field audit uses the current `config/protocol_knowledge.json`, the existing
decoders, and captured runtime frames. The authoritative tank response is an
18-byte `E5 B2` response in direction Main-to-Display. Offsets below are logical
Data offsets and also raw response offsets because this response has no header:

| Frame | Offset / mask | Meaning | Polarity | Confidence |
|---|---:|---|---|---|
| E5 B2 | Data[4] / `0x08` | Internal tank MIN / BELOW LOW | 1 = below low | CONFIRMED |
| E5 B2 | Data[4] / `0x20` | Internal tank MAX / ABOVE HIGH | 0 = above high | CONFIRMED |
| E5 B2 | Data[4] / `0x01` | External tank water | 0 = available, 1 = empty | CONFIRMED |
| E5 B2 | Data[11] / `0x04` | Booster / filtration fill process active | 1 = active | CONFIRMED |
| A5 | Data[11] / `0x01` | Internal BELOW LOW mirror | 1 = below low | CONFIRMED |
| A5 | Data[11] / `0x04` | Internal ABOVE HIGH mirror | 0 = above high | CONFIRMED |

The external-bit polarity is also consistent with current captures: normal
dispensing occurs with B2 Data[4] `0x20`, where bit0 is clear. An older findings
copy and an older Display-role decoder used the opposite wording; neither is
used by this Mega safety path.

The first valid empty observation (`bit0=1`) immediately latches an external
stop. There is no longer a ten-second release delay. Dispense, flush, and heat
operations that were already active may finish from the internal tank. While
one of those operations remains active, the bridge masks only Data[4].bit0 as
available toward the Display. It never masks the other stop/safety inputs and
never masks external empty for the fill process itself. Once the appliance is
idle, external empty is exposed and new output/fill starts stay blocked.

Physical water availability (`bit0=0`) alone does not clear the latch. The
operator acknowledgement is deliberately two-stage while Main is idle:

1. after refilling the external tank, the first valid AA00 `D1=01` is
   suppressed and arms reset; no real dispense starts;
2. the following valid AA00 `D1=00` is passed unchanged, then the latch is
   cleared after transmission.

This preserves the normal second-button-press/IDLE interaction and prevents a
reed contact oscillating at its switching point from repeatedly starting fill.
Invalid/stale B2 data remains blocked and cannot acknowledge reset.

`SET_TANK_CONFIG` (`0x0F`) is retained for wire compatibility, but delay values
are ignored and firmware reports delay disabled/zero. `GET_TANK_STATUS`
(`0x10`) requests current state. Firmware reports `TANK_STATUS` (`0x94`) and
`TANK_EVENT` (`0x95`). Status flags include external-stop-latched and
reset-armed. New events are `EXTERNAL_STOP_LATCHED`, `EXTERNAL_RESET_ARMED`,
and `EXTERNAL_STOP_CLEARED`; the existing tank/fill events remain available.
## Persistent semantic volume overrides

The gateway path supports eight local `VOLUME_PRESET` rules. At the beginning
of a valid Display AA00 start, the Mega selects a rule by the original volume,
then reuses the existing stateful replacement/session-origin path. Refreshes
are replaced consistently; a Display AA00 D1=00 remains a user abort and is
passed immediately. Disabled or unmatched rules are transparent PASS.

User configuration is stored only after the explicit QG `SAVE_CONFIG`
command. The EEPROM record has a magic value, schema version and CRC16 and
contains tank delay, presets and the eight mappings. An invalid magic,
version, CRC or out-of-range rule loads safe compiled defaults. `EEPROM.put`
avoids rewriting unchanged bytes.
