# Author: joelsnl and Anthropic Claude
"""MainWindow mixin: library check/update/remove/Download EPUB."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Slot
from PySide6.QtGui import QDesktopServices

from core.download_job import save_job
from core.download_runner import completion_dialog_title, downloads_folder, epub_path
from core.notify import notify
from core.settings import get_default_books_dir
from gui.dialogs import ask_yes_no, show_info, show_warning
from gui.window.worker_host import _is_gui_thread
from gui.workers.download_worker import (
    LibraryCheckWorker,
    LibraryUpdateAllWorker,
    LibraryUpdateWorker,
)


class LibraryActionsMixin:
    def _start_library_check(self):
        entries = self.session.library_store.get_library()
        if not entries:
            show_info(self, "Library", "No tracked novels yet.")
            return
        if self.session.control.is_downloading:
            self.progress.set_status("Busy — wait for the current download to finish")
            return
        if self._is_check_running():
            self.progress.set_status("Already checking library…")
            return
        self.library.set_check_busy(True)
        for e in entries:
            self.library.check_status[e.source_url] = {"state": "checking"}
        self.library.refresh()
        n = len(entries)
        first = entries[0].translated_title or entries[0].title or "library"
        self.progress.set_status(f"Checking 1/{n}: {first[:40]}…")
        worker = LibraryCheckWorker(self.session, entries, self.options.snapshot())
        if not self._bind_and_run_check(
            worker,
            (worker.entry_done, self._on_library_entry_status),
            (worker.progress, self._on_library_check_progress),
            (worker.finished, self._library_check_done),
        ):
            self.library.set_check_busy(False)
            self.progress.set_status("Busy — wait for the current download to finish")
            return

    @Slot(str, object)
    def _on_library_entry_status(self, url: str, st: object):
        if not _is_gui_thread(self):
            self._call_on_gui(
                lambda u=url, s=st: self._on_library_entry_status(u, s)
            )
            return
        self.library.apply_entry_status(url, st)

    @Slot(int, int, str)
    def _on_library_check_progress(self, idx: int, total: int, name: str):
        if not _is_gui_thread(self):
            self._call_on_gui(
                lambda i=idx, t=total, n=name: self._on_library_check_progress(i, t, n)
            )
            return
        current = idx if idx >= 1 else 1
        shown = name[:40] if name else ""
        self.progress.set_status(f"Checking {current}/{total}: {shown}…")

    @Slot(int, int)
    def _library_check_done(self, with_updates: int, total: int):
        if not _is_gui_thread(self):
            self._call_on_gui(
                lambda w=with_updates, t=total: self._library_check_done(w, t)
            )
            return
        self.library.set_check_busy(False)
        if with_updates:
            msg = f"{with_updates}/{total} novel(s) have new chapters"
            self.library.status_label.setText(msg)
            self.progress.set_status(msg)
            notify("Library updates available", msg)
        else:
            msg = f"All {total} novel(s) up to date"
            self.library.status_label.setText(msg)
            self.progress.set_status(msg)
        self.library.refresh()
        self._finish_check_worker_later()

    @Slot(object)
    def _start_library_update(self, entry):
        if isinstance(entry, list):
            entries = [e for e in entry if e]
            if not entries:
                return
            if len(entries) > 1:
                self._start_library_update_many(entries)
                return
            entry = entries[0]
        if entry is None:
            return
        if self.session.control.is_downloading or self._worker_busy or self._is_check_running():
            self.progress.set_status("Busy — wait for the current job to finish")
            return
        self._persist_settings()
        self._set_downloading(True)
        self.tabs.setCurrentWidget(self.library)
        o = self.options.snapshot()
        worker = LibraryUpdateWorker(self.session, entry, o)
        if not self._bind_and_run(
            worker,
            (worker.progress, self._on_progress),
            (worker.finished_ok, self._lib_update_ok),
            (worker.finished_cancel, self._download_cancelled),
            (worker.finished_error, self._download_error),
            (worker.up_to_date, self._lib_up_to_date),
        ):
            self._set_downloading(False)
            return

    def _start_library_update_many(self, entries):
        self._queue_library_update_batch(
            entries,
            title="Update",
            confirm=f"Update {len(entries)} selected novel(s)?",
        )

    @Slot(str)
    def _lib_update_ok(self, msg: str):
        if not _is_gui_thread(self):
            self._call_on_gui(lambda m=msg: self._lib_update_ok(m))
            return
        self._set_downloading(False)
        self.progress.set_progress(1.0, "Library updated")
        show_info(self, completion_dialog_title(msg, "Library updated"), msg)
        self.library.refresh()
        self._queue_drive_sync()
        self._finish_worker_later()

    @Slot(str)
    def _lib_up_to_date(self, display: str):
        if not _is_gui_thread(self):
            self._call_on_gui(lambda d=display: self._lib_up_to_date(d))
            return
        self._set_downloading(False)
        show_info(self, "Up to date", f"No new chapters for:\n{display}")
        self._finish_worker_later()

    def _start_library_update_all(self):
        if self._worker_busy or self.session.control.is_downloading or self._is_check_running():
            self.progress.set_status("Busy — wait for the current job to finish")
            return
        entries = [
            e for e in self.session.library_store.get_library()
            if (self.library.check_status.get(e.source_url) or {}).get("state") == "update"
        ]
        if not entries:
            show_info(self, "Update All", "No novels with updates. Run Check updates first.")
            return
        self._queue_library_update_batch(
            entries,
            title="Update All",
            confirm=f"Update {len(entries)} novel(s)?",
        )

    def _queue_library_update_batch(self, entries, *, title: str, confirm: str):
        if self._worker_busy or self.session.control.is_downloading or self._is_check_running():
            self.progress.set_status("Busy — wait for the current job to finish")
            return
        if not entries:
            show_info(self, title, "No novels to update.")
            return
        if not ask_yes_no(self, title, confirm):
            return
        self._persist_settings()
        o = self.options.snapshot()
        job = {
            "kind": "library_update_all",
            "status": "running",
            "options": o,
            "entries": [
                {
                    "source_url": e.source_url,
                    "title": e.title or "",
                    "translated_title": e.translated_title or "",
                    "done": False,
                }
                for e in entries
            ],
        }
        self.session.control.active_job = job
        save_job(job, self.session.data_dir)
        self._run_library_update_all(entries, label=title)

    def _run_library_update_all(self, entries, label: str = "Update All"):
        self.resume_banner.hide_banner()
        self._set_downloading(True)
        o = self.options.snapshot()
        worker = LibraryUpdateAllWorker(self.session, entries, o, label=label)
        if not self._bind_and_run(
            worker,
            (worker.progress, self._on_progress),
            (worker.finished_ok, self._lib_update_all_done),
        ):
            self._set_downloading(False)
            return

    @Slot(str)
    def _lib_update_all_done(self, summary: str):
        if not _is_gui_thread(self):
            self._call_on_gui(lambda s=summary: self._lib_update_all_done(s))
            return
        self._set_downloading(False)
        self.progress.set_progress(1.0, summary)
        show_info(self, completion_dialog_title(summary, "Update All"), summary)
        self.library.refresh()
        job = self.session.control.active_job
        if job and job.get("kind") == "library_update_all":
            pending = [e for e in job.get("entries") or [] if not e.get("done")]
            if pending:
                self.resume_banner.show_job(job, self.session.cache)
        self._queue_drive_sync()
        self._finish_worker_later()

    def _open_library_url(self, url: str):
        self.tabs.setCurrentWidget(self.single)
        self.single.set_url(url)

    @Slot(object)
    def _remove_library(self, urls):
        if isinstance(urls, str):
            urls = [urls]
        urls = [(u or "").strip() for u in (urls or [])]
        urls = [u for u in urls if u]
        if not urls:
            return
        entries = []
        titles = []
        for url in urls:
            entry = self.session.library_store.get_library_entry(url)
            if entry:
                entries.append(entry)
                titles.append(entry.translated_title or entry.title or url)
            else:
                titles.append(url)
        drive_on = bool(self.library.drive_enabled.isChecked())
        extra = (
            "\n• the Google Drive EPUB and library.json entry "
            "(it will not come back on the next sync)"
            if drive_on
            else "\n• a sync marker so Google Drive cannot restore it later"
        )
        if len(urls) == 1:
            heading = f'Remove “{titles[0]}” from your library?'
        else:
            shown = titles[:8]
            rest = len(titles) - len(shown)
            listing = "\n".join(f"• {t}" for t in shown)
            if rest:
                listing += f"\n• and {rest} more"
            heading = f"Remove {len(urls)} novels from your library?\n\n{listing}"
        noun = "this novel" if len(urls) == 1 else "these novels"
        msg = (
            f"{heading}\n\n"
            "This deletes:\n"
            "• the local EPUB in your books folder\n"
            f"• chapter, cover, and table-of-contents cache for {noun}\n"
            "• the reading position on this PC"
            f"{extra}"
        )
        if not ask_yes_no(self, "Remove", msg):
            return
        from core.library import purge_novel_artifacts

        extra_dirs = [get_default_books_dir()]
        custom = (self.session.output_dir or "").strip()
        if custom:
            extra_dirs.append(Path(custom))
        by_url = {e.source_url: e for e in entries}
        for url in urls:
            removed = self.session.library_store.remove_library(url)
            target = removed or by_url.get(url)
            if target:
                purge_novel_artifacts(
                    target, cache=self.session.cache, extra_dirs=extra_dirs,
                    data_dir=self.session.data_dir,
                )
        self.library.refresh()
        if drive_on and self.session.drive_sync.is_connected():
            self._start_drive_sync(silent=True)

    @Slot(object)
    def _download_library_epub(self, payload):
        entries = payload if isinstance(payload, list) else [payload]
        entries = [e for e in entries if e]
        if not entries:
            return
        if len(entries) == 1:
            self._download_one_library_epub(entries[0], open_folder=True)
            return
        local_n = 0
        saved = []
        missing = []
        errors = []
        for entry in entries:
            kind, detail = self._download_one_library_epub(entry, open_folder=False)
            if kind == "local":
                local_n += 1
            elif kind == "saved":
                saved.append(detail)
            elif kind == "missing":
                missing.append(detail)
            else:
                errors.append(detail)
        lines = []
        if saved:
            lines.append(f"Downloaded {len(saved)} EPUB(s) from Drive.")
        if local_n:
            lines.append(f"{local_n} already on disk.")
        if missing:
            lines.append(f"{len(missing)} had no local or Drive EPUB.")
        if errors:
            lines.append("Errors:\n" + "\n".join(errors[:8]))
        body = "\n".join(lines) if lines else "Nothing to download."
        if errors:
            show_warning(self, "Download EPUB", body)
        else:
            show_info(self, "Download EPUB", body)

    def _download_one_library_epub(self, entry, *, open_folder: bool):
        from core.security import is_allowed_epub_path

        folder = downloads_folder(self.options.snapshot().get("output_dir", ""))
        roots = [get_default_books_dir(), folder]
        title = entry.translated_title or entry.title or entry.source_url or "book"
        path = entry.output_path
        if path and Path(path).is_file() and is_allowed_epub_path(Path(path), roots):
            if open_folder:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
            return "local", title
        if entry.drive_file_id:
            dest = epub_path(
                folder,
                entry.title or "book",
                preferred_name=entry.epub_filename or "",
            )
            try:
                self.session.drive_sync.download_epub(
                    entry.drive_file_id, dest, allowed_root=folder
                )
                if open_folder:
                    show_info(self, "Download", f"Saved to:\n{dest}")
                return "saved", str(dest)
            except Exception as e:
                if open_folder:
                    show_warning(self, "Download EPUB", str(e))
                    return "error", str(e)
                return "error", f"{title}: {e}"
        if open_folder:
            show_info(self, "Download EPUB", "No local or Drive EPUB found for this entry.")
        return "missing", title

    def _reset_library(self):
        if not ask_yes_no(
            self, "Reset library",
            "Clear all tracked novels from your local library?\n\n"
            "They will not come back on Drive sync. Local EPUB files are kept.",
        ):
            return
        self.session.library_store.clear(clear_library=True, clear_history=False)
        self.library.check_status.clear()
        self.library.refresh()
        if self.library.drive_enabled.isChecked() and self.session.drive_sync.is_connected():
            self._start_drive_sync(silent=True)
