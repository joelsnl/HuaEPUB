# Author: joelsnl and Anthropic Claude
"""MainWindow mixin: checking for and installing an app update."""

from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QApplication

from core.branding import APP_TITLE
from core.updater import check_for_updates_async, download_update_async, get_current_version
from gui.dialogs import UpdateProgressDialog, ask_yes_no, show_info, show_warning


class UpdateActionsMixin:
    def _auto_check_updates(self):
        self._app_update_checking = True
        check_for_updates_async(callback=self._update_check_cb, force=False)

    def _manual_check_updates(self):
        self._update_check_notify = True
        self._app_update_checking = True
        self.progress.set_status("Checking for app updates…")
        check_for_updates_async(callback=self._update_check_cb, force=True)

    def _update_check_cb(self, has_update, latest, message):
        # Runs on a plain threading.Thread — never touch Qt widgets here.
        self._sig_update_check.emit(
            bool(has_update),
            str(latest or ""),
            str(message or ""),
        )

    @Slot(bool, str, str)
    def _on_update_check_ready(self, has_update: bool, latest: str, message: str):
        now = time.monotonic()
        key = (bool(has_update), str(latest), str(message))
        prev = getattr(self, "_last_app_update_check", None)
        if prev is not None and prev[0] == key and (now - prev[1]) < 2.0:
            self._app_update_checking = False
            return
        self._last_app_update_check = (key, now)
        notify = bool(getattr(self, "_update_check_notify", False))
        self._update_check_notify = False

        failed = (message or "").startswith("Failed to check")
        if has_update:
            self._app_update_pending = True
            self._app_update_checking = False
            self.progress.set_status(f"Update available: {latest}")
            accepted = ask_yes_no(
                self, "Update available",
                f"{message}\n\nDownload and install?",
            )
            self._app_update_pending = False
            if accepted:
                self._begin_app_update(latest)
            return
        self._app_update_checking = False
        self.progress.set_status(message or "App is up to date")
        if notify:
            if failed:
                show_warning(self, "Updates", message or "Failed to check for updates.")
            else:
                show_info(
                    self, "Updates",
                    message or f"You're running the latest version ({get_current_version()}).",
                )

    def _begin_app_update(self, version: str = ""):
        """Hide the main window and download the update."""
        self._app_update_installing = True
        self._open_update_progress("Connecting to GitHub…", version)
        download_update_async(
            progress_callback=lambda c, t, s: self._sig_update_progress.emit(
                int(c or 0), int(t or 0), s or "Downloading update…"
            ),
            completion_callback=lambda ok, msg: self._sig_update_done.emit(
                bool(ok), str(msg or "")
            ),
        )

    def _open_update_progress(self, text: str, version: str = "") -> None:
        dlg = UpdateProgressDialog(version)
        dlg.restart_requested.connect(self._restart_for_update)
        dlg.set_progress(0, 100, text)
        center = self.frameGeometry().center()
        self.hide()
        dlg.adjustSize()
        dlg.move(center.x() - dlg.width() // 2, center.y() - dlg.height() // 2)
        self._update_progress_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _close_update_progress(self) -> None:
        dlg = self._update_progress_dlg
        self._update_progress_dlg = None
        if dlg is None:
            return
        dlg.allow_close()
        dlg.close()
        dlg.deleteLater()

    @Slot(int, int, str)
    def _on_update_progress(self, current: int, total: int, text: str):
        dlg = self._update_progress_dlg
        if dlg is not None:
            dlg.set_progress(current, total, text)

    @Slot(bool, str)
    def _on_update_download_done(self, ok: bool, message: str):
        if not ok:
            self._app_update_installing = False
            self._close_update_progress()
            self.show()
            self.raise_()
            self.activateWindow()
            self.progress.set_status(message or "Update failed")
            show_warning(self, "Update failed", message or "Update failed.")
            return
        # Same window, ready state: no second box stacked on the progress dialog.
        self._update_progress_dlg.show_ready(
            f"{APP_TITLE} will close and reopen to finish installing. "
            "Your library and downloads are kept."
        )

    @Slot()
    def _restart_for_update(self):
        """Quit so the staged helper can swap the binary and relaunch."""
        self._close_update_progress()
        self._exiting_for_update = True
        self.close()
        app = QApplication.instance()
        if app is None:
            os._exit(0)
        app.quit()

        # Helpers wait for this PID. Qt may tear down timers with the
        # window; a daemon thread still force-exits if something hangs.
        def _exit_soon():
            import time
            time.sleep(2.5)
            os._exit(0)

        threading.Thread(target=_exit_soon, daemon=True).start()
