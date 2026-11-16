# Quella Pro Web HMI audit and deployment

## Audited baseline

1. The homeserver uses Docker Compose, `vm-net`, Traefik and the shared project
   health/watchdog conventions. The new HMI follows that structure.
2. There was no FastAPI application. The old port-8001 endpoint was a small
   read-only HTTP handler in the metrics exporter; it has been replaced by a
   dedicated FastAPI service.
3. `main.py` receives CRC-protected QT UDP batches on port 42042 and writes an
   atomic state snapshot up to 5 Hz. It remains unchanged as the raw data path.
4. VictoriaMetrics already scrapes the exporter every second. Charts query
   this source through a fixed server-side allowlist; no second database was
   added.
5. Confirmed QG commands were dispense, stop, preset and tank delay. The HMI
   never exposes a raw injection operation.
6. The ESP remains an HTTP/QG gateway. Its API key is read only from server
   environment and is never included in HTML, JavaScript or browser responses.
7. The Mega had one volatile exact-frame volume rule. It now additionally has
   eight semantic `VOLUME_PRESET` mappings that select the existing stateful
   display-override path. Temperature/flow overrides remain unavailable.
8. No persistence existed. The Mega now has an explicit-save EEPROM record
   with magic, schema version, CRC16 and validation/default fallback.
9. A5 D11.bit4 was labelled `ready`, but captures show cyclic idle toggling.
   It is now `a5_d11_bit4_cyclic`, debug-only, and excluded from mode logic.
10. E5 E1 is an observed feed-pump parameter and A5 delta volume/time is
    derived flow telemetry. No confirmed semantic QG flow-control command
    exists, so the product UI keeps flow read-only.

## State derivation

The mutually exclusive primary mode priority is `ERROR`, `DISPENSING`,
`FILLING`, `FLUSHING`, `HEATING`, then `IDLE`. With no usable source it is
`UNKNOWN`. Command rejection (`TANK_EMPTY`, `BUSY`, etc.) is returned as a
command result, not falsely promoted to a machine error.

## Deployment

```bash
cd /srv/docker/projects/osmofresh-quella-pro
cp .env.example .env
# edit .env; QUELLA_ESP_API_KEY must match private ESP secrets.h
docker compose up -d --build
docker compose logs -f osmofresh-quella-pro-web
```

Traefik routes `https://osmofresh-quella.server.home/` to the web container on
port 8001. If systemd owns project startup, install `deploy/quella-web.service`
under `/etc/systemd/system/`, then run `systemctl daemon-reload` and
`systemctl enable --now quella-web.service`.

## First hardware test

1. Flash the compiled Mega firmware, then the compiled ESP firmware.
2. Start the three Docker services and open the HTTPS hostname.
3. Confirm Gateway, Mega and Telemetry are ONLINE and an unused machine reads
   IDLE even while `a5_d11_bit4_cyclic` changes in Debug.
4. Install the PWA from Android and verify OFFLINE appears when the server is
   unreachable; cached pages must not show cached measurements as live.
5. Start a display dispense and verify mode, current/target/remaining ml,
   derived flow, temperature and progress.
6. Start a safe remote dispense, then STOP. Verify Mega result and physical
   stop; test rejection with the external tank unavailable.
7. Observe tank-delay pending/remaining and a fill cycle.
8. Apply a confirmed mapping such as 150 → 350 ml. Press the original 150 ml
   display key and verify Mega replaces start and refresh locally.
9. Press the display key again during that session. Verify original D1=00 is
   passed immediately and the user-abort/session cleanup still occurs.
10. Reboot Mega and verify the saved rule returns; disable/delete and verify
    transparent original behavior after another reboot.
11. Disconnect ESP/homeserver and verify the Mega remains transparent/safe and
    that a deliberately persistent enabled mapping remains local as configured.

## Deliberately still unknown

No semantics were invented for A5 D11.bit4, unmapped payload bytes, secondary
temperature/counters, or other raw bitfields. Internal tank percentage is not
shown because only confirmed MIN/MAX states exist. Flow control, temperature
override and flow override stay disabled until hardware captures confirm a
safe semantic command path.
