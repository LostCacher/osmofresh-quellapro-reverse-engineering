from gui.main_window import preferred_serial_port


def test_preferred_serial_port_selects_first_ttyusb_device():
    ports = ["/dev/ttyAMA0", "/dev/ttyS0", "/dev/ttyUSB1", "/dev/ttyUSB0"]

    assert preferred_serial_port(ports) == "/dev/ttyUSB1"


def test_preferred_serial_port_is_case_insensitive_and_supports_bare_names():
    assert preferred_serial_port(["ttyS0", "ttyusb0"]) == "ttyusb0"


def test_preferred_serial_port_returns_none_without_usb_serial_device():
    assert preferred_serial_port(["/dev/ttyAMA0", "/dev/ttyS0"]) is None
