# Water Level Control

This document describes the water level monitoring and pump control logic of the internal and external water tanks.

---

# Sensors

## Internal Tank Level MIN

- Connector: CN14
- Reed switch: Normally Closed (NC)
- Trigger height: ~90 mm
- Function:
  - Indicates that the minimum required water level has been reached.
  - Enables the feed pump.
  - Does **not** start the feed pump automatically.

---

## Internal Tank Level MAX

- Connector: CN6
- Reed switch: Normally Closed (NC)
- Trigger height: ~180 mm
- Function:
  - Stops / inhibits the booster pump.

---

## External Water Tank Empty

- Connector: CN2
- Reed switch: Normally Open (NO)
- Function:
  - Detects an empty external water tank.
  - Stops / inhibits the booster pump.

---

# Booster Pump Logic

The booster pump is inhibited when:

- The external water tank is empty.
- The internal tank reaches the MAX level.

The booster pump is allowed to operate again when the corresponding inhibit condition is no longer present.

---

# Feed Pump Logic

The feed pump may only operate when:

- The Internal Tank Level MIN sensor has been reached (feed pump enabled).

The MIN sensor only provides the permission to operate the feed pump.
The feed pump starts only when requested by the control logic (e.g. during a dispensing cycle).

---

# Dispensing Logic

The controller keeps track of the **cumulative dispensing time**.

After approximately **25 seconds of total water dispensing**, the booster pump is enabled again to refill the internal tank.

The dispensing time is accumulated across multiple dispensing cycles and is not reset when dispensing is interrupted.
