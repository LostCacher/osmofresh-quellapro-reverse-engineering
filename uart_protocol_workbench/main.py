#!/usr/bin/env python3
"""Launch the existing PySide workbench; UDP telemetry is an optional mode."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


DARK_STYLE = """
QWidget { background: #202124; color: #e6e6e6; }
QGroupBox { border: 1px solid #555; margin-top: 8px; padding-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QTableWidget {
    background: #292b2f; color: #eeeeee; border: 1px solid #555; selection-background-color: #315d84;
}
QPushButton { background: #383b40; border: 1px solid #666; padding: 5px 9px; border-radius: 3px; }
QPushButton:hover { background: #484c52; }
QPushButton:disabled { color: #777; background: #292a2d; }
QTabBar::tab { background: #303236; padding: 7px 12px; border: 1px solid #4b4d52; }
QTabBar::tab:selected { background: #3e5264; }
QHeaderView::section { background: #34373b; color: #eee; border: 1px solid #505257; padding: 4px; }
QToolTip { background: #efefef; color: #111; }
"""


def gui_main() -> int:
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("UART Protocol Workbench")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description="UART Protocol Workbench")
    parser.add_argument("--gui", action="store_true", help="launch the PySide6 workbench (default)")
    parser.add_argument("--telemetry", action="store_true", help="run the QT UDP terminal receiver instead of the GUI")
    parser.add_argument("--bind", default="0.0.0.0", help="UDP bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=42042, help="QT UDP port (default: 42042)")
    parser.add_argument("--raw", "--raw-log", action="store_true", help="print every decoded frame as one line")
    parser.add_argument("--record", type=Path, help="append decoded packets and frames as JSONL")
    args = parser.parse_args()
    if not args.telemetry:
        return gui_main()
    from telemetry_receiver import TelemetryReceiver
    TelemetryReceiver(args.bind, args.port, raw=args.raw, record=args.record).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
