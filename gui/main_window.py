# Author: joelsnl and Anthropic Claude
"""Main Qt window: modes, workers, pause/resume, and menus."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)

from core.branding import (
    APP_TITLE, LOG_FILE_NAME,
)
from core.download_job import (
    book_job, multi_job, save_job,
)
from core.download_runner import (
    completion_dialog_title, downloads_folder, format_completion_notes,
    library_epub_path, translator_backend_kwargs,
)
from core.logger import setup_logging
from core.settings import save_settings
from core.parser import cleanup_browser, create_http_session
from core.updater import (
    get_auto_check_updates,
    get_current_version, set_auto_check_updates,
)
from core.utils import extract_urls, format_ratio, looks_like_url, sanitize_runtime_env

from gui import theme
from gui.help_content import TRANSLATION_HELP_HTML, about_html
from gui.icon import apply_app_icon, load_app_pixmap
from gui.dialogs import (
    busy_message, pick_recent_download, show_cache_dialog,
    show_error, show_info, show_info_with_preview, show_rich_info, show_warning,
)
from gui.pages.library_page import LibraryPage
from gui.pages.multi_page import MultiPage
from gui.pages.reader_page import ReaderPage
from gui.pages.single_page import SinglePage
from gui.session import AppSession
from gui.widgets.options_bar import OptionsBar
from gui.widgets.progress_panel import ProgressPanel
from gui.widgets.resume_banner import ResumeBanner
from gui.workers.download_worker import MultiDownloadWorker, SingleDownloadWorker
from gui.workers.fetch_worker import FetchWorker
from gui.window.glossary_actions import GlossaryActionsMixin
from gui.window.library_actions import LibraryActionsMixin
from gui.window.look_actions import LookActionsMixin
from gui.window.reader_actions import ReaderActionsMixin
from gui.window.resume_actions import ResumeActionsMixin
from gui.window.server_actions import ServerActionsMixin
from gui.window.update_actions import UpdateActionsMixin
from gui.window.worker_host import WorkerHostMixin

import parsers  # noqa: F401 - registers the site parsers


class MainWindow(
    WorkerHostMixin,
    GlossaryActionsMixin,
    ResumeActionsMixin,
    UpdateActionsMixin,
    ReaderActionsMixin,
    LibraryActionsMixin,
    ServerActionsMixin,
    LookActionsMixin,
    QMainWindow,
):
    # Cross-thread marshaling for plain threading.Thread callbacks (updater, etc.)
    _sig_update_check = Signal(bool, str, str)
    _sig_update_done = Signal(bool, str)
    _sig_update_progress = Signal(int, int, str)
    _sig_status = Signal(str)

    def __init__(self):
        super().__init__()
        sanitize_runtime_env()
        self.session = AppSession()
        setup_logging(self.session.data_dir)
        self.setWindowTitle(f"{APP_TITLE} v{get_current_version()}")
        apply_app_icon(QApplication.instance(), self)
        self.resize(960, 720)
        self.setMinimumSize(800, 600)

        self._thread: QThread | None = None
        self._worker = None
        self._worker_busy = False
        self._worker_epoch = 0
        self._check_thread: QThread | None = None
        self._check_worker = None
        self._check_busy = False
        self._exiting_for_update = False
        self._app_update_checking = False
        self._app_update_pending = False
        self._app_update_installing = False
        self._update_progress_dlg = None
        self._update_check_notify = False
        self._last_app_update_check = None
        self._clipboard_last = ""
        self._clipboard_seen = set()
        self._http = create_http_session()
        self._reader_return = None
        self._pending_reader_entry = None
        self._reader_last_fetch = 0.0
        self._reader_open_gen = 0
        self._glossary_qwen_dlg = None

        self._sig_update_check.connect(
            self._on_update_check_ready, Qt.ConnectionType.QueuedConnection
        )
        self._sig_update_done.connect(
            self._on_update_download_done, Qt.ConnectionType.QueuedConnection
        )
        self._sig_update_progress.connect(
            self._on_update_progress, Qt.ConnectionType.QueuedConnection
        )
        self._sig_status.connect(
            self._set_status_safe, Qt.ConnectionType.QueuedConnection
        )

        # Page 0: the desktop tabs. Page 1: the server screen (server mode).
        stack = QStackedWidget()
        self.setCentralWidget(stack)
        central = QWidget()
        stack.addWidget(central)
        layout = QVBoxLayout(central)

        self.resume_banner = ResumeBanner()
        self.resume_banner.resume_clicked.connect(self._on_resume_job)
        self.resume_banner.discard_clicked.connect(self._on_discard_job)
        layout.addWidget(self.resume_banner)

        self.tabs = QTabWidget()
        self.single = SinglePage()
        self.multi = MultiPage()
        self.library = LibraryPage(self.session)
        self.reader = ReaderPage()
        self.tabs.addTab(self.single, "Single")
        self.tabs.addTab(self.multi, "Multi")
        self.tabs.addTab(self.library, "Library")
        self.tabs.addTab(self.reader, "Read")
        layout.addWidget(self.tabs, 1)

        self.options = OptionsBar(self.session)
        layout.addWidget(self.options)

        self.progress = ProgressPanel()
        layout.addWidget(self.progress)

        self._build_server_ui(stack)
        self._build_menu()
        self._wire()
        self._restore_window_geometry()

        if self.session.settings.get("server_enabled"):
            QTimer.singleShot(0, self._maybe_start_server_on_launch)
        QTimer.singleShot(400, self._check_resume_job)
        if get_auto_check_updates():
            # After the startup burst below (Library covers at 4 s, glossary GPU probe
            # at 3.5 s): fired inside the Update prompt, they froze its Yes button.
            QTimer.singleShot(5000, self._auto_check_updates)
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.timeout.connect(self._poll_clipboard)
        self._clipboard_timer.start(3000)
        if self.session.library_store.get_library():
            QTimer.singleShot(4000, self.library.refresh)
        QTimer.singleShot(3500, self._maybe_offer_glossary_qwen)

    def _build_menu(self):
        mb = self.menuBar()
        file_m = mb.addMenu("File")
        file_m.addAction("Open books folder", self._open_books)
        file_m.addAction("Open data folder", self._open_data)
        file_m.addAction("Open log file", self._open_log)
        file_m.addSeparator()
        server_act = file_m.addAction("Server mode…", self._open_server_dialog)
        file_m.addSeparator()
        file_m.addAction("Exit", self.close)

        view_m = mb.addMenu("View")
        self._build_look_menu(view_m)

        lib_m = mb.addMenu("Library")
        lib_m.addAction("Check for updates", lambda: self.library.check_requested.emit())
        lib_m.addAction("Reset library…", self._reset_library)

        help_m = mb.addMenu("Help")
        help_m.addAction("Check for updates", self._manual_check_updates)
        act = QAction("Auto-check updates on startup", self, checkable=True)
        act.setChecked(bool(get_auto_check_updates()))
        act.toggled.connect(set_auto_check_updates)
        help_m.addAction(act)
        help_m.addAction("How translation works…", self._translation_help)
        glossary_act = help_m.addAction("Polish glossaries with Qwen…", self._menu_glossary_qwen)
        cache_act = help_m.addAction("Cache…", self._cache_dialog)
        help_m.addAction("About", self._about)
        # Off while serving: the browser owns jobs, the library and the cache then.
        self._server_locked_actions = [server_act, lib_m.menuAction(), glossary_act, cache_act]

    def _wire(self):
        self.single.fetch_requested.connect(self._start_fetch)
        self.single.recent_requested.connect(self._show_recent)
        self.single.read_requested.connect(self._open_reader_from_single)
        self.progress.download_clicked.connect(self._start_single_download)
        self.progress.pause_clicked.connect(self._toggle_pause)
        self.progress.cancel_clicked.connect(self._cancel_download)

        self.multi.fetch_all_requested.connect(self._start_multi_fetch)
        self.multi.download_all_requested.connect(self._start_multi_download)

        self.library.check_requested.connect(self._start_library_check)
        self.library.update_all_requested.connect(self._start_library_update_all)
        self.library.update_selected.connect(self._start_library_update)
        self.library.open_selected.connect(self._open_library_url)
        self.library.read_selected.connect(self._open_reader_from_library)
        self.library.remove_selected.connect(self._remove_library)
        self.library.refresh_requested.connect(self.library.refresh)
        self.library.view_changed.connect(lambda v: self._persist_settings())
        self.library.filter_changed.connect(lambda v: self._persist_settings())
        self.library.download_epub_selected.connect(self._download_library_epub)
        self.reader.back_requested.connect(self._close_reader)
        self.reader.chapter_requested.connect(self._on_reader_chapter)
        self.reader.font_changed.connect(self._on_reader_font)
        self.reader.prefs_changed.connect(self._on_reader_prefs)
        self.reader.bookmark_requested.connect(self._on_reader_bookmark)
        self.reader.place_changed.connect(self._save_reader_position)
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
            translation_glossary=o.get("glossary", "off"),
            ollama_model=o.get("ollama_model", "qwen2.5:3b"),
            ollama_url=o.get("ollama_url", "http://127.0.0.1:11434"),
            ollama_polish=bool(o.get("ollama_polish", False)),
            library_view=self.library._view,
            library_filter=self.library._filter,
        )

    @Slot(str)
    def _on_glossary_qwen_progress(self, status: str):
        GlossaryActionsMixin._on_glossary_qwen_progress(self, status)

    @Slot(object)
    def _on_glossary_qwen_ok(self, payload):
        GlossaryActionsMixin._on_glossary_qwen_ok(self, payload)

    @Slot(str)
    def _on_glossary_qwen_error(self, message: str):
        GlossaryActionsMixin._on_glossary_qwen_error(self, message)

    def _restore_window_geometry(self):
        s = self.session.settings
        try:
            w = int(s.get("window_w") or 0)
            h = int(s.get("window_h") or 0)
            x = int(s.get("window_x") or 0)
            y = int(s.get("window_y") or 0)
        except (TypeError, ValueError):
            return
        if w < self.minimumWidth() or h < self.minimumHeight():
            return
        geo = QRect(x, y, w, h)
        screens = QGuiApplication.screens()
        if screens and not any(scr.availableGeometry().intersects(geo) for scr in screens):
            ag = screens[0].availableGeometry()
            self.resize(min(w, ag.width()), min(h, ag.height()))
            return
        self.setGeometry(geo)

    def _save_window_geometry(self):
        geo = self.normalGeometry()
        self.session.settings["window_x"] = int(geo.x())
        self.session.settings["window_y"] = int(geo.y())
        self.session.settings["window_w"] = int(geo.width())
        self.session.settings["window_h"] = int(geo.height())
        save_settings(self.session.settings)

    def closeEvent(self, event):
        if getattr(self, "_headless_handoff", False):
            self._close_keeping_server(event)
            return
        update_exit = bool(getattr(self, "_exiting_for_update", False))
        if getattr(self, "_app_update_installing", False) and not update_exit:
            event.ignore()
            return
        self._finalize_close(event)

    def _finalize_close(self, event):
        self._shutdown_server_for_close()
        self._save_reader_position()
        self._persist_settings()
        self._save_window_geometry()
        worker = self._worker
        if worker is not None and hasattr(worker, "request_cancel"):
            try:
                worker.request_cancel()
            except Exception:
                pass
        downloading = bool(self.session.control.is_downloading)
        update_exit = bool(getattr(self, "_exiting_for_update", False))
        if downloading and not update_exit:
            try:
                self.session.control.request_cancel()
            except Exception:
                pass
            wait_ms = 15000
        elif update_exit:
            wait_ms = 200
        else:
            wait_ms = 5000
        try:
            cleanup_browser()
        except Exception:
            pass
        self.session.close()
        self._stop_reader_side(wait_ms=min(wait_ms, 2000))
        self._stop_thread(wait_ms=wait_ms)
        self._stop_check_thread(wait_ms=min(wait_ms, 5000))
        event.accept()


    # ------------------------------------------------------------------
    # Single
    # ------------------------------------------------------------------

    @Slot(str)
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
            (worker.status, self._set_status_safe),
            (worker.error, self._fetch_error),
            (worker.finished, self._fetch_done),
        ):
            self.single.set_fetch_enabled(True)
            self.progress.set_status(busy_message("look up this novel"))
            return

    @Slot(str)
    def _fetch_error(self, msg: str):
        self.single.set_fetch_enabled(True)
        show_error(self, "Fetch failed", msg)
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
        if self._worker_busy or self.session.control.is_downloading:
            self.progress.set_status(busy_message("download this book"))
            return
        if not self.single.novel_info or not self.single.chapters:
            return
        selected = self.single.selected_chapters()
        if not selected:
            show_warning(self, "Warning", "Select at least one chapter")
            return
        self._persist_settings()
        o = self.options.snapshot()
        info = self.single.novel_info
        out = library_epub_path(
            self.session.library_store, self.single.translated_title or info.title,
            info.source_url, o.get("output_dir", ""),
        )
        job = book_job("single", info, selected, out, o,
                       translated_title=self.single.translated_title or "")
        self._begin_single_download(
            self.single.parser, self.single.novel_info, selected, out,
            self.single.translated_title, job,
        )

    @Slot(float, str)
    @Slot(int, str)
    def _on_progress(self, fraction: float, status: str):
        """On MainWindow so QueuedConnection is a real QObject slot, not a mixin."""
        WorkerHostMixin._on_progress(self, fraction, status)

    @Slot(int, int, str)
    def _on_library_check_progress(self, idx: int, total: int, name: str):
        LibraryActionsMixin._on_library_check_progress(self, idx, total, name)

    @Slot(str, object)
    def _on_library_entry_status(self, url: str, st: object):
        LibraryActionsMixin._on_library_entry_status(self, url, st)

    @Slot(int, int)
    def _library_check_done(self, with_updates: int, total: int):
        LibraryActionsMixin._library_check_done(self, with_updates, total)

    @Slot(str)
    def _lib_update_ok(self, msg: str):
        LibraryActionsMixin._lib_update_ok(self, msg)

    @Slot(str)
    def _lib_up_to_date(self, display: str):
        LibraryActionsMixin._lib_up_to_date(self, display)

    @Slot(str)
    def _lib_update_all_done(self, summary: str):
        LibraryActionsMixin._lib_update_all_done(self, summary)

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

    @Slot(str, list, list, bool, list)
    def _single_done(self, path: str, failed: list, warnings: list = None, polish_cancelled: bool = False, heuristic: list = None):
        self._set_downloading(False)
        self.progress.set_progress(1.0, f"Done! Saved to: {path}")
        notes = format_completion_notes(
            failed, warnings or [], polish_cancelled, heuristic or [],
        )
        self.progress.mark_finished(translated=bool(self.options.snapshot().get("translate")),
                                    flagged=bool(notes))
        msg = f"EPUB saved to:\n{path}"
        if notes:
            msg += "\n\n" + notes
        title = "Saved with warnings" if notes else "Success"
        want_preview = False
        if path and Path(path).is_file():
            want_preview = show_info_with_preview(self, title, msg)
        else:
            show_info(self, title, msg)
        self.library.refresh()
        self._finish_worker_later()
        if want_preview:
            info = self.single.novel_info
            self._preview_downloaded_epub(
                path=path,
                source_url=(info.source_url if info else "") or "",
                title=self.single.translated_title or ((info.title if info else "") or ""),
                extra_chapters=self.single.chapters,
            )

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
        show_error(self, "Download failed", msg)
        self._finish_worker_later()

    # ------------------------------------------------------------------
    # Multi
    # ------------------------------------------------------------------

    def _start_multi_fetch(self):
        urls = self.multi.get_urls()
        if not urls:
            show_warning(self, "Multi", "Paste at least one URL")
            return
        self.multi.begin_fetch(urls)
        self.progress.set_status(f"Fetching {format_ratio(0, len(urls))}…")
        self.multi.set_busy(True)
        self._multi_fetch_urls = urls
        self._multi_fetch_i = 0
        self._multi_fetch_next()

    def _multi_fetch_next(self):
        urls = getattr(self, "_multi_fetch_urls", [])
        i = getattr(self, "_multi_fetch_i", 0)
        if i >= len(urls):
            self.multi.set_busy(False)
            self.progress.set_status(
                f"Fetched {format_ratio(len(self.multi.fetched_novels()), len(urls))}"
            )
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
            (worker.status, self._set_status_safe),
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
        if self._worker_busy or self.session.control.is_downloading:
            self.progress.set_status(busy_message("download these books"))
            return
        novels = self.multi.fetched_novels()
        if not novels:
            return
        self._persist_settings()
        job = multi_job(novels, self.options.snapshot())
        self.session.control.active_job = job
        save_job(job, self.session.data_dir)
        self._start_multi_download_with(novels)

    def _start_multi_download_with(self, novels):
        self.resume_banner.hide_banner()
        self._set_downloading(True)
        for i in range(len(novels)):
            self.multi.set_status(i, "Queued")
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

    @Slot(str, list)
    def _multi_done(self, summary: str, previews: list = None):
        self._set_downloading(False)
        self.progress.set_progress(1.0, "Multi-download complete")
        self.progress.mark_finished(translated=bool(self.options.snapshot().get("translate")),
                                    flagged="warning" in (summary or "").lower())
        books = [
            p for p in (previews or [])
            if isinstance(p, dict) and p.get("path") and Path(p["path"]).is_file()
        ]
        title = completion_dialog_title(summary, "Multi-download complete")
        want_preview = False
        if books:
            want_preview = show_info_with_preview(self, title, summary)
        else:
            show_info(self, title, summary)
        self.library.refresh()
        job = self.session.control.active_job
        if job and job.get("kind") == "multi":
            pending = [n for n in job.get("novels") or [] if not n.get("done")]
            if pending:
                self.resume_banner.show_job(job, self.session.cache)
        self._finish_worker_later()
        if want_preview:
            self._preview_multi_epubs(books)

    # ------------------------------------------------------------------
    # Library
    # ------------------------------------------------------------------

    def _show_recent(self):
        url = pick_recent_download(self, self.session.library_store.get_history())
        if url:
            self.single.set_url(url)
            self.tabs.setCurrentWidget(self.single)

    def _poll_clipboard(self):
        if not self.options.clipboard_cb.isChecked() or self._serving():
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
            show_info(self, "Log", f"No log yet at:\n{log}")

    def _cache_dialog(self):
        show_cache_dialog(
            self,
            self.session.cache,
            self.session.settings,
            self.progress.set_status,
        )

    def _translation_help(self):
        show_rich_info(self, "How translation works", TRANSLATION_HELP_HTML)

    def _about(self):
        logo = load_app_pixmap(64)
        show_rich_info(
            self,
            f"About {APP_TITLE}",
            about_html(get_current_version(), theme.current().muted),
            icon_pixmap=None if logo.isNull() else logo,
        )

    @Slot(str)
    def _set_status_safe(self, text: str):
        if QThread.currentThread() != self.thread():
            self._call_on_gui(lambda t=text: self._set_status_safe(t))
            return
        self.progress.set_status(text)

    @Slot(bool, str, str)
    def _on_update_check_ready(self, has_update: bool, latest: str, message: str):
        UpdateActionsMixin._on_update_check_ready(self, has_update, latest, message)

    @Slot(int, int, str)
    def _on_update_progress(self, current: int, total: int, text: str):
        UpdateActionsMixin._on_update_progress(self, current, total, text)

    @Slot(bool, str)
    def _on_update_download_done(self, ok: bool, message: str):
        UpdateActionsMixin._on_update_download_done(self, ok, message)

    @Slot()
    def _restart_for_update(self):
        UpdateActionsMixin._restart_for_update(self)
