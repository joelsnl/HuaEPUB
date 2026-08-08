# Author: joelsnl and Anthropic Claude
"""Qt application entry."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from core.utils import sanitize_runtime_env
from gui.main_window import MainWindow


def run():
    sanitize_runtime_env()
    app = QApplication(sys.argv)
    app.setApplicationName("HuaEPUB")
    app.setOrganizationName("HuaEPUB")

    qss = Path(__file__).with_name("style.qss")
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))

    win = MainWindow()
    win.show()
    return app.exec()
