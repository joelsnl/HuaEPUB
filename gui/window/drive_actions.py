# Author: joelsnl and Anthropic Claude
"""MainWindow mixin: Google Drive connect/sync/folder."""

from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QInputDialog

from core.drive_sync import oauth_setup_instructions
from gui.dialogs import show_error, show_info, show_warning
from gui.window.worker_host import _is_gui_thread
from gui.workers.drive_workers import DriveConnectWorker, DriveSyncWorker


class DriveActionsMixin:
    def _drive_connect(self):
        if not self.session.drive_sync.client_configured():
            show_info(self, "Drive setup", oauth_setup_instructions())
            return
        self.progress.set_status("Connecting to Google Drive…")
        worker = DriveConnectWorker(self.session.drive_sync)
        if not self._bind_and_run(worker, (worker.finished, self._drive_connect_done)):
            self.progress.set_status("Busy — wait for the current job to finish")
            return

    @Slot(bool, str, str)
    def _drive_connect_done(self, ok: bool, email: str, err: str):
        if not _is_gui_thread(self):
            self._call_on_gui(
                lambda o=ok, em=email, e=err: self._drive_connect_done(o, em, e)
            )
            return
        if ok:
            self.library.drive_status.setText(f"Connected: {email}")
            self.progress.set_status(f"Drive connected: {email}")
            # Finish connect thread first, then start sync (don't kill sync with stop)
            self._queue_drive_sync()
            self._finish_worker_later()
        else:
            show_error(self, "Drive", err or "Connect failed")
            self._finish_worker_later()

    def _drive_disconnect(self):
        try:
            self.session.drive_sync.logout()
        except Exception:
            pass
        self.library.drive_status.setText("Disconnected")

    def _start_drive_sync(self, silent: bool = True):
        if not _is_gui_thread(self):
            self._drive_sync_silent = silent
            self._call_on_gui(self._start_drive_sync_silent)
            return
        if not self.library.drive_enabled.isChecked():
            self._pending_drive_sync = False
            return
        self._drive_sync_silent = silent
        if self._worker_busy and self._thread and self._thread.isRunning():
            self._pending_drive_sync = True
            if not getattr(self, "_check_busy", False) and not getattr(
                self, "_app_update_checking", False
            ):
                self.progress.set_status("Drive sync queued…")
            return
        self._pending_drive_sync = False
        self._persist_settings()
        self.library.set_drive_busy(True)
        self.library.drive_status.setText("Syncing…")
        if not getattr(self, "_check_busy", False) and not getattr(
            self, "_app_update_checking", False
        ):
            self.progress.set_status("Syncing with Google Drive…")
        worker = DriveSyncWorker(self.session)
        if not self._bind_and_run(
            worker,
            (worker.progress, self._on_drive_sync_progress),
            (worker.finished, self._on_drive_sync_finished),
        ):
            self.library.set_drive_busy(False)
            self._pending_drive_sync = True
            if not getattr(self, "_check_busy", False) and not getattr(
                self, "_app_update_checking", False
            ):
                self.progress.set_status("Drive sync queued…")
            return

    @Slot()
    def _start_drive_sync_silent(self):
        self._start_drive_sync(silent=getattr(self, "_drive_sync_silent", True))

    @Slot()
    def _drive_sync_now(self):
        self._start_drive_sync(silent=False)

    def _queue_drive_sync(self):
        """Silent Drive push after Library Update / Update All / Connect (no tab switch)."""
        if self.library.drive_enabled.isChecked():
            self._pending_drive_sync = True
            self._drive_sync_silent = True

    @Slot(str)
    def _on_drive_sync_progress(self, msg: str):
        if not _is_gui_thread(self):
            self._call_on_gui(lambda m=msg: self._on_drive_sync_progress(m))
            return
        self.library.drive_status.setText(msg)
        # App update / library Check use other threads; don't clobber their footer.
        if getattr(self, "_app_update_checking", False):
            return
        if getattr(self, "_check_busy", False):
            return
        self.progress.set_status(msg)

    @Slot(str, str)
    def _on_drive_sync_finished(self, summary: str, err: str):
        # Mixin @Slot is not in MainWindow's QMetaObject — PySide may invoke
        # this on the Drive QThread. refresh()/QMessageBox then setParent a
        # GUI widget from the worker → crash.
        if not _is_gui_thread(self):
            self._call_on_gui(
                lambda s=summary, e=err: self._on_drive_sync_finished(s, e)
            )
            return
        silent = getattr(self, "_drive_sync_silent", True)
        checking_app = getattr(self, "_app_update_checking", False)
        checking_library = getattr(self, "_check_busy", False)
        self.library.set_drive_busy(False)
        if err:
            self.library.drive_status.setText(f"Sync error: {err[:80]}")
            if not checking_app and not checking_library:
                self.progress.set_status(f"Drive sync error: {err[:60]}")
            if not silent:
                show_warning(self, "Drive sync", err)
        else:
            self.library.drive_status.setText(summary)
            # Drive pull only needs library.json — show All so Updates filter
            # doesn't hide every novel before Check updates has been run.
            try:
                self.session.library_store.reload()
            except Exception:
                pass
            n = len(self.session.library_store.get_library())
            if not checking_library:
                self.library.show_all()
                if not silent:
                    self.tabs.setCurrentWidget(self.library)
                else:
                    self.library.refresh()
            elif not silent:
                self.tabs.setCurrentWidget(self.library)
            if not checking_app and not checking_library:
                self.progress.set_status(summary or f"Drive sync done — {n} novel(s)")
            if not silent:
                extra = ""
                if n == 0:
                    extra = (
                        "\n\nLibrary UI is still empty. Confirm ~/.huaepub/library.json "
                        "was written, or click Refresh."
                    )
                else:
                    extra = (
                        f"\n\n{n} novel(s) are in your library list now "
                        "(covers/EPUBs stay optional — no full download required)."
                    )
                show_info(self, "Drive sync", (summary or "Sync done") + extra)
        self._finish_worker_later()

    def _drive_change_folder(self):
        from core.drive_sync import DriveSync

        text, ok = QInputDialog.getText(
            self, "Drive folder",
            "Paste the Drive folder URL from your other PC\n"
            "(Library → Open folder), or type a folder name:",
            text=self.session.settings.get("drive_folder_name") or "HuaEPUB",
        )
        if not ok:
            return
        text = (text or "").strip()
        if not text:
            return
        try:
            parsed = DriveSync.parse_folder_id(text)
            if parsed:
                self.session.drive_sync.set_custom_folder(folder_url_or_id=text)
            else:
                self.session.drive_sync.set_custom_folder(folder_name=text)
            info = self.session.drive_sync.inspect_sync_folder()
            label = info.get("name") or self.session.drive_sync.location_description()
            novels = int(info.get("library_novels") or 0)
            epubs = int(info.get("epub_count") or 0)
            detail = (
                f"Using: {label}\n"
                f"library.json novels: {novels}\n"
                f"EPUB files in books/: {epubs}\n"
                f"{info.get('web_link') or ''}"
            )
            if info.get("error"):
                show_warning(
                    self, "Drive folder",
                    detail + f"\n\nWarning: {info['error']}",
                )
            else:
                show_info(self, "Drive folder", detail)
            self.library.drive_status.setText(
                f"{label} — {novels} novel(s), {epubs} EPUB(s) on Drive"
            )
            # Pull immediately so Library fills from this folder
            QTimer.singleShot(100, self._drive_sync_now)
        except Exception as e:
            show_error(self, "Drive folder", str(e))

    def _drive_open_folder(self):
        link = ""
        try:
            link = self.session.drive_sync.folder_web_link() or ""
        except Exception:
            pass
        if link:
            QDesktopServices.openUrl(QUrl(link))
        else:
            show_info(self, "Open folder", "Connect to Drive first.")

    def _drive_setup_help(self):
        show_info(self, "Drive OAuth setup", oauth_setup_instructions())
