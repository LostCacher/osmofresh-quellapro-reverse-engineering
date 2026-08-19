# MainPCB Bill of Materials (BOM)

This document lists all components identified on the OsmoFresh Quella Pro MainPCB.

> **Status**
> - ✅ = Identified
> - 🟡 = Partially identified
> - ❓ = Unknown / To be analyzed

---

# Protection Devices

| RefDes | Component | Value | Model | Status |
|--------|-----------|-------|-------|:------:|
| FUSE1 | Fuse | T16A | TT16AL250V | ✅ |
| ZNR1 | Varistor | 560 V | VDR 14D561K | ✅ |

---

# Integrated Circuits (IC)

| RefDes | Component | Value / Function | Part Number / Marking | Package | Status | Notes |
|--------|-----------|------------------|------------------------|---------|:------:|-------|
| IC1 | MCU | Main Controller | HIGHWAY-C225F | QFP-28 | ✅ | Custom MCU |
| U1 | Buck Converter | 24 V → 5 V DC/DC | Unknown | SOT-23-5 | 🟡 | Step-down converter |
| U3 | Opto-Triac Driver | | | SOIC-6 | 🟡 | |
| U4 | Darlington Driver | 7-Channel Driver | ULN2003A | SOIC-16 | ✅ | |
| U5 | Dual Comparator | Voltage Comparator | KM393 | SOIC-8 | ✅ |

---

# Relays

| RefDes | Function | Model | Coil Voltage | Contact Rating | Status |
|--------|----------|-------|--------------|----------------|:------:|
| K1 | Heater Relay | Vuanze Y32F-SS-124HM | 24 VDC | 10 A / 250 VAC | ✅ |
| K2 | Water Pump Relay | Vuanze Y32F-SS-124DM | 24 VDC | 5 A / 250 VAC | ✅ |

---

# Mechanical Components

| RefDes | Description | Status | Notes |
|--------|-------------|:------:|-------|
| SW1 | Thermal Safety Switch | ✅ | Heater protection |

---

# Transformers / Magnetics

| RefDes | Component | Model | Status | Notes |
|--------|-----------|-------|:------:|-------|
| T1 | Signal Transformer | JB-TV/A2/2-E | ✅ | AC sensing / Zero-cross detection |
| L1 | Inductor | Unknown | 🟡 | Parallel to buck inductor L1 |

---

# Transistors / Power Switches

| RefDes | Type | Part Number | Package | Status | Notes |
|--------|------|-------------|---------|:------:|-------|
| Q1 | TRIAC | BTA16-800CW | TO-220 | ✅ | 900 W heater triac |
| Q2 | NMOS | Unknown | SOT-23 | 🟡 | Heater relay low-side driver |
| Q3 | NPN BJT | Unknown | SOT-23 | 🟡 | Input inverter / signal conditioning |
| Q4 | PMOS | Unknown | SOT-23 | 🟡 | Water outlet valve high-side switch |
| Q5 | NPN BJT | Unknown | SOT-23 | 🟡 | Water pump relay low-side driver |
| Q6 | NPN BJT | Unknown | SOT-23 | 🟡 | Input inverter / signal conditioning |
| Q8 | NPN BJT | Unknown | SOT-23 | 🟡 | PMOS gate driver |

---

# Diodes

| RefDes | Type | Model | Status | Notes |
|--------|------|-------|:------:|-------|
| D4 | Flyback Diode | Unknown | 🟡 | K1 relay coil suppression |
| D5 | Flyback Diode | Unknown | 🟡 | Water pump inductive load suppression |
| D6 | Flyback Diode | Unknown | 🟡 | Water outlet valve coil suppression |
| D7 | Flyback Diode | Unknown | 🟡 | K2 relay coil suppression |

---

# Resistors

| RefDes | Value | Type | Package | Status | Notes |
|--------|-------|------|---------|:------:|-------|
| RL1 | | Resistor | SMD 1206 | 🟡 | Parallel to L1 (buck converter) |

| R4 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R5 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R6 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R7 | 180 Ω | Resistor | SMD 2010 | ✅ | Triac gate resistor |

| R10 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R11 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R12 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R13 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R14 | 1 kΩ | Resistor | SMD 0805 | ✅ | |
| R15 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R16 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R17 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R18 | 4.7 kΩ | Resistor | SMD 0805 | ✅ | |
| R19 | 4.7 kΩ | Resistor | SMD 0805 | ✅ | |
| R20 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R21 | 1 kΩ | Resistor | SMD 0805 | ✅ | |
| R22 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R23 | 47 kΩ | Resistor | SMD 0805 | ✅ | |
| R24 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R25 | 1 kΩ | Resistor | SMD 0805 | ✅ | |
| R26 | 82 kΩ | Resistor | SMD 1206 | ✅ | AC sense resistor chain |
| R27 | 82 kΩ | Resistor | SMD 1206 | ✅ | AC sense resistor chain |
| R28 | 82 kΩ | Resistor | SMD 1206 | ✅ | AC sense resistor chain |
| R29 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R30 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R31 | 4.7 kΩ | Resistor | SMD 0805 | ✅ | |
| R32 | 510 Ω | Resistor | SMD 0805 | ✅ | |
| R33 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R34 | 510 Ω | Resistor | SMD 0805 | ✅ | |
| R35 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R36 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R37 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R38 | 10 kΩ | Resistor | SMD 0805 | ✅ | |

| R40 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R41 | 10 kΩ | Resistor | SMD 0805 | ✅ | |

| R44 | 330 Ω | Resistor | SMD 0805 | ✅ | |
| R45 | 2 kΩ | Resistor | SMD 0805 | ✅ | |

| R49 | 220 Ω | Resistor | SMD 0805 | ✅ | |
| R50 | 220 Ω | Resistor | SMD 0805 | ✅ | |
| R51 | 750 Ω | Resistor | SMD 1206 | ✅ | |
| R52 | 750 Ω | Resistor | SMD 1206 | ✅ | |
| R53 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R54 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R55 | 5.1 Ω | Power resistor | SMD 2010 | ✅ | Water outlet valve series resistor |

| R59 | 470 Ω | Resistor | SMD 0805 | ✅ | |
| R60 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R61 | 47 kΩ | Resistor | SMD 0805 | ✅ | |
| R62 | 0.62 Ω | Power resistor | Axial THT | ✅ | Pump current shunt (parallel with R63) |
| R63 | 0.62 Ω | Power resistor | Axial THT | ✅ | Pump current shunt (parallel with R62) |
| R64 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R65 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R66 | 2 kΩ | Resistor | SMD 0805 | ✅ | |
| R67 | 10 kΩ | Resistor | SMD 0805 | ✅ | |
| R68 | 10 kΩ | Resistor | SMD 0805 | ✅ | |

---

# Capacitors

| RefDes | Value | Type | Package | Voltage | Status | Notes |
|--------|-------|------|---------|:------:|-------|
| XC1 | 100 nF | X2 Safety Capacitor | Radial Box THT (10 mm LP) | 275 VAC | ✅ | |
| EC1 | 470 µF | Electrolytic | Radial Electrolytic Ø12.5 × 20 mm | 50 V | ✅ | |
| EC2 | 330 µF | Electrolytic | Radial Electrolytic Ø6.3 × 11 mm oder Ø6.3 × 11.5 mm | 10 V | ✅ | |
| C1 | | Ceramic | 0805 | | ❓ | |
| C2 | | Ceramic | 0805 | | ❓ | |
| C3 | | Ceramic | 0805 | | ❓ | |
| C4 | | Ceramic | 0805 | | ❓ | |
| C5 | | Ceramic | 0805 | | ❓ | |
| C6 | | Ceramic | 0805 | | ❓ | |
| C7 | | Ceramic | 0805 | | ❓ | |
| C8 | | Ceramic | 0805 | | ❓ | |
| C9 | | Ceramic | 0805 | | ❓ | |
| C10 | | Ceramic | 0805 | | ❓ | |
| C11 | | Ceramic | 0805 | | ❓ | |
| C12 | | Ceramic | 0805 | | ❓ | |
| C13 | | Ceramic | 0805 | | ❓ | |
| C14 | | Ceramic | 0805 | | ❓ | |
| C15 | | Ceramic | 0805 | | ❓ | |
| C16 | | Ceramic | 0805 | | ❓ | |
| C17 | | Ceramic | 0805 | | ❓ | |
| C18 | | Ceramic | 0805 | | ❓ | |
| C19 | | Ceramic | 0805 | | ❓ | |
| C20 | | Ceramic | 0805 | | ❓ | |
| C21 | | Ceramic | 0805 | | ❓ | |
| C22 | | Ceramic | 0805 | | ❓ | |
| C23 | | Ceramic | 0805 | | ❓ | |
| C24 | | Ceramic | 0805 | | ❓ | |
| C25 | | Ceramic | 0805 | | ❓ | |
| C26 | | Ceramic | 0805 | | ❓ | |
| C27 | | Ceramic | 0805 | | ❓ | |

---

# Notes

- Unknown components will be updated as reverse engineering progresses.
- Values are taken from PCB markings whenever possible.
- Package sizes are identified by physical measurements where no manufacturer data is available.
- Datasheets are available in the `Datasheets/` directory where applicable.
