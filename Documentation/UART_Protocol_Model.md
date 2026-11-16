# UART protocol model

## Corrected frame layout

The earlier interpretation of the penultimate byte as a Meta/Dataset ID was
wrong. Main-to-display responses contain exactly:

```text
Data[0..15] + SUM16_H + SUM16_L
```

Display-to-main AA frames contain:

```text
AA + Data[0..15] + SUM16_H + SUM16_L
```

`AA` is a header and is excluded from the calculation. For both directions:

```text
sum16   = sum(Data[0..15])
SUM16_H = (sum16 >> 8) & 0xFF
SUM16_L = sum16 & 0xFF
```

Examples:

- E5 B2 default payload: SUM16 `00 DE`
- E5 B2 second observed payload: SUM16 `01 38`
- E5 E1 observed payload: SUM16 `02 D8`
- A5 observed payload: SUM16 `03 8A`

The high byte is not a page number, dataset identifier or response selector.
It must not split histories, choose frames, or drive round-robin transmission.

## Payload states

Different captured payloads for the same request are retained losslessly as
`observed payload state A/B/...`. Their discriminator is `UNKNOWN` until the
wire protocol provides independent evidence. Static emulation transmits only
the explicitly selected state. Startup replay continues to transmit the exact
bytes of each configured replay step.

For E5 replies, Data[15] matching the request identifier (`B2` or `E1`) is a
strong observation, but it is not generalized to A5. A5 Data[15] is currently
unknown and often `00`.

AA currently has two payload-class candidates based on Data[15]:

- `00`: control/setpoint candidate
- `A3`: system/process candidate

These names remain hypotheses. They are independent of SUM16.

## Knowledge reset

All earlier byte meanings, bit meanings, categories and confidence values were
discarded after correcting the checksum model. `protocol_knowledge.json` uses
schema version 9. Byte and bit meanings exist once per frame type and are shared
by all of its observed payload states; state selection remains solely in
`default_frames.json`. Raw frames remain preserved in default and startup
profile files.

## Editing and validation

Editing Data with Auto checksum enabled recalculates both SUM16 bytes. Merely
loading or transmitting a captured frame never silently normalizes it. Manual
checksum editing is available only after Auto checksum is disabled. AA is
acknowledged with `CC` only when both checksum bytes validate.
