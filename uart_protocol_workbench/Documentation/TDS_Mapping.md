# TDS mapping

`E5 B2 Data[3]` is one binary byte. To avoid mixing its hexadecimal frame
notation with decimal UI values, both representations are listed explicitly.

| Raw byte (HEX) | Raw byte (DEC) | Display value (DEC) |
|---|---:|---:|
| `0x00–0x1F` | 0–31 | 0 |
| `0x20–0x2A` | 32–42 | 1 |
| `0x2B–0x36` | 43–54 | 2 |
| `0x37–0x41` | 55–65 | 3 |
| `0x42–0x4C` | 66–76 | 4 |
| `0x4D–0x58` | 77–88 | 5 |
| `0x59–0x6E` | 89–110 | 6 |
| `0x6F–0x79` | 111–121 | 7 |
| `0x7A–0x84` | 122–132 | 8 |
| `0x85–0x8F` | 133–143 | 9 |
| `0x90–0x9A` | 144–154 | 10 |
| `0x9B–0xA4` | 155–164 | 11 |
| `0xA5–0xAF` | 165–175 | 12 |
| `0xB0–0xC4` | 176–196 | 13 |
| `0xC5–0xCF` | 197–207 | 14 |
| `0xD0–0xDA` | 208–218 | 15 |
| `0xDB–0xE4` | 219–228 | 16 |
| `0xE5–0xEF` | 229–239 | 17 |
| `0xF0–0xF9` | 240–249 | 18 |
| `0xFA–0xFF` | 250–255 | 19 |

Example: raw decimal `48` is byte `0x30` and maps to display value `2` in
this table. A displayed `3` requires a committed raw byte from `0x37` through
`0x41` (decimal 55 through 65). If the physical display shows `3` while the
latched receiver reports `0x30 (48 dec)`, the captured raw byte and latch phase
must be checked; changing the threshold would contradict the measured table.
