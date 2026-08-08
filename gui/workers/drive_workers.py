# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot


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

    def __init__(self, session, silent: bool = True, parent=None):
        super().__init__(parent)
        self.session = session
        self.silent = silent

    @Slot()
    def run(self):
        try:
            ds = self.session.drive_sync
            if not ds.is_connected():
                self.progress.emit("Restoring Drive session…")
                if not ds.try_restore_session():
                    self.finished.emit("", "Not connected")
                    return
            summary_parts = []
            if self.session.settings.get("drive_sync_library", True):
                self.progress.emit("Syncing library.json…")
                ds.sync_library_with_store(self.session.library_store)
                summary_parts.append("library")
            if self.session.settings.get("drive_sync_epubs", True):
                self.progress.emit("Listing remote EPUBs…")
                remote = ds.list_remote_books()
                uploaded = 0
                library = self.session.library_store.get_library()
                pending = []
                for entry in library:
                    path = entry.output_path or ""
                    name = entry.epub_filename or (Path(path).name if path else "")
                    if path and name and Path(path).is_file() and name not in remote:
                        pending.append((path, name))
                for i, (path, name) in enumerate(pending):
                    self.progress.emit(f"Uploading EPUB {i + 1}/{len(pending)}: {name[:40]}")
                    try:
                        ds.upload_epub(path, name)
                        uploaded += 1
                    except Exception as e:
                        print(f"Drive upload failed for {name}: {e}")
                summary_parts.append(f"epubs(+{uploaded})")
            from core.settings import save_settings
            import time
            self.session.settings["drive_last_synced_at"] = time.time()
            summary = "Synced " + ", ".join(summary_parts) if summary_parts else "Sync done"
            self.session.settings["drive_last_sync_summary"] = summary
            save_settings(self.session.settings)
            self.progress.emit(summary)
            self.finished.emit(summary, "")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished.emit("", str(e))
