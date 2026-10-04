# Author: joelsnl and Anthropic Claude
"""Server mode on the main window: the Serve chip, File → Server mode…, and the server screen.

While serving, the browser is the app: the tabs are swapped for the server
screen so the desktop and the browser never run jobs side by side on the
same library, cache and resume file. The choice is remembered
(``server_enabled``) and serving starts again on the next launch.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Qt, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QToolButton

from core.settings import save_settings
from core.updater import get_current_version
from gui.dialogs import ask_yes_no, show_error, show_info
from gui.server_dialog import ChangePasswordDialog, ServerModeDialog
from gui.widgets.server_screen import ServerScreen


class ServerActionsMixin:
    _server_host = None

    # -- set-up ------------------------------------------------------------
    def _build_server_ui(self, stack) -> None:
        """Add the server screen to ``stack`` and the Serve chip to the tab bar."""
        self._server_stack = stack
        self.server_screen = ServerScreen()
        self.server_screen.open_browser.connect(self._server_open_browser)
        self.server_screen.new_code.connect(self._server_new_code)
        self.server_screen.change_password.connect(self._server_change_password)
        self.server_screen.stop_serving.connect(self._stop_serving_clicked)
        stack.addWidget(self.server_screen)

        chip = QToolButton()
        chip.setObjectName("serveChip")
        chip.setText("SERVE")
        chip.setToolTip("Server mode: use HuaEPUB from a browser on this PC, "
                        "your phone or another computer.")
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.clicked.connect(self._open_server_dialog)
        self.tabs.setCornerWidget(chip, Qt.Corner.TopRightCorner)
        self.serve_chip = chip

        self._server_timer = QTimer(self)
        self._server_timer.setInterval(1000)
        self._server_timer.timeout.connect(self._refresh_server_screen)

    def _server_host_obj(self):
        if self._server_host is None:
            from web.host import ServerHost

            self._server_host = ServerHost(self.session, version=get_current_version())
        return self._server_host

    def _serving(self) -> bool:
        host = self._server_host
        return bool(host is not None and host.running)

    def _desktop_busy_reason(self) -> str:
        if self._worker_busy or self.session.control.is_downloading:
            return "A download is running. Let it finish, or pause it, first."
        if self._is_check_running():
            return "Library is checking for updates. Wait for it to finish first."
        if self._drive_sync_running():
            return "Google Drive is syncing. Wait for it to finish first."
        return ""

    # -- start -------------------------------------------------------------
    @Slot()
    def _open_server_dialog(self) -> None:
        if self._serving():
            return
        busy = self._desktop_busy_reason()
        if busy:
            show_info(self, "Server mode", busy)
            return
        from web.host import find_public_address

        host = self._server_host_obj()
        dlg = ServerModeDialog(self, settings=self.session.settings,
                               has_password=host.secrets.has_password,
                               find_public_address=find_public_address)
        if not dlg.exec() or dlg.choice is None:
            return
        choice = dlg.choice
        if choice.password:
            host.secrets.set_password(choice.password)
        s = self.session.settings
        s["server_mode"] = choice.mode
        s["server_port"] = choice.port
        s["server_hostname"] = choice.hostname
        s["server_cert_path"] = choice.cert_path
        s["server_key_path"] = choice.key_path
        save_settings(s)
        self._start_serving(announce_errors=True)

    def _maybe_start_server_on_launch(self) -> bool:
        """Serve again if it was on when HuaEPUB closed. True when serving."""
        if not self.session.settings.get("server_enabled"):
            return False
        return self._start_serving(announce_errors=True, on_launch=True)

    def _start_serving(self, *, announce_errors: bool, on_launch: bool = False) -> bool:
        s = self.session.settings
        # The browser edits the same settings: write what the tabs show first.
        self._persist_settings()
        self._save_reader_position()
        host = self._server_host_obj()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            host.start(mode=s.get("server_mode") or "lan",
                       port=int(s.get("server_port") or 8765),
                       hostname=s.get("server_hostname") or "",
                       cert_path=s.get("server_cert_path") or "",
                       key_path=s.get("server_key_path") or "")
        except Exception as exc:  # ServerStartError, ports, certificate files, cryptography, …
            QApplication.restoreOverrideCursor()
            s["server_enabled"] = False
            save_settings(s)
            if announce_errors:
                lead = ("Server mode was on when HuaEPUB closed, but it could not start again."
                        if on_launch else "Server mode could not start.")
                show_error(self, "Server mode", f"{lead}\n\n{exc}\n\nThe desktop app is open "
                                                "as usual.")
            return False
        QApplication.restoreOverrideCursor()
        s["server_enabled"] = True
        save_settings(s)
        self._enter_server_view()
        return True

    def _enter_server_view(self) -> None:
        self.resume_banner.hide_banner()
        self._server_stack.setCurrentWidget(self.server_screen)
        self._clipboard_timer.stop()
        for action in self._desktop_only_actions():
            action.setEnabled(False)
        self.setWindowTitle(f"{self._base_window_title()} · Serving")
        self._show_server_status()
        self._server_timer.start()

    def _base_window_title(self) -> str:
        from core.branding import APP_TITLE

        return f"{APP_TITLE} v{get_current_version()}"

    def _desktop_only_actions(self):
        """Menu items that start desktop jobs or touch the cache while serving."""
        return list(getattr(self, "_server_locked_actions", []))

    def _show_server_status(self) -> None:
        host = self._server_host
        if host is None or not host.running:
            return
        self.server_screen.show_status(host.status, code=host.secrets.lan_code,
                                       qr_text=host.phone_link())
        self._refresh_server_screen()

    @Slot()
    def _refresh_server_screen(self) -> None:
        host = self._server_host
        if host is None or not host.running:
            if self._server_stack.currentWidget() is self.server_screen:
                self._leave_server_view()
                show_error(self, "Server mode", "The server stopped unexpectedly. "
                                                "The desktop app is back.")
            return
        self.server_screen.show_task(host.task_snapshot())

    # -- while serving -----------------------------------------------------
    @Slot()
    def _server_open_browser(self) -> None:
        host = self._server_host
        if host is None or not host.running:
            return
        link = host.open_link()
        if link:
            QDesktopServices.openUrl(QUrl(link))

    @Slot()
    def _server_new_code(self) -> None:
        host = self._server_host
        if host is None:
            return
        if not ask_yes_no(self, "New access code",
                          "Make a new access code? Every phone and browser signed in "
                          "with the old one has to sign in again."):
            return
        host.secrets.rotate_code()
        self._show_server_status()

    @Slot()
    def _server_change_password(self) -> None:
        host = self._server_host
        if host is None:
            return
        dlg = ChangePasswordDialog(self)
        if dlg.exec() and dlg.password:
            host.secrets.set_password(dlg.password)
            show_info(self, "Change password", "Password changed. Devices signed in with the "
                                               "old password have to sign in again.")

    # -- stop --------------------------------------------------------------
    @Slot()
    def _stop_serving_clicked(self) -> None:
        host = self._server_host
        if host is None:
            return
        if host.task_busy():
            if not ask_yes_no(self, "Stop serving",
                              "A job started from the browser is still running. Stop serving "
                              "anyway?\n\nA download stops between chapters and keeps its "
                              "resume point, so you can resume it here."):
                return
        self._stop_serving(remember_off=True)

    def _stop_serving(self, *, remember_off: bool, leave_view: bool = True) -> None:
        host = self._server_host
        self._server_timer.stop()
        if host is not None and host.running:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                host.stop()
            finally:
                QApplication.restoreOverrideCursor()
        if remember_off:
            self.session.settings["server_enabled"] = False
            save_settings(self.session.settings)
        if leave_view and self._server_stack.currentWidget() is self.server_screen:
            self._leave_server_view()

    def _leave_server_view(self) -> None:
        self._server_timer.stop()
        self._server_stack.setCurrentIndex(0)
        for action in self._desktop_only_actions():
            action.setEnabled(True)
        self.setWindowTitle(self._base_window_title())
        self._sync_options_from_settings()
        if self.options.clipboard_cb.isChecked():
            self._clipboard_timer.start(3000)
        self.library.refresh()
        QTimer.singleShot(0, self._check_resume_job)

    def _sync_options_from_settings(self) -> None:
        """Show what the browser changed (Settings page) in the options bar."""
        s = dict(self.session.settings)
        snapshot = {
            "translate": s.get("translate", True),
            "clean": s.get("clean", True),
            "use_cache": s.get("use_chapter_cache", True),
            "workers": s.get("workers", 200),
            "backend": s.get("translation_backend", "google"),
            "glossary": s.get("translation_glossary", "auto"),
            "ollama_polish": s.get("ollama_polish", False),
        }
        self.options.blockSignals(True)
        try:
            self.options.apply_snapshot(snapshot)
        finally:
            self.options.blockSignals(False)
        font = s.get("reader_font_pt")
        if font:
            self.reader.set_font_pt(int(font))

    def _shutdown_server_for_close(self) -> None:
        """Window closing: stop serving but remember it was on."""
        if self._serving():
            self._sync_options_from_settings()
            self._stop_serving(remember_off=False, leave_view=False)
