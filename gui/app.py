# Author: joelsnl and Anthropic Claude
"""Qt application entry."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QStyleFactory

from core.utils import sanitize_runtime_env
from gui.icon import apply_app_icon, apply_windows_app_id
from gui.main_window import MainWindow


def run():
    sanitize_runtime_env()
    apply_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("HuaEPUB")
    app.setApplicationDisplayName("HuaEPUB")
    app.setOrganizationName("HuaEPUB")
    apply_app_icon(app)
    # Fusion + QSS keeps menu text aligned; Windows native menus misalign under stylesheet
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, True)

    qss = Path(__file__).with_name("style.qss")
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))

    win = MainWindow()
    apply_app_icon(app, win)
    win.show()
    return app.exec()
