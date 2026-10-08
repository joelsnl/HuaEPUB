# Author: joelsnl and Anthropic Claude
"""Application entry. Qt stays behind the desktop path so --headless needs no display."""

from __future__ import annotations

import sys

from core.utils import sanitize_runtime_env


def run():
    sanitize_runtime_env()
    from web.host import (
        _write_terminal,
        headless_requested,
        install_service_requested,
        install_user_service,
        serve_headless,
    )

    if install_service_requested():
        code, message = install_user_service()
        _write_terminal(message)
        return code

    if headless_requested():
        return serve_headless()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QStyleFactory

    from core.settings import get_setting
    from gui.icon import apply_app_icon, apply_windows_app_id
    from gui.main_window import MainWindow

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

    from gui import theme

    theme.apply_look(app, get_setting("ui_look") or "auto")

    if _yield_to_headless_server():
        return 0

    win = MainWindow()
    apply_app_icon(app, win)
    win.show()
    code = app.exec()
    from web.host import finish_gui_handoff

    held = finish_gui_handoff()
    return code if held is None else held


def _yield_to_headless_server() -> bool:
    """True when a no-window server should keep this launch from opening a second window."""
    from core.settings import get_data_dir
    from gui.dialogs import ask_yes_no, show_error
    from web.host import request_headless_stop, running_headless_pid, wait_headless_exit

    data_dir = get_data_dir()
    pid = running_headless_pid(data_dir)
    if not pid:
        return False
    if not ask_yes_no(
        None,
        "HuaEPUB is serving",
        "HuaEPUB is already serving with no window.\n\n"
        "Stop it and open the desktop app?",
    ):
        return True
    request_headless_stop(data_dir)
    if wait_headless_exit(pid):
        return False
    show_error(
        None,
        "HuaEPUB is serving",
        "That server did not stop. End the HuaEPUB process, then open the app again.",
    )
    return True
