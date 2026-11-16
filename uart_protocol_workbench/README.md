# UART Protocol Workbench

Dark-mode Python 3.11+ workbench for reverse-engineering the proprietary UART
link of a reverse-osmosis water-dispenser. The preferred transport is now an
ATmega2560 MITM bridge connected over its CH340G USB serial port. The legacy
direct USB-UART transport remains selectable. Its selectable role
either emulates the **Main MCU toward an isolated display PCB** or emulates the
**display toward a verified real Main MCU**. Both roles share capture, history,
live differences, checksums, datasets, statistics, and raw logging.

> Select the physical role before connecting. In **Main MCU Emulator**, the
> display must be completely disconnected from the real Main PCB. In **Display
> Emulator**, polling starts OFF, AA control frames start locked, and connecting
> is passive until the user explicitly sends or starts polling.

## Safety and ATmega2560 hardware

The observed UART is normally 9600 baud, 8 data bits, no parity, 1 stop bit
(8N1), idle HIGH, and measures approximately 5 V. The Quella baud remains
configurable. **Verify voltage and wiring before making any connection.**

Preferred MITM wiring, with a common signal ground:

```text
Display TX ----------> Mega RX1 pin 19
Display RX <---------- Mega TX1 pin 18

Main TX -------------> Mega RX2 pin 17
Main RX <------------- Mega TX2 pin 16

Display GND ---------- Mega GND ---------- Main GND

Mega Serial/USB <----- CH340G ------------ PC /dev/ttyUSB*

Pins 14/15 (Serial3) remain unused and reserved for the ESP8266.
```

Never connect two actively-driven TX outputs together. In emulator-only roles,
keep the isolated-PCB safety rules shown by the GUI. `DIRECT_PASS` is the
firmware default and continues forwarding if USB is unplugged or the GUI exits.

## Installation and launch

From this directory:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

For full passive raw capture over WiFi, select **Network UDP telemetry
(passive)** and bind `udp://0.0.0.0:42042`. The ESP target in `secrets.h` must
point to this PC and the host firewall must allow UDP port 42042. The WebSocket
option remains available for status/control events, but it is not the raw-frame
transport. Incoming UDP frames enter the same Live Changes, Live Status,
history, dataset, and statistics paths as USB captures. Both network transports
disable all emulation, injection, polling, and frame-send controls.

Each A5, E5 B2, and E5 E1 page in **Frame editor** has a **Live values** switch.
It overlays the newest received response in Hex, Decimal, Binary, and the bit
view. Live byte values are read-only; Known meaning, Category, and Confidence
remain editable and the saved response frame is never overwritten.

If UDP terminates on the homeserver, select **Home server HTTP state
(passive)** and use `https://osmofresh-quella.server.home/state`. The GUI polls
the read-only snapshot at 5 Hz and feeds new latest frames into the same Live
Status, Live Changes, history, and Frame Editor paths. This snapshot mode is
intended for monitoring; unlike direct UDP it cannot reproduce every
intermediate frame that occurred between two HTTP polls.

On Linux, serial-port access may require membership in the system's serial
device group. Fix permissions through the operating system rather than running
the GUI as root.

To run the protocol tests:

```bash
pytest -q
```

## ATmega2560 bridge firmware

Firmware: [`firmware/quella_uart_bridge/quella_uart_bridge.ino`](firmware/quella_uart_bridge/quella_uart_bridge.ino)

With Arduino CLI and the AVR core installed:

```bash
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:mega firmware/quella_uart_bridge
arduino-cli upload -p /dev/ttyUSB0 --fqbn arduino:avr:mega firmware/quella_uart_bridge
```

The firmware uses fixed-size rings and records, no `String`, no delays and no
blocking text parser in its bridge path. USB events use a CRC16-protected binary
envelope documented in
[`Documentation/Mega_Bridge_Protocol.md`](Documentation/Mega_Bridge_Protocol.md).
Normal forwarding is serviced before USB parsing/reporting. Injection waits for
a complete known frame boundary or the configurable idle gap.

The optional onboard ESP8266 gateway firmware lives in
[`firmware/quella_esp_gateway`](firmware/quella_esp_gateway). It exchanges only
semantic, CRC-protected status and commands with Mega Serial3; it never creates
raw Quella frames or makes appliance safety decisions. OTA is intentionally not
part of the current gateway version.

### First display-only hardware test

1. Upload the firmware with only the Mega connected by USB.
2. Disconnect the real Main MCU completely. Connect Display TX to RX1 pin 19,
   Display RX to TX1 pin 18, and connect grounds. Do not use Serial2 yet.
3. Start `python main.py`, select **ATmega2560 Bridge**, `/dev/ttyUSB0`, PC baud
   `115200`, and Quella baud `9600`.
4. Open **Mega Bridge**, select `DISPLAY_ONLY_TEST`, keep sniffing enabled and
   apply the configuration.
5. Connect. Confirm `HELLO`, then observe `DISPLAY_TO_MAIN` traffic in Live UART,
   Raw RX and the bridge event log.
6. Do not inject anything during this first capture. Validate frame boundaries,
   direction and baud first.
7. For the second test only, use one already-known harmless preset and watch for
   `INJECT_QUEUED` followed by `INJECT_SENT`.

The **Mega Bridge** tab now exposes the first deliberately narrow manipulation
rule for an exact known AA00 dispense command. It supports PASS, SUPPRESS, and
volume REPLACE with firmware-side SUM16 recalculation, counters, and original /
replacement diagnostics. Manipulation starts disabled. A 2-second heartbeat
lease returns firmware to PASS if the GUI or USB control link disappears. See
[`Documentation/Mega_Bridge_Protocol.md`](Documentation/Mega_Bridge_Protocol.md)
for the wire format, buffering latency, and the safe first hardware test.

## Known protocol

Requests are separate transactions. `A5 E5 B2 E5 E1 A5 AA` is **not** one
header; responses occur between the individual requests.

| Display → emulator | Emulator → display |
|---|---|
| `A5` | configured A5 response (18 bytes) |
| `E5 B2` | configured E5 B2 response (18 bytes) |
| `E5 E1` | configured E5 E1 response (18 bytes) |
| `AA` + 16 Data + SUM16_H + SUM16_L | `CC`, only when SUM16 is valid |

The stream parser retains fragmented requests (for example `E5` and then `B2`)
and handles several frames in one read. Unknown bytes are logged rather than
silently discarded. The Raw RX tab also shows every read exactly as received.

For every 18-byte response:

```text
bytes 0..15 = Data[0..15]
byte 16     = SUM16 high byte
byte 17     = SUM16 low byte
SUM16       = integer sum(Data[0..15])
SUM16_H     = (SUM16 >> 8) & 0xFF
SUM16_L     = SUM16 & 0xFF
```

For a display status frame, the layout is `AA + 16 Data + SUM16_H + SUM16_L`.
The `AA` header does not participate in SUM16. The frame editor recalculates
both checksum bytes after an explicit Data edit. Captured bytes are otherwise
transmitted unchanged. Disable **Auto checksum** only for deliberate manual
checksum experiments.

All consumers use one shared complete-frame layout descriptor. For 18-byte
A5/E5 responses raw offsets 16 and 17 are always `SUM16 High` and `SUM16 Low`;
for 19-byte AA frames raw offset 0 is the header, offsets 1..16 are Data[0..15],
and offsets 17/18 are SUM16. Live Changes displays raw and logical offsets
together, classifies checksum changes as `CHECKSUM` with confirmed meanings
`SUM16_H`/`SUM16_L`, and can hide both checksum bytes without hiding the payload
change that caused them.

### Correction of the former Meta hypothesis

The former interpretation of byte 16 as a Meta/Dataset ID was incorrect. Values
such as E5 B2 `00 DE` and `01 38` are the high and low bytes of the payload sum.
Checksum bytes must never identify a page, choose a response, split comparison
history, or drive round-robin behavior.

Strongly different captures are retained as named **observed payload states**.
Their discriminator remains `UNKNOWN`; static emulation always sends the
explicitly selected state. For AA only, Data[15] currently provides candidate
classes: `00` control/setpoint, `A3` system/process and `E1` system/process.
This classification is a payload hypothesis and is not derived from SUM16.

Editable startup frames live in
[`config/default_frames.json`](config/default_frames.json). Profiles can save
and restore port settings, delay, auto-response state, frame values, and each
frame's auto-checksum setting.

Reverse-engineering annotations are stored separately in the human-readable
[`config/protocol_knowledge.json`](config/protocol_knowledge.json). A5, E5 B2
and E5 E1 store every Data byte once per frame type. AA instead stores separate
byte and bit meanings for its `Data[15]=00`, `Data[15]=A3` and `Data[15]=E1`
payload classes; only the AA header and SUM16 descriptions are shared. Every
Data offset has a category:
`UNKNOWN`, `BITFIELD`, `VALUE`, `ENUM`, `COUNTER`, `TEMPERATURE`, or `POWER`. Only a
`BITFIELD` contains eight individual bit descriptions; choosing any other
category removes `bits` from the JSON and disables the bit editor. Hexadecimal
and decimal byte cells are both editable and update one another automatically.
Edit byte summaries in **Known meaning** and choose **Confidence** from its
dropdown. Changes are saved immediately and are not tied to a serial profile.

The consolidated field map, behavioral findings and explicit unknown list are
maintained in
[`../Documentation/UART_Protocol_Complete.md`](../Documentation/UART_Protocol_Complete.md).

## Knowledge reset

All field meanings, categories, confidence values and bit descriptions were
reset to `UNKNOWN` when the checksum model was corrected. Raw captures remain
available, but no earlier interpretation is carried forward automatically.

## Operating modes and tools

The prominent **Emulation role** selector changes both protocol behavior and
direction labels:

| Role | Receive direction | Transmit direction |
|---|---|---|
| Main MCU Emulator | `DISPLAY -> EMU` | `EMU -> DISPLAY` |
| Display Emulator | `MAIN -> EMU(DISPLAY)` | `EMU(DISPLAY) -> MAIN` |

### Display Emulator

Connecting in this role is always passive: no request is transmitted and the
UI reports `NO TRAFFIC OBSERVED` when the Main MCU remains silent. The Display
Request Scheduler can independently enable A5, E5 B2, E5 E1, and AA. Editable
default post-request delays are 80 ms, 80 ms, 130 ms, and 0 ms. **Start
polling**, **Stop polling**, **Step next frame**, and **Send once** provide
continuous and controlled manual operation.

A5/E5 transactions expect exactly 18 bytes. Fragmented serial reads are
collected against the pending request, SUM16 is validated, and the trace marks
`VALID SUM16`, `INVALID SUM16`, `TIMEOUT`, or an unexpected response/length.
AA expects `CC`. Latency count, min, max, average, median, jitter, and timeout
count are maintained independently for all four request types.

AA uses a generic, freely named 16-byte payload sequence. Presets can be added,
duplicated, removed, reordered, held, or used round-robin; this does not infer
semantics from Data[15] and is unrelated to the removed Meta model. Automatic
and manual AA transmissions are blocked until **Allow AA control frames** is
checked. AA scheduling is separately disabled by default, missing-CC retry is
OFF by default, and loading any profile forces polling and AA control back OFF.

In Display Emulator role, the **Frame editor** switches from Main-MCU response
frames to three AA payload editors for the currently distinguished `00`, `A3`
and `E1` classes. Each provides byte formats, class-specific protocol knowledge,
bit editing and a calculated AA/SUM16 preview. A5, E5 B2 and E5 E1 can be sent
once directly from the Frame editor header; these buttons do not start polling.

Manual raw HEX sending works in both roles. Optional **Append SUM16** accepts
either 16 payload bytes or `AA` plus 16 payload bytes; when unchecked, bytes are
sent exactly as entered.

- **Capture startup without responding** opens the UART and records traffic but
  transmits nothing. **Start emulation** switches modes without reconnecting.
- Automatic response uses an explicitly selected observed payload state and a
  configurable delay (1 ms default). There is no checksum-driven round-robin.
  Invalid AA checksums receive no acknowledgement.
- Live traffic has millisecond timestamps, direction colors, filters, save,
  copy, pause, and clear. Pausing affects display only, not capture or response.
- Manual send validates hexadecimal input and sends exactly once per click.
- The single-bit test previews original/modified bytes and checksums and changes
  a value only after **Apply Test Value** is clicked.
- Experiment rows record dataset, byte, bit, before/after values, visible/error/
  LED/buzzer effects and notes, and export to JSON or CSV.
- History compares a selected frame only with the preceding frame of equal
  direction, type, real payload class, and length. Dataset Inspector and
  Statistics include a payload-class histogram and unique-payload counts.
- Statistics include request counts, AA validity, unknown bytes, responses, and
  measured average/minimum/maximum response latency.

## Captured startup replay

The **Startup / Replay** tab adds a request-driven layer without changing
static emulation. It never sends startup frames on a timer. An A5 response is
only sent after A5, an E5 B2 response only after E5 B2, and an E5 E1 response
only after E5 E1. Each dataset has an independent queue, so polling order does
not need to match the original capture exactly.

Four modes are available:

- **Static** uses the current Frame Editor values.
- **Startup Replay** advances the matching dataset queue automatically and can
  switch to Static when all configured sequences have reached their final state.
- **Manual Step** holds each state until **Next Step** authorizes one transition.
- **Capture/Learn** records complete parsed requests and relative timing while
  transmitting nothing automatically.

`repeat: N` sends that exact captured state for N matching requests; requests
for other datasets do not affect its counter. A step with `hold: true` remains
active indefinitely and is never treated as replay completion. The state panel
shows each current name and repetition (for example `A1 repetition 2/4`) or
`HOLD`. **Reset Startup Sequence** atomically returns every independent cursor
and repetition counter to its first state.

Profiles are editable JSON files in
[`config/startup_profiles`](config/startup_profiles). A step can define a
response, repeat count, hold behavior, comment, timeout, transition rule, and
for AA triggers an `expected_aa` plus `aa_mask`. Masked matching uses
`(received & mask) == (expected & mask)` and permits a shorter prefix pattern.
Complete AA frames are checksum-validated, shown in the trace, and acknowledged
with CC only when valid.

The bundled `real_main_startup.json` contains the current exact A5, E5 B2 and
E5 E1 captures, including both transmitted SUM16 bytes. Captured bytes are
never silently corrected. Invalid stored/calculated SUM16 values are displayed
as warnings. The explicit
**Recalculate checksums…** action changes them only after confirmation.

The comparison table shows the current replay response against the current
Static response byte-by-byte, including XOR and changed bits. TDS observations
are appended to `logs/tds_observations.json` with the phase, all three current
responses, last AA frame, timestamp, visible TDS value, and notes. Protocol
snapshots and replay/capture traces can also be exported as JSON.

## Shared Data fields

The Frame Editor can mark an individual Data byte as **Shared across payload
states**. Shared fields belong to one request type and appear once in the
separate Shared Data table.

When a field becomes shared, the selected payload state supplies its initial
value. Editing it affects every payload state and recalculates both SUM16 bytes.
Switching it back copies its last value and metadata to all existing states.
Definitions and annotations are stored request-wide in
`protocol_knowledge.json`.

## Live Changes and button correlation

The **Live Changes** tab brings the recurring-frame diff workflow into the
emulator, so live reverse engineering no longer requires a separate program.
It observes complete received requests/status frames and the exact response
bytes that were actually transmitted. Frames are isolated by direction, type,
real payload class, and length. SUM16 High is never used as identity. AA
control/setpoint and system/process candidates keep independent references.

Every changed byte is shown with its logical offset, before/after values in hex
and decimal, XOR, and each `bitN 0→1` or `1→0` transition. Selecting a row also
shows binary values, both complete frames, and matching descriptions from
`protocol_knowledge.json` when available. Comparisons can use the previous
frame of the same group or captured baseline variants. **Capture Baseline
Variants** observes traffic for a configurable window (three seconds by
default) and stores every unique state per direction, frame type, and payload class.
Regular alternation between captured states is then filtered out, including
matching A↔B transitions that were already present in the change table.

To correlate a physical display-button press, enter/select an action name and
click **Mark Button Press**, press Enter, or use the global **F8** shortcut. All
changes inside the configurable correlation window carry the action name and
their millisecond offset from the marker. Rows can also be selected and labelled
after the experiment. The list exports losslessly to JSON or CSV. **Pause table
view** only freezes rendering; frame collection and change detection continue.

## First test procedure

1. Disconnect the display completely from the Main PCB.
2. Power it from the verified appropriate external supply.
3. Connect the logic analyzer.
4. Select **Capture startup without responding**.
5. Power-cycle the display.
6. Check whether it independently sends A5, E5 B2, E5 E1, or AA.
7. Start emulation using the unmodified known frames.
8. Verify stable operation and compare timings on the logic analyzer.
9. Record a baseline window before changing any unknown field.
10. Change only one byte or bit, keep auto checksum enabled, and document the
    observed result without inferring a meaning prematurely.
