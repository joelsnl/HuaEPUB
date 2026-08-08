# Author: joelsnl and Anthropic Claude
"""Qt application entry."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QStyleFactory

from core.utils import sanitize_runtime_env
from gui.main_window import MainWindow


def run():
    sanitize_runtime_env()
    app = QApplication(sys.argv)
    app.setApplicationName("HuaEPUB")
    app.setOrganizationName("HuaEPUB")
    # Fusion + QSS keeps menu text aligned; Windows native menus misalign under stylesheet
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, True)

    qss = Path(__file__).with_name("style.qss")
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))

    win = MainWindow()
    win.show()
    return app.exec()
