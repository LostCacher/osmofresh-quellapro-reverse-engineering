# Third-party notices

This repository contains source code that uses, but does not vendor, the
following third-party projects. Installing or compiling the project downloads
those dependencies under their own licenses.

| Dependency | Use | License noted by upstream |
|---|---|---|
| Qt for Python / PySide6 | Desktop GUI | LGPL-3.0 / GPL-3.0 / commercial options |
| pySerial | Desktop serial transport | BSD-3-Clause |
| pytest | Tests | MIT |
| Rich | Telemetry terminal UI | MIT |
| FastAPI | Web HMI | MIT |
| Starlette | Web HMI | BSD-3-Clause |
| Uvicorn | Web HMI | BSD-3-Clause |
| Pydantic | Web HMI | MIT |
| HTTPX / HTTPCore | Web HMI | BSD-3-Clause |
| Arduino AVR core | ATmega2560 build dependency | upstream Arduino licenses |
| ESP8266 Arduino core | ESP8266 build dependency | LGPL-2.1 and bundled component licenses |
| Arduino WebSockets | ESP8266 WebSocket dependency | upstream library license |

No third-party installer, compiler, vendor programming application, MCU
firmware dump or manufacturer datasheet is intended to be part of the public
Git tree.

This notice is an inventory, not a replacement for the dependency license
texts. Anyone distributing a bundled executable or firmware image must review
the exact dependency versions and satisfy their source, notice and relinking
requirements.
