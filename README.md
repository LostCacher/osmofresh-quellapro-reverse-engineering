# OsmoFresh Quella Pro Reverse Engineering

> Reverse engineering and hardware documentation of the OsmoFresh Quella Pro water dispenser.

## Project Goals

This project aims to document the hardware architecture of the OsmoFresh Quella Pro to improve repairability and technical understanding.

Current objectives include:

- Reverse engineering the hardware
- Creating complete schematics
- Identifying electronic components
- Documenting the UART communication protocol
- Documenting sensors and actuators
- Exploring the possibility of custom firmware (if technically feasible)

## Repository Structure

```
Documentation/
KiCAD/OsmoFresh_Quella_PRO/
Pictures/
uart_protocol_workbench/
docker/projects/osmofresh-quella-pro/
```

- [Complete UART protocol](Documentation/UART_Protocol_Complete.md)
- [Component inventory](Documentation/Componens.md)
- [Connector pinout](Documentation/Connector_Pinout.md)
- [UART Protocol Workbench](uart_protocol_workbench/README.md)
- Web HMI and VictoriaMetrics exporter:
  `docker/projects/osmofresh-quella-pro/`

## Project Status

⚠️ This project is currently work in progress.
The published schematics are based on reverse engineering and may contain errors or incomplete sections.

- ✅ Device teardown
- ✅ Component identification
- ✅ Primary power stage schematic
- ✅ Main PCB reverse engineering
- 🔄 UART protocol analysis
- ⏳ Firmware investigation

## Contributions, corrections and additional information are welcome.

## Disclaimer

This repository contains independently created documentation and source code
based on observation and testing of hardware owned by the project author. It is
an unofficial community project and is not affiliated with, endorsed by, or
sponsored by OsmoFresh or the manufacturers of referenced components. Product
and company names may be trademarks of their respective owners and are used
only to identify the analyzed device and interoperability target.

The public source tree intentionally excludes vendor installers, proprietary
firmware, manufacturer manuals/datasheets, private captures, credentials,
deployment secrets and homeserver logs. Links or part numbers may be provided
so readers can obtain third-party documentation from its publisher.

Schematics, measurements, photographs and protocol documentation included in
the public tree were created independently. Protocol meanings are experimental
and may be incomplete or wrong.

## Electrical and operational safety

This device contains mains voltage, high-power heaters, pumps and water.
Incorrect wiring, isolation, emulation or injected commands can cause electric
shock, fire, flooding, overheating or equipment damage. Do not work on an
energized board unless you are qualified and have suitable isolation and test
equipment. All software and documentation are provided without warranty; do
not rely on the project as a certified safety controller.

## License

This repository is licensed under the MIT License.

The MIT license covers material authored for this repository. External tools
and dependencies retain their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
