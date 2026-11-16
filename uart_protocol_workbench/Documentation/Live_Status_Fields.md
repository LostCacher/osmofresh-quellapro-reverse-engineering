# Live Status field audit

The Live Status page only decodes fields present in the current protocol
knowledge, established decoders, or confirmed hardware captures. `UNKNOWN`
entries remain visible but are never synthesized.

| Name | Frame | Direction | Raw offset | Meaning / decode | Unit | Confidence | Live Status |
|---|---|---|---|---|---|---|---|
| Dispensing active | A5 | Main→Display | D1.bit0 | 1 active | bool | LOW | yes |
| Outlet temperature | A5 | Main→Display | D2 | unsigned; valid only while dispensing with requested temperature >25 °C | °C | CONFIRMED | yes; otherwise `—` |
| Current dispensed volume | A5 | Main→Display | D3..D4 | big-endian | ml | CONFIRMED | yes |
| Total dispensed amount | A5 | Main→Display | D5..D8 | big-endian | ml | MEDIUM..STRONG | yes |
| Error flags/codes | A5 | Main→Display | D9..D10 | known active bits mapped to E0/E2..EA | bitfield | STRONG | yes |
| A5 D11.bit4 cyclic | A5 | Main→Display | D11.bit4 | Cyclic observation; meaning unknown, never machine readiness | bool | UNKNOWN | debug only |
| Base/operating flag | A5 | Main→Display | D11.bit7 | 1 base/operating flag | bool | LOW | raw decoded section |
| 900 W heater control | A5 | Main→Display | D11.bit6 | 1 active | bool | MEDIUM | yes |
| 1200 W heater control | A5 | Main→Display | D11.bit5 | 1 active | bool | MEDIUM | yes |
| Flush process | A5 | Main→Display | D11.bit1 | 1 active | bool | STRONG | yes |
| Internal MIN mirror | A5 | Main→Display | D11.bit0 | 1 below low | bool | CONFIRMED | raw/debug; B2 is primary |
| Internal MAX mirror | A5 | Main→Display | D11.bit2 | 0 above high | bool | CONFIRMED | raw/debug; B2 is primary |
| Inlet temperature | A5 | Main→Display | D12 | unsigned | °C | CONFIRMED | yes |
| Heating level | A5 | Main→Display | D13.bits0..3 | raw heating/control nibble | raw | HIGH | yes |
| AC mains voltage | A5 | Main→Display | D14 | unsigned | V | CONFIRMED | yes |
| Internal tank MIN | E5 B2 | Main→Display | D4.bit3 | 1 below low | bool | CONFIRMED | yes |
| Internal tank MAX | E5 B2 | Main→Display | D4.bit5 | 0 above high | bool | CONFIRMED | yes |
| TDS raw | E5 B2 | Main→Display | D3 + D11.bit4 + D13.bit0 | D3 is retained during the measurement phase and committed when the D11.bit4 update strobe ends | raw | CONFIRMED | yes; latched |
| TDS calculated/display | — | — | threshold mapping pending | remains `UNKNOWN` until enough raw/display pairs establish all original-display thresholds | — | UNKNOWN | shown separately |
| External tank LOW | E5 B2 | Main→Display | D4.bit0 | 1 empty, 0 available | bool | CONFIRMED | yes |
| Booster/fill active | E5 B2 | Main→Display | D11.bit2 | 1 active | bool | CONFIRMED | yes |
| Wastewater flush valve | E5 B2 | Main→Display | D11.bit1 | 1 energized/open | bool | CONFIRMED | yes |
| Data-changed flag | E5 B2 | Main→Display | D14.bit0 | 1 changed | bool | LOW | raw decoded section |
| B2 D14 candidate raw | E5 B2 | Main→Display | D14 | unscaled experimental value; strongly correlated with fill/process load, possible booster-current feedback | raw | UNKNOWN | VictoriaMetrics/debug only |
| Feed-pump control parameter | E5 E1 | Main→Display | D1..D2 | big-endian | ml/min | MEDIUM | yes; not labelled as measured flow |
| Dispense command | AA00 | Display→Main | D1 | 00 abort, 01 start, 80 idle/refresh | enum | CONFIRMED | yes |
| Requested temperature | AA00 | Display→Main | D2 | unsigned | °C | CONFIRMED | yes |
| Requested volume | AA00 | Display→Main | D3..D4 | big-endian | ml | CONFIRMED | yes |
| Requested process | AA A3 | Display→Main | D4 | 68 base, 78 filter, 7C flush | enum | UNKNOWN | shown explicitly as requested/raw |
| Requested feed-pump parameter | AA E1 | Display→Main | D2..D3 | big-endian | ml/min | CONFIRMED | yes |
| Flow rate | derived A5 | Main→Display | ΔD3..D4 / Δtime | rolling three-second volume slope | ml/min | DERIVED | yes |
| Fill/flush/runtime/lifetime counters | — | — | — | no audited mapping | raw | UNKNOWN | visible as UNKNOWN |

Freshness is evaluated per field from its source timestamp. Recurring response
fields are fresh below 2 seconds, stale from 2 through 5 seconds, and expired
after 5 seconds. The raw section retains the last frame for inspection even
after its semantic values become stale.

Fields without an audited mapping are collected in the separate
`Unknown / not yet mapped` group. A speculative external DC voltage field is
not displayed because the current protocol model has no such value.

TDS is intentionally latched. Ordinary idle B2 frames frequently carry
`Data[3]=0` and must not erase the last completed measurement. Captures show
the value-transfer strobe in D11.bit4. Across 334,062 long-term samples every
observed bit4 sample was adjacent to a D3 change; D11.bit3 instead pulsed at a
regular 19–21 second cadence without a corresponding TDS change. D13.bit0 marks the associated
measurement phase.
