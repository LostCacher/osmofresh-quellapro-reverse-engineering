# Quella ESP8266 gateway

The ESP8266 is a network transport only. Appliance state, tank delay, command
validation, dispense/stop behavior, and all fail-safe decisions remain on the
ATmega2560. UART0 carries only the CRC-protected `QG` protocol at 115200 baud;
it must never contain debug text or raw Quella frames.

OTA is intentionally not included. ESP updates currently use USB upload.

## Dependencies and secrets

```bash
arduino-cli core install esp8266:esp8266
arduino-cli lib install WebSockets
cp firmware/quella_esp_gateway/secrets.example.h \
   firmware/quella_esp_gateway/secrets.h
```

Edit `secrets.h`. The API key must contain at least 16 characters. All mutating
REST calls require it in `X-API-Key`; `secrets.h` is ignored locally.

Also set `QUELLA_TELEMETRY_HOST` to the IPv4 address of the Homeserver and
`QUELLA_TELEMETRY_PORT` to its UDP listen port (default `42042`). `0.0.0.0`
disables telemetry without affecting REST, WebSocket, or Mega control.

### Static network configuration

The local `secrets.h` is currently configured for:

- IP: `10.0.1.27`
- Gateway: `10.0.1.1`
- DNS: `10.0.255.2`
- Subnet: `255.255.0.0` (`/16`)

Set `QUELLA_WIFI_USE_STATIC_IP` to `0` to return to DHCP. The `/16` subnet is
intentional so the DNS server at `10.0.255.2` is reachable directly. If the
actual LAN uses a different subnet mask, adjust the four
`QUELLA_WIFI_SUBNET_*` values in `secrets.h` before flashing.

## Build and upload

For the board's usual 4 MB ESP8266 flash:

```bash
arduino-cli compile --fqbn esp8266:esp8266:generic \
  firmware/quella_esp_gateway
arduino-cli upload -p /dev/ttyUSB0 --fqbn esp8266:esp8266:generic \
  firmware/quella_esp_gateway
```

ESP upload DIP position on the common RobotDyn/WeMOS 8-switch board:

`1 OFF, 2 OFF, 3 OFF, 4 OFF, 5 ON, 6 ON, 7 ON, 8 OFF`.

Press `MODE/FLASH` when upload starts if required by the board revision. After
upload, leave flash mode before rebooting. Never move DIP switches while the
board is powered.

## USB WiFi diagnosis

For a temporary WLAN-only diagnosis, set this in `secrets.h`:

```cpp
#define QUELLA_WIFI_SERIAL_DEBUG 1
```

Compile and upload normally. For the subsequent serial-monitor run use:

`1 OFF, 2 OFF, 3 OFF, 4 OFF, 5 ON, 6 ON, 7 OFF, 8 OFF`.

This connects USB to the running ESP without connecting Mega Serial3. Open a
115200-baud serial monitor. The firmware prints the configured SSID and its
length, numeric/textual `WiFi.status`, a network scan after about five seconds,
and IP/gateway/subnet/RSSI/channel/BSSID after connection. It never prints the
password.

In this mode QG, REST, WebSocket, and all Mega communication are deliberately
not started. Return `QUELLA_WIFI_SERIAL_DEBUG` to `0`, rebuild, flash, and use
the normal `1+2 ON` routing after diagnosis. With `0`, UART0 remains exclusively
binary QG and the normal setup/loop path is unchanged.

The same diagnostic build can be selected without editing `secrets.h`:

```bash
arduino-cli compile --fqbn esp8266:esp8266:generic \
  --build-property 'compiler.cpp.extra_flags=-DQUELLA_WIFI_SERIAL_DEBUG=1' \
  firmware/quella_esp_gateway
```

## Normal Mega-to-ESP operation

For the direct Mega Serial3 to ESP UART connection:

`1 ON, 2 ON, 3 OFF, 4 OFF, 5 OFF, 6 OFF, 7 OFF, 8 OFF`.

Set the separate routing selector, when present, to `RXD3/TXD3`. Some revisions
also allow switches 3 and 4 ON to keep USB connected to the Mega, but this can
electrically join USB and the MCU UART paths. Use the isolated `1+2 ON` setting
for the first test and confirm the exact switch table printed on the PCB.

## First Serial3 link test

1. Flash the Mega firmware and the ESP firmware separately.
2. Set the normal `1+2 ON` position and `RXD3/TXD3`, then power-cycle.
3. For logging without corrupting UART0, optionally set
   `QUELLA_SERIAL1_LINK_DEBUG 1` and connect a **3.3 V** USB-UART RX input to
   ESP GPIO2/TX1 plus common ground.
4. A valid status JSON line approximately every 200 ms proves that ESP PING,
   Mega HELLO/PONG, GET_STATUS, CRC framing, and snapshot decoding work.
5. With debug disabled, the same proof is available from `GET /api/health`:
   `mega_link` must be `true` and `mega_last_seen_ms` normally below 1000.

Do not connect a 5 V USB-UART input to an ESP8266 pin.

## HTTP API

Read-only:

- `GET /api/status`
- `GET /api/config`
- `GET /api/health`

Authenticated writes:

```bash
curl -H 'X-API-Key: YOUR_KEY' -H 'Content-Type: application/json' \
  -d '{"ml":50,"temperature_c":25}' \
  http://quella-gateway.local/api/dispense

curl -X POST -H 'X-API-Key: YOUR_KEY' \
  http://quella-gateway.local/api/dispense/stop

curl -H 'X-API-Key: YOUR_KEY' -H 'Content-Type: application/json' \
  -d '{"enabled":true,"delay_ms":10000}' \
  http://quella-gateway.local/api/tank-delay

curl -H 'X-API-Key: YOUR_KEY' -H 'Content-Type: application/json' \
  -d '{"id":1,"ml":50,"temperature_c":25}' \
  http://quella-gateway.local/api/preset
```

The ESP only performs coarse JSON/range checks. The Mega accepts the original
remote range `1..1500 ml` and confirmed temperatures 25, 45, 55, 80, 90, or
95 °C. Every integer millilitre value in that range is accepted. The original
Display itself has only been captured with regular setpoints up to 900 ml.

WiFi modem sleep is disabled with `WiFi.setSleepMode(WIFI_NONE_SLEEP)` before
each connection attempt, including serial diagnostic mode. This favors stable
low-latency networking at the cost of somewhat higher ESP8266 power
consumption and heat.

POST handlers wait for the sequence-matched Mega `COMMAND_RESULT`. Link loss
returns 503; a missing result returns 504. A queued gateway dispense expires
on the Mega before the HTTP timeout, so it cannot unexpectedly start later.

## WebSocket

Connect to `ws://<gateway-ip>:81/ws`. Port 80 `/ws` returns an HTTP 426 response
containing the actual WebSocket URL. Messages are JSON envelopes:

```json
{"type":"status","data":{}}
{"type":"event","data":{"domain":2,"event":4,"mega_uptime_ms":1234}}
{"type":"command_result","data":{"command":3,"result":"OK"}}
{"type":"error","data":{"code":6}}
```

Snapshots arrive at a controlled 5 Hz and are broadcast only when their full
snapshot changes. In `status.water`, `tds_raw` is the latched B2 D3 value,
`tds_calculated` uses the captured original-display threshold table, and
`tds_mapping` is `CAPTURED_DISPLAY_THRESHOLDS_V1`. No external DC voltage is
invented. WebSocket carries status/control only. Complete raw UART frames use
binary UDP telemetry; see `Documentation/Telemetry_Protocol.md`. This prevents
binary appliance traffic from entering the UTF-8 WebSocket/JSON path.

## First API hardware test

1. Verify normal transparent operation with no API commands.
2. Check `/api/health`, then `/api/status` and live tank freshness.
3. Ensure external water is stable, no fill/flush is active, and the appliance
   is otherwise safe for the 25 °C / 1..1500 ml remote test path.
4. Request 50 ml at 25 °C with the API key. Expect HTTP 200/`OK`, then verify
   the physical output and A5 progress independently.
5. Repeat and immediately call `/api/dispense/stop`. Verify the Mega-originated
   abort frame, CC, A5 stop state, and pump stop.
6. Disconnect/reset the ESP. The Mega must continue transparent standalone
   operation; `/api/health` must show 503 while the link is absent.
