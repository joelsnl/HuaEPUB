# Author: joelsnl and Anthropic Claude
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal, Slot

from core.tasks import DRIVE_SYNC_CANCELLED


class DriveConnectWorker(QObject):
    finished = Signal(bool, str, str)  # ok, email, error

    def __init__(self, drive_sync, parent=None):
        super().__init__(parent)
        self.drive_sync = drive_sync

    @Slot()
    def run(self):
        try:
            email = self.drive_sync.login()
            self.finished.emit(True, email or "", "")
        except Exception as e:
            self.finished.emit(False, "", str(e))


class DriveSyncWorker(QObject):
    finished = Signal(str, str)  # summary, error
    progress = Signal(str)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    @Slot()
    def run(self):
        from core.tasks import DriveSyncCancelled, run_drive_sync

        try:
            summary = run_drive_sync(
                self.session,
                progress=self.progress.emit,
                cancelled=self._cancelled,
            )
            self.finished.emit(summary, "")
        except DriveSyncCancelled:
            self.finished.emit(DRIVE_SYNC_CANCELLED, "")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished.emit("", str(e))
