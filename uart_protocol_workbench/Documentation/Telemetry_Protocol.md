# Quella raw telemetry protocol

This telemetry plane is read-only and best-effort. It carries complete raw
Quella UART frames from the Mega2560 through the ESP8266 to one Homeserver. It
contains no control command and has no authority over machine safety.

All multi-byte integers are little-endian. Raw Quella frame bytes remain in
their original wire order (including header and checksum where present).

## Mega ring and QG_RAW_BATCH

The Mega stores up to 32 completed frames in a fixed ring. One entry is:

| Field | Type | Meaning |
|---|---|---|
| direction | `u8` | `0` Display→Main, `1` Main→Display |
| type | `u8` | optional recognized frame ID, `0` means unknown |
| timestamp_us | `u32` | Mega `micros()` at receipt of the final byte |
| length | `u8` | raw length, `1..19` |
| raw | bytes | exact frame bytes |

The `micros()` value wraps naturally after 2^32 microseconds. Consumers must
compare it using modular arithmetic and must not treat it as wall-clock time.

QG message `0x87` is `QG_RAW_BATCH`. Its payload is:

| Offset | Type | Field |
|---:|---|---|
| 0 | `u8` | format version (`1`) |
| 1 | `u16` | batch sequence |
| 3 | `u8` | frame count |
| 4 | `u32` | cumulative Mega ring/drop counter |
| 8 | entries | frame entries in completion order |

The regular QG envelope supplies its CRC16. Its maximum payload is 192 bytes.
The Mega flushes when seven maximum-size frames are ready (the payload limit)
or when the oldest queued frame reaches 50 ms. Smaller frames may share the
same batch. The UART bridge and control messages always take priority.

On ring overflow the newest telemetry entry is discarded and the cumulative
drop counter increases. Appliance UART forwarding is never blocked. USB/QB
capture remains independent and immediate.

## QT v1/v2 UDP datagram

The ESP validates a complete QG batch, copies its entries without semantic
decoding, and sends one binary UDP datagram. There is no JSON conversion and
no retransmission.

| Offset | Size | Type | Field |
|---:|---:|---|---|
| 0 | 2 | bytes | ASCII magic `QT` |
| 2 | 1 | `u8` | version (`1` or `2`) |
| 3 | 1 | `u8` | flags; bit 0 means CRC16 present |
| 4 | 4 | `u32` | ESP UDP packet sequence |
| 8 | 4 | `u32` | ESP uptime in milliseconds |
| 12 | 1 | `u8` | frame count |
| 13 | 2 | `u16` | combined frame-entry length |
| 15 | 2 | `u16` | originating Mega batch sequence |
| 17 | 4 | `u32` | cumulative Mega raw-frame drops |
| 21 | 1 | `i8` | ESP WiFi RSSI in dBm (`v2` only) |
| 21 (`v1`) / 22 (`v2`) | variable | entries | same frame-entry encoding as above |
| final 2 | 2 | `u16` | CRC16-CCITT |

CRC16 uses polynomial `0x1021`, initial value `0xFFFF`, and covers byte 2
(version) through the final frame byte. The `QT` magic and stored CRC bytes
are excluded.

The ESP sequence increments for each send attempt. A received sequence jump,
for example `123, 124, 126`, lets the server count packet `125` as lost.
Duplicate and out-of-order datagrams are counted separately. UDP loss is
expected behavior and never triggers a control action.

QT v2 adds only the signed RSSI byte. Frame-entry encoding and the CRC rules
are unchanged. The Homeserver decoder accepts both versions; for QT v1 the
RSSI value is unavailable. RSSI is diagnostic telemetry only and has no
control or safety effect.

The ESP exposes `udp_packets_sent`, `udp_send_errors`, `raw_frames_received`,
`raw_frames_sent`, `raw_frames_dropped`, `qg_raw_batches_received`, and the
Mega cumulative drop counter from `/api/health`.

## Example

This symbolic packet contains an A5 request followed by its 18-byte response:

```text
QT | v=1 | flags=01 | seq=42 | esp_uptime_ms=123456
frames=2 | payload_len=... | mega_batch=17 | mega_dropped=0
  dir=0 type=1 ts_us=900000 len=1  raw=A5
  dir=1 type=8 ts_us=921100 len=18 raw=00 00 19 ... 03 8A
crc16=....
```

An exact QT v1 datagram containing one A5 request (`seq=42`, ESP uptime
`123456 ms`, Mega batch `17`, Mega timestamp `900000 us`) is:

```text
51 54 01 01 2A 00 00 00 40 E2 01 00 01 08 00 11 00 00 00 00 00
00 01 A0 BB 0D 00 01 A5 E8 17
```

## Receiver

Set the Homeserver address in `firmware/quella_esp_gateway/secrets.h`, flash
Mega and ESP, install dependencies, then run from the project directory:

```bash
python -m pip install -r requirements.txt
python main.py --telemetry
python main.py --telemetry --raw
python main.py --telemetry --record evidence/runtime/telemetry.jsonl

# Or use the dedicated receiver entry point:
python telemetry_receiver.py --raw
```

Plain `python main.py` continues to launch the existing PySide6 Workbench. The
telemetry receiver's default bind is `0.0.0.0:42042`; use `--bind` and `--port`
to override it.
The fixed live view retains the last decoded state when traffic becomes stale.
`--raw` prints one line per frame. JSONL recordings store server receive time,
ESP/Mega counters, exact raw frames, and any detected sequence gap.

The viewer reuses the project's existing `QuellaLiveState`, A5/E5/AA decoder,
captured TDS threshold mapping, and protocol knowledge. It does not introduce
new machine semantics. Mega timestamps, ESP uptime, and server wall time are
different clock domains, so one-way latency is not claimed without explicit
clock synchronization.
