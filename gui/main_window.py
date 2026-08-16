# Author: joelsnl and Anthropic Claude
"""Main Qt window: modes, workers, pause/resume, menus."""

from __future__ import annotations

import traceback
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QInputDialog, QLabel, QListWidget,
    QMainWindow, QMessageBox, QTabWidget, QVBoxLayout, QWidget,
)

from core.branding import (
    APP_AUTHOR, APP_AUTHOR_HANDLE, APP_DESCRIPTION, APP_LICENSE,
    APP_REPO_URL, APP_TITLE, LOG_FILE_NAME,
)
from core.download_job import (
    chapters_from_job, chapters_to_job, clear_job, load_job,
    novel_info_from_job, novel_info_to_job, save_job,
)
from core.download_runner import downloads_folder, epub_path
from core.logger import setup_logging
from core.notify import notify
from core.parser import cleanup_browser, create_http_session, get_parser_for_url
from core.settings import get_default_books_dir, save_settings
from core.updater import (
    check_for_updates_async, download_update_async, get_auto_check_updates,
    get_current_version, set_auto_check_updates,
)
from core.utils import extract_urls, looks_like_url, sanitize_runtime_env
from core.drive_sync import oauth_setup_instructions

from gui.pages.library_page import LibraryPage
from gui.pages.multi_page import MultiPage
from gui.pages.single_page import SinglePage
from gui.session import AppSession
from core.download_runner import translator_backend_kwargs
from gui.widgets.options_bar import OptionsBar
from gui.widgets.progress_panel import ProgressPanel
from gui.widgets.resume_banner import ResumeBanner
from gui.workers.download_worker import (
    LibraryCheckWorker, LibraryUpdateAllWorker, LibraryUpdateWorker,
    MultiDownloadWorker, SingleDownloadWorker,
)
from gui.workers.drive_workers import DriveConnectWorker, DriveSyncWorker
from gui.workers.fetch_worker import FetchWorker

import parsers  # noqa: F401 — register site parsers


class MainWindow(QMainWindow):
    # Cross-thread marshaling for plain threading.Thread callbacks (updater, etc.)
    _sig_update_check = Signal(bool, str, str)
    _sig_update_done = Signal(bool, str)
    _sig_status = Signal(str)

    def __init__(self):
        super().__init__()
        sanitize_runtime_env()
        self.session = AppSession()
        setup_logging(self.session.data_dir)
        self.setWindowTitle(f"{APP_TITLE} v{get_current_version()}")
        self.resize(960, 720)
        self.setMinimumSize(800, 600)

        self._thread: QThread | None = None
        self._worker = None
        self._worker_busy = False
        self._clipboard_last = ""
        self._clipboard_seen = set()
        self._http = create_http_session()

        self._sig_update_check.connect(
            self._on_update_check_ready, Qt.ConnectionType.QueuedConnection
        )
        self._sig_update_done.connect(
            self._on_update_download_done, Qt.ConnectionType.QueuedConnection
        )
        self._sig_status.connect(
            self._set_status_safe, Qt.ConnectionType.QueuedConnection
        )

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.resume_banner = ResumeBanner()
        self.resume_banner.resume_clicked.connect(self._on_resume_job)
        self.resume_banner.discard_clicked.connect(self._on_discard_job)
        layout.addWidget(self.resume_banner)

        self.tabs = QTabWidget()
        self.single = SinglePage()
        self.multi = MultiPage()
        self.library = LibraryPage(self.session)
        self.tabs.addTab(self.single, "Single")
        self.tabs.addTab(self.multi, "Multi")
        self.tabs.addTab(self.library, "Library")
        layout.addWidget(self.tabs, 1)

        self.options = OptionsBar(self.session)
        layout.addWidget(self.options)

        self.progress = ProgressPanel()
        layout.addWidget(self.progress)

        self._build_menu()
        self._wire()

        QTimer.singleShot(400, self._check_resume_job)
        if get_auto_check_updates():
            QTimer.singleShot(2000, self._auto_check_updates)
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.timeout.connect(self._poll_clipboard)
        self._clipboard_timer.start(3000)
        if self.session.settings.get("drive_sync_enabled"):
            QTimer.singleShot(2500, lambda: self._start_drive_sync(silent=True))
        if self.session.library_store.get_library():
            QTimer.singleShot(4000, self.library.refresh)

    def _build_menu(self):
        mb = self.menuBar()
        file_m = mb.addMenu("File")
        file_m.addAction("Open books folder", self._open_books)
        file_m.addAction("Open data folder", self._open_data)
        file_m.addAction("Open log file", self._open_log)
        file_m.addSeparator()
        file_m.addAction("Exit", self.close)

        lib_m = mb.addMenu("Library")
        lib_m.addAction("Check for updates", lambda: self.library.check_requested.emit())
        lib_m.addAction("Sync Drive now", lambda: self._start_drive_sync(silent=False))
        lib_m.addAction("Reset library…", self._reset_library)

        help_m = mb.addMenu("Help")
        help_m.addAction("Check for updates", self._manual_check_updates)
        act = QAction("Auto-check updates on startup", self, checkable=True)
        act.setChecked(bool(get_auto_check_updates()))
        act.toggled.connect(set_auto_check_updates)
        help_m.addAction(act)
        help_m.addAction("How translation works…", self._translation_help)
        help_m.addAction("About", self._about)
        help_m.addAction("Drive OAuth setup…", self._drive_setup_help)

    def _wire(self):
        self.single.fetch_requested.connect(self._start_fetch)
        self.single.recent_requested.connect(self._show_recent)
        self.progress.download_clicked.connect(self._start_single_download)
        self.progress.pause_clicked.connect(self._toggle_pause)
        self.progress.cancel_clicked.connect(self._cancel_download)

        self.multi.fetch_all_requested.connect(self._start_multi_fetch)
        self.multi.download_all_requested.connect(self._start_multi_download)

        self.library.check_requested.connect(self._start_library_check)
        self.library.update_all_requested.connect(self._start_library_update_all)
        self.library.update_selected.connect(self._start_library_update)
        self.library.open_selected.connect(self._open_library_url)
        self.library.remove_selected.connect(self._remove_library)
        self.library.refresh_requested.connect(self.library.refresh)
        self.library.drive_connect.connect(self._drive_connect)
        self.library.drive_sync.connect(lambda: self._start_drive_sync(silent=False))
        self.library.drive_disconnect.connect(self._drive_disconnect)
        self.library.drive_change_folder.connect(self._drive_change_folder)
        self.library.drive_open_folder.connect(self._drive_open_folder)
        self.library.view_changed.connect(lambda v: self._persist_settings())
        self.library.filter_changed.connect(lambda v: self._persist_settings())
        self.library.download_epub_selected.connect(self._download_library_epub)
        self.options.options_changed.connect(self._persist_settings)

    # ------------------------------------------------------------------
    # Settings / close
    # ------------------------------------------------------------------

    def _persist_settings(self):
        o = self.options.snapshot()
        self.session.save_settings_from_options(
            translate=o["translate"],
            clean=o["clean"],
            use_cache=o["use_cache"],
            clipboard=o["clipboard"],
            workers=o["workers"],
            backend=o["backend"],
            ollama_model=o.get("ollama_model", "qwen2.5:3b"),
            ollama_url=o.get("ollama_url", "http://127.0.0.1:11434"),
            ollama_polish=bool(o.get("ollama_polish", False)),
            drive_enabled=self.library.drive_enabled.isChecked(),
            drive_library=self.library.drive_library.isChecked(),
            drive_epubs=self.library.drive_epubs.isChecked(),
            library_view=self.library._view,
            library_filter=self.library._filter,
            drive_panel_expanded=True,
        )

    def closeEvent(self, event):
        self._persist_settings()
        try:
            cleanup_browser()
        except Exception:
            pass
        self.session.close()
        self._stop_thread()
        event.accept()

    def _stop_thread(self):
        """Stop background worker. Must only be called from the GUI thread."""
        if QThread.currentThread() is not QApplication.instance().thread():
            # Never wait() from inside the worker thread
            QTimer.singleShot(0, self._stop_thread)
            return
        thread = self._thread
        worker = self._worker
        self._thread = None
        self._worker = None
        self._worker_busy = False
        if thread is None:
            return
        if thread.isRunning():
            thread.quit()
            if not thread.wait(5000):
                thread.terminate()
                thread.wait(1000)
        if worker is not None:
            worker.deleteLater()
        thread.deleteLater()

    def _run_worker(self, worker) -> bool:
        """
        Start a QObject worker on a QThread. Returns False if another job is busy
        (caller should show a message). Always invoke from the GUI thread.

        Connect worker signals to @Slot methods on this window *before* calling
        this. Bare lambdas/partials have no QObject receiver, so Qt may invoke
        them on the worker thread (cross-thread UI = crash).
        """
        if QThread.currentThread() is not QApplication.instance().thread():
            return False
        if self._worker_busy and self._thread and self._thread.isRunning():
            return False
        self._stop_thread()
        self._thread = QThread()  # no parent — avoids cross-thread parenting issues
        self._worker = worker
        self._worker_busy = True
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        self._thread.start()
        return True

    def _bind_and_run(self, worker, *pairs) -> bool:
        """Connect (signal, slot) pairs on the GUI thread, then start the worker."""
        for signal, slot in pairs:
            signal.connect(slot, Qt.ConnectionType.QueuedConnection)
        return self._run_worker(worker)

    def _finish_worker_later(self):
        """Cleanup after a worker finished signal (always on GUI thread)."""
        QTimer.singleShot(0, self._stop_thread)

    # ------------------------------------------------------------------
    # Progress / pause
    # ------------------------------------------------------------------

    @Slot(float, str)
    def _on_progress(self, fraction: float, status: str):
        if fraction >= 0:
            self.progress.set_progress(fraction, status or None)
        elif status:
            self.progress.set_status(status)

    def _toggle_pause(self):
        ctrl = self.session.control
        if not ctrl.is_downloading:
            return
        paused = ctrl.toggle_pause()
        ctrl.persist_job(force=True)
        self.progress.set_controls_active(True, paused=paused)
        if paused:
            self.progress.set_status("Paused — click Resume to continue (safe to close the app)")
        else:
            self.progress.set_status("Resuming…")

    def _cancel_download(self):
        self.session.control.request_cancel()
        clear_job(self.session.data_dir)
        self.session.control.active_job = None
        self.resume_banner.hide_banner()
        self.progress.set_status("Cancelling...")

    def _set_downloading(self, on: bool):
        self.session.control.is_downloading = on
        self.session.control.cancel_requested = False
        if on:
            self.session.control.is_paused = False
        self.progress.set_controls_active(on, paused=False)
        self.tabs.setEnabled(not on or True)  # keep tabs usable
        self.single.set_fetch_enabled(not on)
        self.multi.set_busy(on)
        if not on:
            self.progress.set_download_enabled(bool(self.single.chapters))

    # ------------------------------------------------------------------
    # Resume banner
    # ------------------------------------------------------------------

    def _check_resume_job(self):
        if self.session.control.is_downloading:
            return
        job = load_job(self.session.data_dir)
        if not job:
            return
        self.session.control.active_job = job
        self.resume_banner.show_job(job, self.session.cache)
        self.progress.set_status(f"Incomplete download ready: resume available")

    def _on_discard_job(self):
        if QMessageBox.question(
            self, "Discard",
            "Remove the saved resume point?\nCached chapter text stays on this PC.",
        ) != QMessageBox.Yes:
            return
        clear_job(self.session.data_dir)
        self.session.control.active_job = None
        self.resume_banner.hide_banner()

    def _on_resume_job(self):
        job = self.session.control.active_job or load_job(self.session.data_dir)
        if not job:
            self.resume_banner.hide_banner()
            return
        kind = job.get("kind")
        try:
            if kind == "single":
                self._resume_single(job)
            elif kind == "multi":
                self._resume_multi(job)
            elif kind == "library_update":
                self._resume_library_update(job)
            elif kind == "library_update_all":
                self._resume_library_update_all(job)
            else:
                QMessageBox.warning(self, "Resume", f"Unknown job type: {kind}")
                clear_job(self.session.data_dir)
        except Exception as e:
            traceback.print_exc()
            QMessageBox.critical(self, "Resume failed", str(e))

    def _resume_single(self, job: dict):
        self.tabs.setCurrentWidget(self.single)
        self.options.apply_snapshot(job.get("options") or {})
        info = novel_info_from_job(job.get("info"))
        chapters = chapters_from_job(job.get("chapters") or [])
        url = (job.get("source_url") or (info.source_url if info else "")).strip()
        parser = get_parser_for_url(url)
        if not parser or not chapters:
            raise Exception("Saved download incomplete")
        if not info:
            from core.parser import NovelInfo
            info = NovelInfo(title=job.get("title") or "Untitled", source_url=url)
        self.single.translated_title = job.get("translated_title") or None
        self.single.set_url(url)
        self.single.show_novel(info, chapters, parser)
        out = job.get("output_path") or epub_path(
            downloads_folder(self.options.snapshot().get("output_dir", "")),
            self.single.translated_title or info.title,
        )
        self._begin_single_download(parser, info, chapters, out, self.single.translated_title, job)

    def _resume_multi(self, job: dict):
        self.tabs.setCurrentWidget(self.multi)
        self.options.apply_snapshot(job.get("options") or {})
        novels = []
        for item in job.get("novels") or []:
            if item.get("done"):
                continue
            url = (item.get("source_url") or "").strip()
            chapters = chapters_from_job(item.get("chapters") or [])
            info = novel_info_from_job(item.get("info"))
            parser = get_parser_for_url(url) if url else None
            if not parser or not chapters or not info:
                continue
            novels.append({
                "url": url, "parser": parser, "info": info,
                "chapters": chapters, "status": "fetched",
                "translated_title": item.get("translated_title") or "",
            })
        if not novels:
            clear_job(self.session.data_dir)
            raise Exception("No unfinished novels left")
        self.multi.begin_fetch([n["url"] for n in novels])
        for i, n in enumerate(novels):
            self.multi.set_row(
                i, n.get("translated_title") or n["info"].title,
                len(n["chapters"]), "Queued", n,
            )
        self.session.control.active_job = job
        self._start_multi_download_with(novels)

    def _resume_library_update(self, job: dict):
        self.tabs.setCurrentWidget(self.library)
        self.options.apply_snapshot(job.get("options") or {})
        entry = self.session.library_store.get_library_entry(job.get("source_url") or "")
        if not entry:
            raise Exception("Library entry missing — try Update from Library")
        self._start_library_update(entry)

    def _resume_library_update_all(self, job: dict):
        self.tabs.setCurrentWidget(self.library)
        self.options.apply_snapshot(job.get("options") or {})
        entries = []
        for e in job.get("entries") or []:
            if e.get("done"):
                continue
            ent = self.session.library_store.get_library_entry(e.get("source_url") or "")
            if ent:
                entries.append(ent)
        if not entries:
            clear_job(self.session.data_dir)
            raise Exception("No unfinished library novels")
        self.session.control.active_job = job
        self._run_library_update_all(entries)

    # ------------------------------------------------------------------
    # Single
    # ------------------------------------------------------------------

    def _start_fetch(self, url: str):
        if self.session.control.is_downloading:
            return
        self.single.set_fetch_enabled(False)
        self.progress.set_status("Fetching…")
        o = self.options.snapshot()
        worker = FetchWorker(
            url,
            self.session.cache,
            translate_title=bool(o.get("translate")),
            **translator_backend_kwargs(self.session.settings, o),
        )
        if not self._bind_and_run(
            worker,
            (worker.status, self.progress.set_status),
            (worker.error, self._fetch_error),
            (worker.finished, self._fetch_done),
        ):
            self.single.set_fetch_enabled(True)
            self.progress.set_status("Busy — wait for the current job to finish")
            return

    @Slot(str)
    def _fetch_error(self, msg: str):
        self.single.set_fetch_enabled(True)
        QMessageBox.critical(self, "Fetch failed", msg)
        self._finish_worker_later()

    @Slot(object, list, object, str)
    def _fetch_done(self, info, chapters, parser, translated_title: str = ""):
        self.single.set_fetch_enabled(True)
        self.single.translated_title = translated_title or None
        cover = None
        if info and info.cover_url:
            try:
                from core.security import fetch_cover_bytes
                data = fetch_cover_bytes(self._http, info.cover_url, timeout=15)
                pix = QPixmap()
                if pix.loadFromData(data):
                    cover = pix
                    try:
                        self.session.cache.put_cover(
                            data, cover_url=info.cover_url,
                            source_url=info.source_url or "",
                        )
                    except Exception:
                        pass
            except Exception:
                pass
        self.single.show_novel(info, chapters, parser, cover)
        self.progress.set_download_enabled(True)
        self.progress.set_status(f"Ready — {len(chapters)} chapters")
        self._finish_worker_later()
    def _start_single_download(self):
        if not self.single.novel_info or not self.single.chapters:
            return
        selected = self.single.selected_chapters()
        if not selected:
            QMessageBox.warning(self, "Warning", "Select at least one chapter")
            return
        self._persist_settings()
        o = self.options.snapshot()
        title = self.single.translated_title or self.single.novel_info.title
        preferred = ""
        entry = self.session.library_store.get_library_entry(self.single.novel_info.source_url)
        if entry:
            preferred = entry.epub_filename or entry.output_path or ""
        out = epub_path(
            downloads_folder(o.get("output_dir", "")),
            title,
            preferred_name=Path(preferred).name if preferred else "",
            preferred_path=preferred,
        )
        job = {
            "kind": "single",
            "status": "running",
            "source_url": self.single.novel_info.source_url or "",
            "title": self.single.novel_info.title or "",
            "translated_title": self.single.translated_title or "",
            "info": novel_info_to_job(self.single.novel_info),
            "chapters": chapters_to_job(selected),
            "output_path": out,
            "options": o,
        }
        self._begin_single_download(
            self.single.parser, self.single.novel_info, selected, out,
            self.single.translated_title, job,
        )

    def _begin_single_download(self, parser, info, chapters, out, translated_title, job):
        self.resume_banner.hide_banner()
        self.session.control.active_job = job
        save_job(job, self.session.data_dir)
        self._set_downloading(True)
        o = self.options.snapshot()
        worker = SingleDownloadWorker(
            self.session, parser, info, chapters, out, translated_title, o
        )
        if not self._bind_and_run(
            worker,
            (worker.progress, self._on_progress),
            (worker.finished_ok, self._single_done),
            (worker.finished_cancel, self._download_cancelled),
            (worker.finished_error, self._download_error),
        ):
            self._set_downloading(False)
            return

    @Slot(str, list)
    def _single_done(self, path: str, failed: list):
        self._set_downloading(False)
        self.progress.set_progress(1.0, f"Done! Saved to: {path}")
        msg = f"EPUB saved to:\n{path}"
        if failed:
            msg += f"\n\n{len(failed)} chapter(s) had placeholders."
        QMessageBox.information(self, "Success", msg)
        self.library.refresh()
        self._finish_worker_later()
        self._maybe_drive_push(path)

    @Slot()
    def _download_cancelled(self):
        self._set_downloading(False)
        self.progress.set_status("Cancelled")
        self._finish_worker_later()

    @Slot(str)
    def _download_error(self, msg: str):
        self._set_downloading(False)
        job = self.session.control.active_job
        if job:
            self.resume_banner.show_job(job, self.session.cache)
        QMessageBox.critical(self, "Download failed", msg)
        self._finish_worker_later()
    # ------------------------------------------------------------------
    # Multi
    # ------------------------------------------------------------------

    def _start_multi_fetch(self):
        urls = self.multi.get_urls()
        if not urls:
            QMessageBox.warning(self, "Multi", "Paste at least one URL")
            return
        self.multi.begin_fetch(urls)
        self.progress.set_status(f"Fetching 0/{len(urls)}…")
        self.multi.set_busy(True)
        self._multi_fetch_urls = urls
        self._multi_fetch_i = 0
        self._multi_fetch_next()

    def _multi_fetch_next(self):
        urls = getattr(self, "_multi_fetch_urls", [])
        i = getattr(self, "_multi_fetch_i", 0)
        if i >= len(urls):
            self.multi.set_busy(False)
            self.progress.set_status(f"Fetched {len(self.multi.fetched_novels())}/{len(urls)}")
            return
        o = self.options.snapshot()
        worker = FetchWorker(
            urls[i],
            self.session.cache,
            translate_title=bool(o.get("translate")),
            **translator_backend_kwargs(self.session.settings, o),
        )
        if not self._bind_and_run(
            worker,
            (worker.finished, self._multi_fetch_ok),
            (worker.error, self._multi_fetch_err),
        ):
            QTimer.singleShot(100, self._multi_fetch_next)
            return

    @Slot(object, list, object, str)
    def _multi_fetch_ok(self, info, chapters, parser, translated_title: str = ""):
        urls = getattr(self, "_multi_fetch_urls", [])
        i = getattr(self, "_multi_fetch_i", 0)
        url = urls[i] if i < len(urls) else ""
        display = translated_title or (info.title if info else url)
        novel = {
            "url": url, "parser": parser, "info": info,
            "chapters": chapters, "status": "fetched",
            "translated_title": translated_title or "",
        }
        self.multi.set_row(i, display, len(chapters or []), "Ready", novel)
        self._multi_fetch_i = i + 1
        self.progress.set_status(f"Fetching {i + 1}/{len(urls)}…")
        self._finish_worker_later()
        QTimer.singleShot(80, self._multi_fetch_next)

    @Slot(str)
    def _multi_fetch_err(self, msg: str):
        urls = getattr(self, "_multi_fetch_urls", [])
        i = getattr(self, "_multi_fetch_i", 0)
        url = urls[i] if i < len(urls) else ""
        self.multi.set_row(i, url[:40], 0, f"Failed: {msg}")
        self._multi_fetch_i = i + 1
        self._finish_worker_later()
        QTimer.singleShot(80, self._multi_fetch_next)

    def _start_multi_download(self):
        novels = self.multi.fetched_novels()
        if not novels:
            return
        self._persist_settings()
        o = self.options.snapshot()
        job = {
            "kind": "multi",
            "status": "running",
            "options": o,
            "novels": [
                {
                    "source_url": n["url"],
                    "title": n["info"].title,
                    "translated_title": n.get("translated_title") or "",
                    "info": novel_info_to_job(n["info"]),
                    "chapters": chapters_to_job(n["chapters"]),
                    "done": False,
                }
                for n in novels
            ],
        }
        self.session.control.active_job = job
        save_job(job, self.session.data_dir)
        self._start_multi_download_with(novels)

    def _start_multi_download_with(self, novels):
        self.resume_banner.hide_banner()
        self._set_downloading(True)
        o = self.options.snapshot()
        worker = MultiDownloadWorker(self.session, novels, o)
        if not self._bind_and_run(
            worker,
            (worker.progress, self._on_progress),
            (worker.novel_status, self._on_multi_novel_status),
            (worker.finished_ok, self._multi_done),
            (worker.finished_cancel, self._download_cancelled),
        ):
            self._set_downloading(False)
            return

    @Slot(int, str, str)
    def _on_multi_novel_status(self, idx: int, status: str, _color: str = ""):
        self.multi.set_status(idx, status)

    @Slot(str)
    def _multi_done(self, summary: str):
        self._set_downloading(False)
        self.progress.set_progress(1.0, "Multi-download complete")
        QMessageBox.information(self, "Multi-Download Complete", summary)
        self.library.refresh()
        job = self.session.control.active_job
        if job and job.get("kind") == "multi":
            pending = [n for n in job.get("novels") or [] if not n.get("done")]
            if pending:
                self.resume_banner.show_job(job, self.session.cache)
        self._finish_worker_later()

    # ------------------------------------------------------------------
    # Library
    # ------------------------------------------------------------------

    def _start_library_check(self):
        entries = self.session.library_store.get_library()
        if not entries:
            QMessageBox.information(self, "Library", "No tracked novels yet.")
            return
        self.library.set_check_busy(True)
        for e in entries:
            self.library.check_status[e.source_url] = {"state": "checking"}
        self.library.refresh()
        worker = LibraryCheckWorker(self.session, entries)
        if not self._bind_and_run(
            worker,
            (worker.entry_done, self.library.apply_entry_status),
            (worker.progress, self._on_library_check_progress),
            (worker.finished, self._library_check_done),
        ):
            self.library.set_check_busy(False)
            self.progress.set_status("Busy — wait for the current job to finish")
            return

    @Slot(int, int, str)
    def _on_library_check_progress(self, idx: int, total: int, name: str):
        self.progress.set_status(f"Check [{idx + 1}/{total}]: {name[:40]}")

    @Slot(int, int)
    def _library_check_done(self, with_updates: int, total: int):
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
        self._finish_worker_later()

    def _start_library_update(self, entry):
        if self.session.control.is_downloading:
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

    @Slot(str)
    def _lib_update_ok(self, msg: str):
        self._set_downloading(False)
        self.progress.set_progress(1.0, "Library updated")
        QMessageBox.information(self, "Library Updated", msg)
        self.library.refresh()
        self._finish_worker_later()

    @Slot(str)
    def _lib_up_to_date(self, display: str):
        self._set_downloading(False)
        QMessageBox.information(self, "Up to date", f"No new chapters for:\n{display}")
        self._finish_worker_later()

    def _start_library_update_all(self):
        entries = [
            e for e in self.session.library_store.get_library()
            if (self.library.check_status.get(e.source_url) or {}).get("state") == "update"
        ]
        if not entries:
            QMessageBox.information(self, "Update All", "No novels with updates. Run Check updates first.")
            return
        if QMessageBox.question(
            self, "Update All",
            f"Update {len(entries)} novel(s)?",
        ) != QMessageBox.Yes:
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
        self._run_library_update_all(entries)

    def _run_library_update_all(self, entries):
        self.resume_banner.hide_banner()
        self._set_downloading(True)
        o = self.options.snapshot()
        worker = LibraryUpdateAllWorker(self.session, entries, o)
        if not self._bind_and_run(
            worker,
            (worker.progress, self._on_progress),
            (worker.finished_ok, self._lib_update_all_done),
        ):
            self._set_downloading(False)
            return

    @Slot(str)
    def _lib_update_all_done(self, summary: str):
        self._set_downloading(False)
        self.progress.set_progress(1.0, summary)
        QMessageBox.information(self, "Update All", summary)
        self.library.refresh()
        job = self.session.control.active_job
        if job and job.get("kind") == "library_update_all":
            pending = [e for e in job.get("entries") or [] if not e.get("done")]
            if pending:
                self.resume_banner.show_job(job, self.session.cache)
        self._finish_worker_later()

    def _open_library_url(self, url: str):
        self.tabs.setCurrentWidget(self.single)
        self.single.set_url(url)

    def _remove_library(self, url: str):
        entry = self.session.library_store.get_library_entry(url)
        title = ""
        if entry:
            title = entry.translated_title or entry.title or url
        drive_on = bool(self.library.drive_enabled.isChecked())
        extra = (
            "\n• the Google Drive EPUB and library.json entry "
            "(it will not come back on the next sync)"
            if drive_on
            else "\n• a sync marker so Google Drive cannot restore it later"
        )
        msg = (
            f'Remove “{title}” from your library?\n\n'
            "This deletes:\n"
            "• the local EPUB in your books folder\n"
            "• chapter, cover, and table-of-contents cache for this novel"
            f"{extra}"
        )
        if QMessageBox.question(self, "Remove", msg) != QMessageBox.Yes:
            return
        from core.library import purge_novel_artifacts
        from core.settings import get_default_books_dir

        extra_dirs = [get_default_books_dir()]
        custom = (self.session.output_dir or "").strip()
        if custom:
            extra_dirs.append(Path(custom))
        removed = self.session.library_store.remove_library(url)
        target = removed or entry
        if target:
            purge_novel_artifacts(
                target, cache=self.session.cache, extra_dirs=extra_dirs
            )
        self.library.refresh()
        if drive_on and self.session.drive_sync.is_connected():
            self._start_drive_sync(silent=True)

    def _download_library_epub(self, entry):
        # Prefer Drive download if remote id known
        from core.security import is_allowed_epub_path
        from core.settings import get_default_books_dir

        folder = downloads_folder(self.options.snapshot().get("output_dir", ""))
        roots = [get_default_books_dir(), folder]
        path = entry.output_path
        if path and Path(path).is_file() and is_allowed_epub_path(Path(path), roots):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
            return
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
                QMessageBox.information(self, "Download", f"Saved to:\n{dest}")
            except Exception as e:
                QMessageBox.warning(self, "Download EPUB", str(e))
        else:
            QMessageBox.information(self, "Download EPUB", "No local or Drive EPUB found for this entry.")

    def _reset_library(self):
        if QMessageBox.question(
            self, "Reset library",
            "Clear all tracked novels from your local library?\n\n"
            "They will not come back on Drive sync. Local EPUB files are kept.",
        ) != QMessageBox.Yes:
            return
        self.session.library_store.clear(clear_library=True, clear_history=False)
        self.library.check_status.clear()
        self.library.refresh()
        if self.library.drive_enabled.isChecked() and self.session.drive_sync.is_connected():
            self._start_drive_sync(silent=True)

    # ------------------------------------------------------------------
    # Drive
    # ------------------------------------------------------------------

    def _drive_connect(self):
        if not self.session.drive_sync.client_configured():
            QMessageBox.information(self, "Drive setup", oauth_setup_instructions())
            return
        self.progress.set_status("Connecting to Google Drive…")
        worker = DriveConnectWorker(self.session.drive_sync)
        if not self._bind_and_run(worker, (worker.finished, self._drive_connect_done)):
            self.progress.set_status("Busy — wait for the current job to finish")
            return

    @Slot(bool, str, str)
    def _drive_connect_done(self, ok: bool, email: str, err: str):
        if ok:
            self.library.drive_status.setText(f"Connected: {email}")
            self.progress.set_status(f"Drive connected: {email}")
            # Finish connect thread first, then start sync (don't kill sync with stop)
            self._finish_worker_later()
            QTimer.singleShot(100, lambda: self._start_drive_sync(silent=True))
        else:
            QMessageBox.critical(self, "Drive", err or "Connect failed")
            self._finish_worker_later()

    def _drive_disconnect(self):
        try:
            self.session.drive_sync.logout()
        except Exception:
            pass
        self.library.drive_status.setText("Disconnected")

    def _start_drive_sync(self, silent: bool = True):
        if not self.library.drive_enabled.isChecked():
            return
        if self._worker_busy and self._thread and self._thread.isRunning():
            self.progress.set_status("Sync already running…")
            return
        self._persist_settings()
        self._drive_sync_silent = silent
        self.library.set_drive_busy(True)
        self.progress.set_status("Syncing with Google Drive…")
        self.library.drive_status.setText("Syncing…")
        worker = DriveSyncWorker(self.session, silent=silent)
        if not self._bind_and_run(
            worker,
            (worker.progress, self._on_drive_sync_progress),
            (worker.finished, self._on_drive_sync_finished),
        ):
            self.library.set_drive_busy(False)
            self.progress.set_status("Busy — wait for the current job to finish")
            return

    @Slot(str)
    def _on_drive_sync_progress(self, msg: str):
        self.progress.set_status(msg)
        self.library.drive_status.setText(msg)

    @Slot(str, str)
    def _on_drive_sync_finished(self, summary: str, err: str):
        silent = getattr(self, "_drive_sync_silent", True)
        self.library.set_drive_busy(False)
        if err:
            self.library.drive_status.setText(f"Sync error: {err[:80]}")
            self.progress.set_status(f"Drive sync error: {err[:60]}")
            if not silent:
                QMessageBox.warning(self, "Drive sync", err)
        else:
            self.library.drive_status.setText(summary)
            # Drive pull only needs library.json — show All so Updates filter
            # doesn't hide every novel before Check updates has been run.
            try:
                self.session.library_store.reload()
            except Exception:
                pass
            n = len(self.session.library_store.get_library())
            self.library.show_all()
            self.tabs.setCurrentWidget(self.library)
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
                QMessageBox.information(self, "Drive sync", (summary or "Sync done") + extra)
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
                QMessageBox.warning(
                    self, "Drive folder",
                    detail + f"\n\nWarning: {info['error']}",
                )
            else:
                QMessageBox.information(self, "Drive folder", detail)
            self.library.drive_status.setText(
                f"{label} — {novels} novel(s), {epubs} EPUB(s) on Drive"
            )
            # Pull immediately so Library fills from this folder
            QTimer.singleShot(100, lambda: self._start_drive_sync(silent=False))
        except Exception as e:
            QMessageBox.critical(self, "Drive folder", str(e))

    def _drive_open_folder(self):
        link = ""
        try:
            link = self.session.drive_sync.folder_web_link() or ""
        except Exception:
            pass
        if link:
            QDesktopServices.openUrl(QUrl(link))
        else:
            QMessageBox.information(self, "Open folder", "Connect to Drive first.")

    def _drive_setup_help(self):
        QMessageBox.information(self, "Drive OAuth setup", oauth_setup_instructions())

    def _maybe_drive_push(self, path: str):
        if self.library.drive_enabled.isChecked():
            QTimer.singleShot(500, lambda: self._start_drive_sync(silent=True))

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _show_recent(self):
        history = self.session.library_store.get_history()
        if not history:
            QMessageBox.information(self, "Recent", "No download history yet.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Recent downloads")
        dlg.resize(520, 400)
        lay = QVBoxLayout(dlg)
        lst = QListWidget()
        for h in history:
            title = h.translated_title or h.title or h.source_url
            lst.addItem(f"{title}\n{h.source_url}")
        lay.addWidget(lst)
        buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        lay.addWidget(buttons)
        buttons.rejected.connect(dlg.reject)

        def accept():
            row = lst.currentRow()
            if row < 0:
                return
            self.single.set_url(history[row].source_url)
            self.tabs.setCurrentWidget(self.single)
            dlg.accept()

        buttons.accepted.connect(accept)
        lst.itemDoubleClicked.connect(lambda _: accept())
        dlg.exec()

    def _poll_clipboard(self):
        if not self.options.clipboard_cb.isChecked():
            return
        try:
            text = QApplication.clipboard().text() or ""
        except Exception:
            return
        if text == self._clipboard_last:
            return
        self._clipboard_last = text
        urls = [u for u in extract_urls(text) if looks_like_url(u) and u not in self._clipboard_seen]
        if not urls:
            return
        for u in urls:
            self._clipboard_seen.add(u)
        if self.tabs.currentWidget() is self.multi:
            self.multi.append_urls(urls)
            self.progress.set_status(f"Clipboard: queued {len(urls)} URL(s)")
        else:
            self.single.set_url(urls[0])
            self.progress.set_status("Clipboard: pasted URL into Single")

    def _open_books(self):
        p = downloads_folder(self.session.output_dir)
        p.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _open_data(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session.data_dir)))

    def _open_log(self):
        log = self.session.data_dir / "logs" / LOG_FILE_NAME
        if log.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log)))
        else:
            QMessageBox.information(self, "Log", f"No log yet at:\n{log}")

    def _translation_help(self):
        box = QMessageBox(self)
        box.setWindowTitle("How translation works")
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            "<p><b>Google</b> (default) — fast, free, online. Best for most novels.</p>"
            "<p><b>LibreTranslate</b> — your own server. More private, usually slower.</p>"
            "<p><b>Ollama</b> — full local translation. Slow (hours for a long novel). "
            "Needs <a href='https://ollama.com'>Ollama</a> installed and running.</p>"
            "<p><b>Polish English</b> — keep Google (or LibreTranslate) as the translator, "
            "then copy-edit the English with a local Ollama model. Much faster than "
            "translating with Ollama. If you already have a model, HuaEPUB uses it. "
            "If you have none, it asks before downloading about 2 GB.</p>"
            "<p>Workers apply to Google/LibreTranslate only. Polish runs separately.</p>"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        for lbl in box.findChildren(QLabel):
            lbl.setOpenExternalLinks(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    def _about(self):
        version = get_current_version()
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_TITLE}")
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<h3 style='margin-bottom:4px;'>{APP_TITLE} v{version}</h3>"
            f"<p>{APP_DESCRIPTION}</p>"
            "<p>Optional: polish Google English with a local Ollama model "
            "(Help → How translation works).</p>"
            "<p>"
            f"<b>Developer:</b> {APP_AUTHOR} "
            f"(<a href='https://github.com/{APP_AUTHOR_HANDLE}'>@{APP_AUTHOR_HANDLE}</a>)<br>"
            f"<b>Repository:</b> "
            f"<a href='{APP_REPO_URL}'>{APP_REPO_URL.replace('https://', '')}</a><br>"
            f"<b>License:</b> {APP_LICENSE}<br>"
            "<b>UI:</b> PySide6 (Qt)<br>"
            "<b>Data folder:</b> ~/.huaepub/"
            "</p>"
            "<p style='color:#aaa;font-size:11px;'>"
            "Inspired by "
            "<a href='https://github.com/dteviot/WebToEpub'>WebToEpub</a> "
            "(dteviot), which this project started from, and by fixTranslate.py.<br>"
            "Not affiliated with novel sites or Google."
            "</p>"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        for lbl in box.findChildren(QLabel):
            lbl.setOpenExternalLinks(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    def _auto_check_updates(self):
        check_for_updates_async(callback=self._update_check_cb)

    def _manual_check_updates(self):
        self.progress.set_status("Checking for app updates…")
        check_for_updates_async(callback=self._update_check_cb)

    def _update_check_cb(self, has_update, latest, message):
        # Runs on a plain threading.Thread — never touch Qt widgets here.
        self._sig_update_check.emit(
            bool(has_update),
            str(latest or ""),
            str(message or ""),
        )

    @Slot(str)
    def _set_status_safe(self, text: str):
        self.progress.set_status(text)

    @Slot(bool, str, str)
    def _on_update_check_ready(self, has_update: bool, latest: str, message: str):
        if has_update:
            self.progress.set_status(f"Update available: {latest}")
            if QMessageBox.question(
                self, "Update available",
                f"{message}\n\nDownload and install?",
            ) == QMessageBox.Yes:
                self.progress.set_status("Downloading update…")
                download_update_async(
                    progress_callback=lambda _c, _t, s: self._sig_status.emit(
                        s or "Downloading update…"
                    ),
                    completion_callback=lambda ok, msg: self._sig_update_done.emit(
                        bool(ok), str(msg or "")
                    ),
                )
        else:
            self.progress.set_status(message or "App is up to date")

    @Slot(bool, str)
    def _on_update_download_done(self, ok: bool, message: str):
        if ok:
            self.progress.set_status("Update ready — closing to apply…")
            QMessageBox.information(
                self,
                "Update ready",
                message
                or "Update installed.\nThe application will now close and reopen.",
            )
            # Helper waits for this PID to exit before swapping the binary.
            QApplication.instance().quit()
            return
        self.progress.set_status(message or "Update failed")
        QMessageBox.warning(
            self, "Update failed", message or "Update failed."
        )
