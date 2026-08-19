# Connector Pinout

## Primary Side (230 VAC)

| Connector | Description |
|-----------|-------------|
| **P1** | ACL → Heater |
| **P2** | ACN → Thermal safety switch → Heater |
| **P3** | ACN → AC mains input |
| **P4** | ACN → 24 VDC power supply |
| **P5** | ACL → 24 VDC power supply |
| **P6** | ACL → AC mains input |

---

## Secondary Side (24 VDC)

| Connector | Description |
|-----------|-------------|
| **CN1** | 24 VDC input (from power supply) [+24Vdc] |
| **CN2** | Reed switch (2-pin) – Water tank empty detection |
| **CN3** | Water temperature sensor (heater outlet) [+5Vdc] |
| **CN5** | Water temperature sensor (heater inlet) [+5Vdc] |
| **CN6** | Reed switch (2-pin) – Internal tank level sensor (black HW or NW) |
| **CN7** | UNUSED |
| **CN8** | UNUSED [+5Vdc] |
| **CN9** | TDS sensor (2-pin) |
| **CN10** | 6-pin actuator connector |
| **CN11** | Display interface (5 V, TX, RX, GND) |
| **CN12** | Water outlet valve (before heater) [+24Vdc] |
| **CN13** | UNUSED [+24Vdc] |
| **CN14** | Reed switch (2-pin) – Internal tank level sensor (red HW or NW)|
| **CN16** | UNUSED [+5Vdc] |

### CN10 Pinout

| Pin | Function |
|----:|----------|
| 1 | GND |
| 2 | +24 V (Not connected) |
| 3 | GND |
| 4 | +24 V Flush Valve |
| 5 | GND |
| 6 | +24 V Water Pump |
