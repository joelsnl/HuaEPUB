# Author: joelsnl and Anthropic Claude
"""Main Qt window: modes, workers, pause/resume, menus, Drive auto-sync."""

from __future__ import annotations

import os
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QRect, QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QProgressDialog,
    QTabWidget, QVBoxLayout, QWidget,
)

from core.branding import (
    APP_AUTHOR, APP_AUTHOR_HANDLE, APP_DESCRIPTION, APP_LICENSE,
    APP_REPO_URL, APP_TITLE, LOG_FILE_NAME,
)
from core.download_job import (
    chapters_from_job, chapters_to_job, clear_job, load_job,
    novel_info_from_job, novel_info_to_job, save_job,
)
from core.download_runner import (
    completion_dialog_title, downloads_folder, epub_path, format_completion_notes,
    translator_backend_kwargs,
)
from core.logger import setup_logging
from core.settings import save_settings, set_setting
from core.parser import cleanup_browser, create_http_session, get_parser_for_url
from core.updater import (
    check_for_updates_async, download_update_async, get_auto_check_updates,
    get_current_version, set_auto_check_updates,
)
from core.utils import extract_urls, looks_like_url, sanitize_runtime_env

from gui.dialogs import (
    ask_accept_glossary_proposals, ask_yes_no, ask_yes_not_now_dont_ask,
    pick_recent_download, show_cache_dialog, show_error, show_info,
    show_info_with_preview, show_rich_info, show_warning,
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
from gui.workers.glossary_worker import GlossaryQwenWorker
from gui.window.drive_actions import DriveActionsMixin
from gui.window.library_actions import LibraryActionsMixin
from gui.window.reader_actions import ReaderActionsMixin
from gui.window.worker_host import WorkerHostMixin

import parsers  # noqa: F401 — register site parsers


class MainWindow(
    WorkerHostMixin,
    ReaderActionsMixin,
    DriveActionsMixin,
    LibraryActionsMixin,
    QMainWindow,
):
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
        self._worker_epoch = 0
        self._check_thread: QThread | None = None
        self._check_worker = None
        self._check_busy = False
        self._pending_drive_sync = False
        self._drive_sync_silent = True
        self._exiting_for_update = False
        self._app_update_checking = False
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

        self._build_menu()
        self._wire()
        self._restore_window_geometry()

        QTimer.singleShot(400, self._check_resume_job)
        if get_auto_check_updates():
            QTimer.singleShot(2000, self._auto_check_updates)
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.timeout.connect(self._poll_clipboard)
        self._clipboard_timer.start(3000)
        if self.session.settings.get("drive_sync_enabled"):
            QTimer.singleShot(2500, self._start_drive_sync_silent)
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
        file_m.addAction("Exit", self.close)

        lib_m = mb.addMenu("Library")
        lib_m.addAction("Check for updates", lambda: self.library.check_requested.emit())
        lib_m.addAction("Sync Drive now", self._drive_sync_now)
        lib_m.addAction("Reset library…", self._reset_library)

        help_m = mb.addMenu("Help")
        help_m.addAction("Check for updates", self._manual_check_updates)
        act = QAction("Auto-check updates on startup", self, checkable=True)
        act.setChecked(bool(get_auto_check_updates()))
        act.toggled.connect(set_auto_check_updates)
        help_m.addAction(act)
        help_m.addAction("How translation works…", self._translation_help)
        help_m.addAction("Polish glossaries with Qwen…", self._menu_glossary_qwen)
        help_m.addAction("Cache…", self._cache_dialog)
        help_m.addAction("About", self._about)
        help_m.addAction("Drive OAuth setup…", self._drive_setup_help)

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
        self.library.drive_connect.connect(self._drive_connect)
        self.library.drive_sync.connect(self._drive_sync_now)
        self.library.drive_disconnect.connect(self._drive_disconnect)
        self.library.drive_change_folder.connect(self._drive_change_folder)
        self.library.drive_open_folder.connect(self._drive_open_folder)
        self.library.view_changed.connect(lambda v: self._persist_settings())
        self.library.filter_changed.connect(lambda v: self._persist_settings())
        self.library.download_epub_selected.connect(self._download_library_epub)
        self.reader.back_requested.connect(self._close_reader)
        self.reader.chapter_requested.connect(self._on_reader_chapter)
        self.reader.font_changed.connect(self._on_reader_font)
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
            translation_glossary=o.get("glossary", "auto"),
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

    _GLOSSARY_QWEN_PROMPT = (
        "Use the local Qwen model (same llama.cpp GGUF as Polish English) to "
        "classify names and domain terms found in your books.\n\n"
        "This dialog stays in front until the pass finishes — you will not be "
        "able to download while it runs. The polish GGUF must already be on "
        "disk (this will not start a 2–9 GB download).\n\n"
        "You will get a list to Accept all or Discard. "
        "Everyday Chinese is not added (this is not a general dictionary)."
    )

    def _library_glossary_books(self) -> list[dict]:
        books = []
        for entry in self.session.library_store.get_library():
            name = (entry.title or entry.translated_title or "").strip()
            if not name:
                continue
            books.append({
                "title": name,
                "source_url": entry.source_url or "",
                "description": getattr(entry, "description", "") or "",
            })
        return books

    def _maybe_offer_glossary_qwen(self):
        if self._worker_busy or self.session.control.is_downloading:
            return
        if load_job(self.session.data_dir):
            return
        from core.translation.qwen_glossary import (
            has_harvested_terms,
            polish_gguf_on_disk,
            qwen_glossary_capable,
            should_offer_glossary_qwen,
        )

        if not should_offer_glossary_qwen(
            self.session.settings,
            has_library=bool(self.session.library_store.get_library()),
            has_harvested=has_harvested_terms(),
            model_ready=polish_gguf_on_disk(),
            qwen_capable=qwen_glossary_capable(),
        ):
            return
        choice = ask_yes_not_now_dont_ask(
            self,
            "Polish glossaries with Qwen?",
            self._GLOSSARY_QWEN_PROMPT,
        )
        if choice == "later":
            return
        if choice == "never":
            self.session.settings["glossary_qwen_ask"] = False
            set_setting("glossary_qwen_ask", False)
            return
        self._run_glossary_qwen_modal()

    def _menu_glossary_qwen(self):
        if self._worker_busy or self.session.control.is_downloading:
            show_warning(self, "Busy", "Wait for the current job to finish.")
            return
        from core.translation.qwen_glossary import (
            polish_gguf_on_disk,
            qwen_glossary_capable,
        )

        if not polish_gguf_on_disk():
            show_warning(
                self,
                "Glossary · Qwen",
                "The polish GGUF is not on disk yet. Tick Polish English once "
                "so llama.cpp can download it, then run this again. "
                "Glossary Qwen will not start a 2–9 GB download by itself.",
            )
            return
        if not qwen_glossary_capable():
            show_warning(
                self,
                "Glossary · Qwen",
                "This PC is on a 3B polish profile. Glossary classification "
                "needs the 7B or 14B Qwen GGUF. Names are still romanized "
                "with pinyin during translate.",
            )
            return
        if not ask_yes_no(self, "Polish glossaries with Qwen?", self._GLOSSARY_QWEN_PROMPT):
            return
        self._run_glossary_qwen_modal()

    def _run_glossary_qwen_modal(self):
        dlg = QProgressDialog(
            "Starting local Qwen…", "Cancel", 0, 0, self
        )
        dlg.setWindowTitle("Glossary · Qwen")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        self._glossary_qwen_dlg = dlg
        worker = GlossaryQwenWorker(
            self._library_glossary_books(),
            cache=self.session.cache,
        )
        dlg.canceled.connect(worker.request_cancel)
        if not self._bind_and_run(
            worker,
            (worker.progress, self._on_glossary_qwen_progress),
            (worker.finished_ok, self._on_glossary_qwen_ok),
            (worker.finished_error, self._on_glossary_qwen_error),
        ):
            dlg.close()
            self._glossary_qwen_dlg = None
            show_warning(self, "Busy", "Wait for the current job to finish.")
            return
        dlg.show()

    @Slot(str)
    def _on_glossary_qwen_progress(self, status: str):
        dlg = self._glossary_qwen_dlg
        if dlg is not None and status:
            dlg.setLabelText(status)

    def _close_glossary_qwen_dlg(self):
        dlg = self._glossary_qwen_dlg
        self._glossary_qwen_dlg = None
        if dlg is not None:
            dlg.close()

    @Slot(object)
    def _on_glossary_qwen_ok(self, payload):
        self._close_glossary_qwen_dlg()
        self._finish_worker_later()
        now = time.time()
        self.session.settings["glossary_qwen_last_at"] = now
        set_setting("glossary_qwen_last_at", now)
        if isinstance(payload, dict):
            message = str(payload.get("message") or "Done.")
            proposals = list(payload.get("proposals") or [])
        else:
            message = str(payload or "Done.")
            proposals = []
        if proposals:
            from core.translation.qwen_glossary import apply_glossary_proposals

            if ask_accept_glossary_proposals(self, proposals):
                added, updated = apply_glossary_proposals(proposals)
                show_info(
                    self,
                    "Glossaries updated",
                    f"Accepted {added + updated} term(s). {message}",
                )
                return
            show_info(self, "Glossary polish", "Discarded. Nothing was written.")
            return
        show_info(self, "Glossaries updated", message)

    @Slot(str)
    def _on_glossary_qwen_error(self, message: str):
        self._close_glossary_qwen_dlg()
        self._finish_worker_later()
        if "cancel" in (message or "").casefold():
            show_info(self, "Glossary polish", message)
            return
        show_error(self, "Glossary polish failed", message)

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
        self._pending_drive_sync = False
        self._stop_thread(drain_pending_sync=False, wait_ms=wait_ms)
        self._stop_check_thread(wait_ms=min(wait_ms, 5000), drain_pending_sync=False)
        event.accept()

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
        if not ask_yes_no(
            self, "Discard",
            "Remove the saved resume point?\nCached chapter text stays on this PC.",
        ):
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
                show_warning(self, "Resume", f"Unknown job type: {kind}")
                clear_job(self.session.data_dir)
        except Exception as e:
            traceback.print_exc()
            show_error(self, "Resume failed", str(e))

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
            self.progress.set_status("Busy — wait for the current job to finish")
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
            self.progress.set_status("Busy — wait for the current job to finish")
            return
        if not self.single.novel_info or not self.single.chapters:
            return
        selected = self.single.selected_chapters()
        if not selected:
            show_warning(self, "Warning", "Select at least one chapter")
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

    @Slot(float, str)
    @Slot(int, str)
    def _on_progress(self, fraction: float, status: str):
        """On MainWindow so QueuedConnection is a real QObject slot, not a mixin."""
        WorkerHostMixin._on_progress(self, fraction, status)

    @Slot()
    def _start_drive_sync_silent(self):
        DriveActionsMixin._start_drive_sync_silent(self)

    @Slot()
    def _drive_sync_now(self):
        DriveActionsMixin._drive_sync_now(self)

    @Slot(bool, str, str)
    def _drive_connect_done(self, ok: bool, email: str, err: str):
        DriveActionsMixin._drive_connect_done(self, ok, email, err)

    @Slot(str)
    def _on_drive_sync_progress(self, msg: str):
        DriveActionsMixin._on_drive_sync_progress(self, msg)

    @Slot(str, str)
    def _on_drive_sync_finished(self, summary: str, err: str):
        DriveActionsMixin._on_drive_sync_finished(self, summary, err)

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
            self.progress.set_status("Busy — wait for the current job to finish")
            return
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
            show_info(self, "Log", f"No log yet at:\n{log}")

    def _cache_dialog(self):
        show_cache_dialog(
            self,
            self.session.cache,
            self.session.settings,
            self.progress.set_status,
        )

    def _translation_help(self):
        show_rich_info(
            self,
            "How translation works",
            "<p><b>Google (New)</b> (default) — the same <code>translate-pa</code> "
            "engine as Calibre Ebook Translator 2.4+ <i>Google (Free) - New</i>. "
            "Use this first. <b>Google (HTML)</b> is the widget HTML API. "
            "<b>Google (Old)</b> is <code>client=gtx</code>, which Google walled "
            "for many IPs in 2026.</p>"
            "<p><b>Microsoft Edge</b> — another free unofficial engine "
            "(same as the Calibre plugin). No API key.</p>"
            "<p><b>LibreTranslate</b> — your own server. More private, usually slower.</p>"
            "<p><b>Ollama</b> — full local translation. Slow (hours for a long novel). "
            "Needs <a href='https://ollama.com'>Ollama</a> installed and running.</p>"
            "<p><b>Offline NMT</b> — local CTranslate2 (opus-mt-zh-en). Free and offline. "
            "Needs <code>pip install -r requirements-nmt.txt</code> (not in the exe). "
            "First run downloads ~320&nbsp;MB into ~/.huaepub/nmt. "
            "Glossary is <b>Auto</b> by default: the built-in xianxia/wuxia list "
            "is used only when the title or chapter list looks like cultivation "
            "(not for urban/romance). That list is a curated web-novel pack, "
            "not a general Chinese dictionary. "
            "While translating, HuaEPUB also learns character names from this book "
            "into <code>~/.huaepub/glossaries/&lt;title&gt;.json</code> (pinyin, not Google). "
            "If the polish Qwen GGUF is already on disk (7B+), a classify pass can "
            "fix those names and lock sects/techniques that appear in the text. "
            "Help → Polish glossaries with Qwen… runs it anytime and shows Accept all / Discard. "
            "It will not download a GGUF by itself. "
            "Your names in <code>~/.huaepub/glossary.json</code> always apply unless "
            "Glossary is Off. Force the pack with <b>Cultivation pack</b>.</p>"
            "<p><b>Offline NMT GPU</b> — your NVIDIA GPU is used only when "
            "<b>CUDA 12</b> libraries are visible (<code>cublas64_12.dll</code>). "
            "The Game Ready driver is not enough. "
            "<code>nvidia-cublas-cu12</code> and <code>nvidia-cuda-runtime-cu12</code> "
            "are in requirements-nmt.txt. "
            "Do <b>not</b> install CUDA 13 for this. cuDNN is not required. "
            "Then fully quit and reopen the app. "
            "If CUDA 12 still cannot load, Offline NMT stays on CPU (not Google) "
            "and the log prints the same install steps.</p>"
            "<p><b>Polish English</b> — keep Google (or LibreTranslate) as the translator, "
            "then copy-edit awkward English on this PC. <b>Ollama is not required.</b> "
            "The first run downloads llama.cpp and a Qwen2.5 GGUF that fits this GPU "
            "(3B / 7B / 14B) into ~/.huaepub/polish. Fluent sentences are copied; "
            "only dirty spans hit the GPU. The same EPUB is written. "
            "Progress is in File → Open log file. If llama.cpp cannot start because Ollama "
            "is using the GPU, quit Ollama from the tray and retry.</p>"
            "<p>Workers are the Google in-flight <b>ceiling</b> (default 200). "
            "Unofficial Translate rate-limits by IP: the app starts at 8 GETs "
            "and only climbs when requests succeed. A 429 pauses new requests "
            "instead of letting the other 199 keep hammering. "
            "Offline NMT batches locally. Polish runs separately. "
            "The Read tab prefetches the next cached chapter and can live-translate "
            "Chinese cache HTML when Translate is on.</p>"
        )

    def _about(self):
        version = get_current_version()
        show_rich_info(
            self,
            f"About {APP_TITLE}",
            f"<h3 style='margin-bottom:4px;'>{APP_TITLE} v{version}</h3>"
            f"<p>{APP_DESCRIPTION}</p>"
            "<p>Optional: Google (New/HTML/Old) / Microsoft Edge / LibreTranslate / Ollama / Offline NMT translation, then local "
            "llama.cpp polish (auto-installed Qwen GGUF). Ollama is not required for polish. "
            "Help → How translation works. Cache size is Help → Cache…</p>"
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

    @Slot(str)
    def _set_status_safe(self, text: str):
        if QThread.currentThread() != self.thread():
            self._call_on_gui(lambda t=text: self._set_status_safe(t))
            return
        self.progress.set_status(text)

    @Slot(bool, str, str)
    def _on_update_check_ready(self, has_update: bool, latest: str, message: str):
        now = time.monotonic()
        key = (bool(has_update), str(latest), str(message))
        prev = getattr(self, "_last_app_update_check", None)
        if prev is not None and prev[0] == key and (now - prev[1]) < 2.0:
            return
        self._last_app_update_check = (key, now)
        notify = bool(getattr(self, "_update_check_notify", False))
        self._update_check_notify = False
        self._app_update_checking = False

        failed = (message or "").startswith("Failed to check")
        if has_update:
            self.progress.set_status(f"Update available: {latest}")
            if ask_yes_no(
                self, "Update available",
                f"{message}\n\nDownload and install?",
            ):
                self.progress.set_status("Downloading update…")
                download_update_async(
                    progress_callback=lambda _c, _t, s: self._sig_status.emit(
                        s or "Downloading update…"
                    ),
                    completion_callback=lambda ok, msg: self._sig_update_done.emit(
                        bool(ok), str(msg or "")
                    ),
                )
            return
        self.progress.set_status(message or "App is up to date")
        if not notify:
            return
        if failed:
            show_warning(self, "Updates", message or "Failed to check for updates.")
        else:
            show_info(
                self, "Updates",
                message or f"You're running the latest version ({get_current_version()}).",
            )

    @Slot(bool, str)
    def _on_update_download_done(self, ok: bool, message: str):
        if ok:
            self.progress.set_status("Update ready — closing to apply…")
            show_info(
                self,
                "Update ready",
                message
                or "Update installed.\nThe application will now close and reopen.",
            )
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
            return
        self.progress.set_status(message or "Update failed")
        show_warning(
            self, "Update failed", message or "Update failed."
        )
