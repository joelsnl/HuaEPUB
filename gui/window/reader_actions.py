# Author: joelsnl and Anthropic Claude
"""MainWindow mixin: in-app reader, live translate, N+1 prefetch."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QTimer, Slot

from core.download_runner import downloads_folder, epub_path
from core.parser import get_parser_for_url
from core.reader import (
    KIND_CACHE,
    html_needs_live_translate,
    next_cache_prefetch_index,
    resolve_reader_book,
    resume_index,
)
from core.reading import get_position, set_position
from core.settings import set_setting
from gui.dialogs import pick_item, show_info, show_warning
from gui.workers.reader_worker import (
    DriveEpubDownloadWorker,
    ReaderChapterFetchWorker,
    ReaderTranslateWorker,
)


class ReaderActionsMixin:
    def _preview_downloaded_epub(
        self,
        *,
        path: str,
        source_url: str = "",
        title: str = "",
        extra_chapters=None,
    ):
        """Open the Read tab on a just-written EPUB (no Drive fetch)."""
        dest = (path or "").strip()
        if not dest or not Path(dest).is_file():
            show_info(self, "Preview", "That EPUB is not on disk yet.")
            return
        self._open_reader(
            source_url=source_url or "",
            title=title or Path(dest).stem,
            output_path=dest,
            epub_filename=Path(dest).name,
            extra_chapters=extra_chapters,
            extra_epub_path=dest,
            allow_drive=False,
        )

    def _preview_multi_epubs(self, books: list):
        if not books:
            return
        labels = []
        for item in books:
            name = (item.get("title") or "").strip() or Path(item.get("path") or "").name
            labels.append(name)
        idx = pick_item(self, "Preview", "Which novel?", labels)
        if idx is None or not (0 <= idx < len(books)):
            return
        chosen = books[idx]
        self._preview_downloaded_epub(
            path=chosen.get("path") or "",
            source_url=chosen.get("source_url") or "",
            title=chosen.get("title") or "",
        )

    @Slot(object)
    def _open_reader_from_library(self, entry):
        if entry is None:
            return
        self._open_reader(
            source_url=entry.source_url,
            title=entry.translated_title or entry.title or "",
            output_path=entry.output_path or "",
            epub_filename=entry.epub_filename or "",
            drive_file_id=entry.drive_file_id or "",
        )

    @Slot()
    def _open_reader_from_single(self):
        info = self.single.novel_info
        if info is None:
            show_info(self, "Read", "Fetch a novel first.")
            return
        entry = self.session.library_store.get_library_entry(info.source_url or "")
        self._open_reader(
            source_url=info.source_url or "",
            title=self.single.translated_title or info.title or "",
            output_path=(entry.output_path if entry else "") or "",
            epub_filename=(entry.epub_filename if entry else "") or "",
            drive_file_id=(entry.drive_file_id if entry else "") or "",
            extra_chapters=self.single.chapters,
        )

    def _open_reader(
        self,
        *,
        source_url: str,
        title: str,
        output_path: str = "",
        epub_filename: str = "",
        drive_file_id: str = "",
        extra_chapters=None,
        extra_epub_path: str = "",
        allow_drive: bool = True,
    ):
        self._save_reader_position()
        self._reader_open_gen += 1
        result = resolve_reader_book(
            source_url=source_url,
            title=title,
            output_path=output_path,
            epub_filename=epub_filename,
            drive_file_id=drive_file_id if allow_drive else "",
            output_dir=self.session.output_dir,
            cache=self.session.cache,
            extra_chapters=extra_chapters,
            extra_epub_path=extra_epub_path,
        )
        if result.need_drive:
            if self._worker_busy or self.session.control.is_downloading:
                show_warning(
                    self, "Read", "Busy — wait for the current job to finish."
                )
                return
            if self.session.drive_sync.is_connected() and drive_file_id:
                self._start_drive_epub_for_reader(
                    source_url=source_url,
                    title=title,
                    output_path=output_path,
                    epub_filename=epub_filename,
                    drive_file_id=drive_file_id,
                    extra_chapters=extra_chapters,
                )
                return
            result = resolve_reader_book(
                source_url=source_url,
                title=title,
                output_path=output_path,
                epub_filename=epub_filename,
                drive_file_id="",
                output_dir=self.session.output_dir,
                cache=self.session.cache,
                extra_chapters=extra_chapters,
            )
        if result.error or result.book is None:
            show_info(self, "Read", result.error or "Nothing to read yet.")
            return
        self._present_reader(result.book)

    def _start_drive_epub_for_reader(
        self,
        *,
        source_url: str,
        title: str,
        output_path: str,
        epub_filename: str,
        drive_file_id: str,
        extra_chapters=None,
    ):
        folder = downloads_folder(self.session.output_dir)
        dest = epub_path(
            folder,
            title or "book",
            preferred_name=epub_filename,
            preferred_path=output_path,
        )
        self._pending_reader_entry = {
            "source_url": source_url,
            "title": title,
            "output_path": output_path,
            "epub_filename": epub_filename,
            "drive_file_id": drive_file_id,
            "extra_chapters": extra_chapters,
            "gen": self._reader_open_gen,
        }
        worker = DriveEpubDownloadWorker(
            self.session.drive_sync, drive_file_id, dest, folder
        )
        self.progress.set_status("Downloading EPUB from Drive…")
        if not self._bind_and_run(
            worker,
            (worker.status, self._set_status_safe),
            (worker.finished, self._drive_epub_for_reader_done),
            (worker.error, self._drive_epub_for_reader_error),
        ):
            self._pending_reader_entry = None
            show_warning(self, "Read", "Busy — wait for the current job to finish.")

    @Slot(str)
    def _drive_epub_for_reader_done(self, dest: str):
        pending = self._pending_reader_entry or {}
        self._pending_reader_entry = None
        self._stop_thread(drain_pending_sync=False)
        if pending.get("gen") != self._reader_open_gen:
            return
        self._open_reader(
            source_url=pending.get("source_url") or "",
            title=pending.get("title") or "",
            output_path=pending.get("output_path") or "",
            epub_filename=pending.get("epub_filename") or "",
            drive_file_id=pending.get("drive_file_id") or "",
            extra_chapters=pending.get("extra_chapters"),
            extra_epub_path=dest,
            allow_drive=False,
        )

    @Slot(str)
    def _drive_epub_for_reader_error(self, msg: str):
        pending = self._pending_reader_entry or {}
        self._pending_reader_entry = None
        self._stop_thread(drain_pending_sync=False)
        if pending.get("gen") != self._reader_open_gen:
            return
        show_warning(self, "Read", f"Could not download the Drive EPUB.\n{msg}")
        self._open_reader(
            source_url=pending.get("source_url") or "",
            title=pending.get("title") or "",
            output_path=pending.get("output_path") or "",
            epub_filename=pending.get("epub_filename") or "",
            drive_file_id="",
            extra_chapters=pending.get("extra_chapters"),
            allow_drive=False,
        )

    def _present_reader(self, book):
        pos = get_position(book.source_url, data_dir=self.session.data_dir)
        idx = resume_index(book, pos)
        scroll = float((pos or {}).get("scroll") or 0.0)
        try:
            font_pt = int(self.session.settings.get("reader_font_pt") or 18)
        except (TypeError, ValueError):
            font_pt = 18
        current = self.tabs.currentWidget()
        if current is not self.reader:
            self._reader_return = current
        self.reader.load_book(book, index=idx, scroll=scroll, font_pt=font_pt)
        self.tabs.setCurrentWidget(self.reader)
        self._ensure_chapter_loaded(idx)

    def _reader_site_delay(self, url: str, source_url: str = "") -> float:
        parser = get_parser_for_url(url) or get_parser_for_url(source_url)
        try:
            site_delay = float(getattr(parser, "request_delay", 2.0) or 2.0)
        except (TypeError, ValueError):
            site_delay = 2.0
        delay = 0.0
        if self._reader_last_fetch:
            elapsed = time.monotonic() - self._reader_last_fetch
            if elapsed < site_delay:
                delay = site_delay - elapsed
        return delay

    def _apply_reader_html(
        self, index: int, url: str, html: str, *, prefetch: bool = False
    ) -> bool:
        book = self.reader.book
        if book is None or not (0 <= index < len(book.chapters)):
            return False
        ch = book.chapters[index]
        if url and ch.url and ch.url != url:
            return False
        if prefetch:
            ch.html = html
            if self.reader.current_index() == index:
                self.reader.update_chapter_html(index, html)
        else:
            self.reader.update_chapter_html(index, html)
            ch.html = html
        self.reader.set_status("")
        self.progress.set_status("Ready")
        return True

    def _ensure_chapter_loaded(self, index: int):
        book = self.reader.book
        if book is None or not (0 <= index < len(book.chapters)):
            return
        ch = book.chapters[index]
        if (ch.html or "").strip():
            self.reader.set_status("")
            self._after_reader_chapter_ready(index)
            return
        if book.kind != KIND_CACHE or not ch.url:
            self.reader.set_status("This chapter is not in the EPUB.")
            return
        if self._worker_busy or self.session.control.is_downloading:
            self.reader.set_status("Busy — wait for the current job to finish")
            self.progress.set_status("Busy — wait for the current job to finish")
            return
        delay = self._reader_site_delay(ch.url, book.source_url)
        worker = ReaderChapterFetchWorker(
            index,
            ch.url,
            ch.title,
            book.source_url,
            self.session.cache,
            delay=delay,
        )
        self.reader.set_status("Fetching chapter…")
        if not self._bind_and_run(
            worker,
            (worker.status, self._on_reader_fetch_status),
            (worker.finished, self._reader_chapter_fetched),
            (worker.error, self._reader_chapter_fetch_error),
        ):
            self.reader.set_status("Busy — wait for the current job to finish")

    @Slot(str)
    def _on_reader_fetch_status(self, text: str):
        self.reader.set_status(text)
        self.progress.set_status(text)

    @Slot(int, str, str)
    def _reader_chapter_fetched(self, index: int, url: str, html: str):
        self._reader_last_fetch = time.monotonic()
        self._finish_worker_later()
        if not self._apply_reader_html(index, url, html):
            return
        QTimer.singleShot(0, lambda: self._after_reader_chapter_ready(index))

    @Slot(int, str)
    def _reader_chapter_fetch_error(self, index: int, msg: str):
        self._finish_worker_later()
        self.reader.set_status(msg)
        show_warning(self, "Read", msg)

    def _after_reader_chapter_ready(self, index: int):
        if self.session.control.is_downloading:
            return
        if self._maybe_live_translate(index):
            return
        self._queue_reader_n1(index)

    def _maybe_live_translate(self, index: int) -> bool:
        book = self.reader.book
        if book is None or book.kind != KIND_CACHE:
            return False
        if not (0 <= index < len(book.chapters)):
            return False
        o = self.options.snapshot()
        if not o.get("translate"):
            return False
        ch = book.chapters[index]
        if not html_needs_live_translate(ch.html or ""):
            return False
        if self._worker_busy or self.session.control.is_downloading:
            return False
        worker = ReaderTranslateWorker(
            index,
            ch.url or "",
            ch.html or "",
            self.session.cache,
            options=o,
            novel_title=book.title or "",
            detect_text=" ".join(
                [book.title or ""] + [c.title or "" for c in book.chapters[:40]]
            ),
        )
        self.reader.set_status("Translating chapter…")
        return self._bind_and_run(
            worker,
            (worker.status, self._on_reader_fetch_status),
            (worker.finished, self._reader_chapter_translated),
            (worker.error, self._reader_translate_error),
        )

    @Slot(int, str, str)
    def _reader_chapter_translated(self, index: int, url: str, html: str):
        self._finish_worker_later()
        if not self._apply_reader_html(index, url, html):
            return
        QTimer.singleShot(0, lambda: self._queue_reader_n1(index))

    @Slot(int, str)
    def _reader_translate_error(self, index: int, msg: str):
        self._finish_worker_later()
        self.reader.set_status(msg)
        QTimer.singleShot(0, lambda: self._queue_reader_n1(index))

    def _queue_reader_n1(self, index: int):
        book = self.reader.book
        nxt = next_cache_prefetch_index(book, index)
        if nxt is None:
            return
        if self._worker_busy or self.session.control.is_downloading:
            return
        ch = book.chapters[nxt]
        delay = self._reader_site_delay(ch.url, book.source_url)
        worker = ReaderChapterFetchWorker(
            nxt,
            ch.url,
            ch.title,
            book.source_url,
            self.session.cache,
            delay=delay,
        )
        self.reader.set_status("Prefetching next chapter…")
        self._bind_and_run(
            worker,
            (worker.status, self._on_reader_fetch_status),
            (worker.finished, self._reader_prefetch_fetched),
            (worker.error, self._reader_prefetch_error),
        )

    @Slot(int, str, str)
    def _reader_prefetch_fetched(self, index: int, url: str, html: str):
        self._reader_last_fetch = time.monotonic()
        self._finish_worker_later()
        self._apply_reader_html(index, url, html, prefetch=True)

    @Slot(int, str)
    def _reader_prefetch_error(self, _index: int, _msg: str):
        self._finish_worker_later()
        self.reader.set_status("")

    @Slot(int)
    def _on_reader_chapter(self, index: int):
        self._save_reader_position()
        self._ensure_chapter_loaded(index)

    @Slot(int)
    def _on_reader_font(self, pt: int):
        self.session.settings["reader_font_pt"] = int(pt)
        set_setting("reader_font_pt", int(pt))

    def _save_reader_position(self):
        book = self.reader.book
        if book is None or not book.source_url:
            return
        idx = self.reader.current_index()
        ch = book.chapters[idx] if 0 <= idx < len(book.chapters) else None
        set_position(
            book.source_url,
            chapter_url=(ch.url or ch.key) if ch else "",
            chapter_index=idx,
            scroll=self.reader.scroll_ratio(),
            data_dir=self.session.data_dir,
        )

    @Slot()
    def _close_reader(self):
        self._save_reader_position()
        target = self._reader_return or self.library
        self.tabs.setCurrentWidget(target)
