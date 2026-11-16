# TDS display-threshold mapping test

The available captures prove that E5 B2 D3 is the measurement value, but they
do not contain enough raw/display pairs to reconstruct the original display's
complete threshold table. One reliable pair is raw values around `0x32..0x36`
producing display value `2`; that single interval is not enough to infer every
boundary.

No second Mega firmware is required. The normal `quella_uart_bridge` firmware
retains the isolated-display emulator path:

1. Disconnect the real Main MCU completely.
2. Connect only the Display to Mega Serial1 and select **Main MCU Emulator**.
3. Select **ATmega2560 Bridge**, then configure `DISPLAY_ONLY_TEST` in the Mega
   Bridge tab. This prevents forwarding toward an absent Main MCU.
4. Open **Mega Bridge → Isolated Display TDS mapping**, choose `XX` in
   **Synthetic E5 B2 Data[3]**, and press **Queue raw update**. Python then
   responds to the display's normal requests and emits these two B2 phases
   automatically:

   ```text
   measurement: 00 00 00 XX 00 0C 00 00 00 00 00 1C 00 01 14 B2 | SUM16
   commit:      00 00 00 XX 00 0C 00 00 00 00 00 0C 00 01 14 B2 | SUM16
   ```

   The `D11.bit4` falling edge commits D3. Python recalculates
   SUM16 for both frames. Further B2 polls hold the commit phase; pressing
   **Queue raw update** again restarts the pulse for the new value.
5. Record `XX`, the number rendered by the physical display, and the first poll
   on which it changes. Repeat near each transition using a binary search.
6. Keep tank/status bytes constant during a sweep and power-cycle between
   ambiguous tests, because the display retains the previous completed TDS
   result.

Press **Stop TDS emulation** before reconnecting the real Main MCU. The tool is
refused in `DIRECT_PASS`; it is only active in `DISPLAY_ONLY_TEST`.


The binary UDP `QT` telemetry stream records the exact B2 values and pulse
timing at the same time. The measured table in `TDS_Mapping.md` is implemented
as `tds_calculated`; `tds_raw` remains available independently.
