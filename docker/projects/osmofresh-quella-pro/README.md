# Osmofresh Quella Pro telemetry

This project receives the ESP8266 gateway's binary `QT` UDP stream, validates
its CRC and UART checksums, evaluates confirmed fields, and exports both the
decoded state and every payload byte/bit to VictoriaMetrics through vmagent.

## Services

- `osmofresh-quella-pro`: listens on host UDP port `42042` and atomically
  updates `data/live_state.json` at up to 5 Hz.
- `osmofresh-quella-pro-metrics`: exposes the state on port `8000` inside
  `vm-net`. vmagent scrapes it once per second. It also refreshes the standard
  `tmp/health.json` and `service_last_success_timestamp` used by the common
  Docker healthcheck and the central watchdog.
- `osmofresh-quella-pro-web`: production FastAPI HMI and installable PWA.
  Traefik exposes only this service at
  `https://osmofresh-quella.server.home/`; no additional host HTTP port is
  published. The browser never receives the ESP API key.

The receiver remains useful on its own: `live_state.json` contains receiver
statistics, decoded fields, the latest raw frames, payload byte arrays,
checksum status, and timestamps. Unknown fields are deliberately not assigned
meanings; their raw bytes and all eight bits remain available as metrics.

## Deploy

The ESP firmware's `QUELLA_TELEMETRY_HOST` must point to the Docker host. Ensure
UDP port 42042 is allowed from the ESP VLAN, then run:

```bash
cd /srv/docker/projects/osmofresh-quella-pro
docker compose up -d
docker compose logs -f osmofresh-quella-pro
```

The web image uses the host network only while installing its Python packages
during the image build. This works around the homeserver's BuildKit DNS not
being able to reach the local resolver. Runtime networking remains restricted
to `vm-net` and `traefik` as declared in Compose.

### Docker-to-ESP firewall route

The public/browser connection terminates TLS at Traefik. The private
FastAPI-to-ESP hop deliberately uses the ESP8266 HTTP server on
`10.0.1.27:80`; the ESP firmware does not run an HTTPS listener. If a host
`curl http://10.0.1.27/api/health` works but the same command in the web
container times out, permit only this routed destination in UFW:

```bash
docker network inspect vm-net --format '{{(index .IPAM.Config 0).Subnet}}'
docker network inspect traefik --format '{{(index .IPAM.Config 0).Subnet}}'

# Replace the two example subnets with the values printed above.
ufw route allow proto tcp from 172.20.0.0/16 to 10.0.1.27 port 80
ufw route allow proto tcp from 172.21.0.0/16 to 10.0.1.27 port 80
ufw reload
```

Then verify both hops independently:

```bash
curl -sS --max-time 3 http://10.0.1.27/api/health
docker compose exec osmofresh-quella-pro-web \
  curl -sS --max-time 3 http://10.0.1.27/api/health
```

Do not open port 80 generally and do not expose the ESP through Traefik. The
browser continues to use only `https://osmofresh-quella.server.home/`, and the
ESP mutation key remains server-side.

Copy `.env.example` to `.env` and set the same `QUELLA_ESP_API_KEY` as in the
ESP's private `secrets.h`. The file must remain server-side and must not be
committed. The web container uses the existing `vm-net` connection for
VictoriaMetrics history and the `traefik` network for HTTPS.

The optional `deploy/quella-web.service` wraps Docker Compose for hosts that
manage project startup through systemd. Docker's `restart: unless-stopped`
remains the container-level restart policy.

Restart vmagent after copying the updated scrape configuration:

```bash
cd /srv/docker/core/victoriametrics
docker compose restart vmagent
```

Useful checks:

```bash
docker compose exec osmofresh-quella-pro cat data/live_state.json
docker compose exec osmofresh-quella-pro-metrics \
  curl -s http://127.0.0.1:8000/metrics | grep osmofresh_quella
curl -sk https://osmofresh-quella.server.home/api/state | jq .machine
```

To accept packets only from the ESP, set `QUELLA_EXPECTED_SOURCE` in
`docker-compose.yml` to `10.0.1.27`.

Important metrics include:

- `osmofresh_quella_up`
- `osmofresh_quella_field_value{field="..."}`
- `osmofresh_quella_payload_byte_value{direction,frame,offset}`
- `osmofresh_quella_payload_bit_value{direction,frame,offset,bit}`
- `osmofresh_quella_lost_packets_total`
- `osmofresh_quella_mega_dropped_raw_frames`
- `osmofresh_quella_wifi_rssi_dbm`
- `osmofresh_quella_booster_pump_power_watt` (E5 B2 D14, approximately W,
  feed-pump and heater power excluded)
- `osmofresh_quella_b2_d14_candidate_raw` (deprecated compatibility alias)
- `osmofresh_quella_external_empty_stop_latched`
- `osmofresh_quella_external_empty_reset_armed`

The Debug page shows the ESP WiFi RSSI from QT v2. Receivers remain compatible
with QT v1, in which case RSSI is unavailable. The Control page stores eight
volume presets persistently in the Mega configuration. `USE` only copies a
preset into the requested-volume input; dispensing still requires a separate
press on `START DISPENSE`.

An external-empty observation is latched immediately. An already running
dispense/flush/heat process may finish, but fill and new output remain blocked.
After refilling the external tank, press dispense once to arm acknowledgement
(the frame is suppressed), then press again to return the Display to IDLE. The
second press clears the latch; no time-based release is used.
