# Author: joelsnl and Anthropic Claude
"""MainWindow mixin: in-app reader, live translate, N+1 prefetch."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QTimer, Slot

from core.parser import get_parser_for_url
from core.reader import (
    KIND_CACHE,
    html_needs_live_translate,
    next_cache_prefetch_index,
    resolve_reader_book,
    resume_index,
)
from core.reading import get_bookmarks, get_position, set_position, toggle_bookmark
from core.settings import set_setting
from gui.dialogs import busy_message, pick_item, show_info, show_warning
from gui.window.worker_host import _reap_qthread
from gui.workers.reader_worker import ReaderChapterFetchWorker, ReaderTranslateWorker


class ReaderActionsMixin:
    def _preview_downloaded_epub(
        self,
        *,
        path: str,
        source_url: str = "",
        title: str = "",
        extra_chapters=None,
    ):
        """Open the Read tab on a just-written EPUB."""
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
            extra_chapters=self.single.chapters,
        )

    def _open_reader(
        self,
        *,
        source_url: str,
        title: str,
        output_path: str = "",
        epub_filename: str = "",
        extra_chapters=None,
        extra_epub_path: str = "",
    ):
        self._save_reader_position()
        self._reader_open_gen += 1
        result = resolve_reader_book(
            source_url=source_url,
            title=title,
            output_path=output_path,
            epub_filename=epub_filename,
            output_dir=self.session.output_dir,
            cache=self.session.cache,
            extra_chapters=extra_chapters,
            extra_epub_path=extra_epub_path,
        )
        if result.error or result.book is None:
            show_info(self, "Read", result.error or "Nothing to read yet.")
            return
        self._present_reader(result.book)

    def _present_reader(self, book):
        pos = get_position(book.source_url, data_dir=self.session.data_dir)
        idx = resume_index(book, pos)
        scroll = float((pos or {}).get("scroll") or 0.0)
        settings = self.session.settings
        try:
            font_pt = int(settings.get("reader_font_pt") or 18)
        except (TypeError, ValueError):
            font_pt = 18
        marks = get_bookmarks(book.source_url, data_dir=self.session.data_dir)
        current = self.tabs.currentWidget()
        if current is not self.reader:
            self._reader_return = current
        self.reader.load_book(
            book, index=idx, scroll=scroll, font_pt=font_pt,
            theme=str(settings.get("reader_theme") or "paper"),
            mode=str(settings.get("reader_mode") or "pages"),
            face=str(settings.get("reader_face") or "serif"),
            leading=str(settings.get("reader_leading") or "normal"),
            align=str(settings.get("reader_align") or "justify"),
            marks=marks,
        )
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
            self.reader.set_status(busy_message("fetch this chapter"))
            self.progress.set_status(busy_message("fetch this chapter"))
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
            self.reader.set_status(busy_message("fetch this chapter"))

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
        if self._maybe_live_translate(index):
            return
        if self.session.control.is_downloading or self._worker_busy:
            return
        self._queue_reader_n1(index)

    def _translate_worker(self, index: int):
        book = self.reader.book
        ch = book.chapters[index]
        return ReaderTranslateWorker(
            index,
            ch.url or "",
            ch.html or "",
            self.session.cache,
            options=self.options.snapshot(),
            novel_title=book.title or "",
            detect_text=" ".join(
                [book.title or ""] + [c.title or "" for c in book.chapters[:40]]
            ),
            chapter_title=ch.title or "",
            source_url=book.source_url or "",
        )

    def _maybe_live_translate(self, index: int) -> bool:
        book = self.reader.book
        if book is None or book.kind != KIND_CACHE:
            return False
        if not (0 <= index < len(book.chapters)):
            return False
        if not self.options.snapshot().get("translate"):
            return False
        ch = book.chapters[index]
        if not html_needs_live_translate(ch.html or ""):
            return False
        worker = self._translate_worker(index)
        self.reader.set_status("Translating chapter…")
        if self._worker_busy or self.session.control.is_downloading or self._is_check_running():
            return self._start_reader_side(worker)
        return self._bind_and_run(
            worker,
            (worker.status, self._on_reader_fetch_status),
            (worker.finished, self._reader_chapter_translated),
            (worker.error, self._reader_translate_error),
        )

    def _start_reader_side(self, worker) -> bool:
        """Translate while the download thread is busy. This thread is not that worker."""
        if getattr(self, "_reader_tr_busy", False):
            return True
        self._stop_reader_side(wait_ms=0)
        thread = QThread()
        self._reader_tr_thread = thread
        self._reader_tr_worker = worker
        self._reader_tr_busy = True
        worker.moveToThread(thread)
        worker.status.connect(self._on_reader_fetch_status, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._reader_side_translated, Qt.ConnectionType.QueuedConnection)
        worker.error.connect(self._reader_side_translate_error, Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.run, Qt.ConnectionType.QueuedConnection)
        thread.start()
        return True

    def _stop_reader_side(self, wait_ms: int = 2000) -> None:
        thread = getattr(self, "_reader_tr_thread", None)
        worker = getattr(self, "_reader_tr_worker", None)
        self._reader_tr_busy = False
        self._reader_tr_thread = None
        self._reader_tr_worker = None
        _reap_qthread(thread, worker, wait_ms)

    def _note_translated_title(self, index: int):
        worker = self.sender()
        title = getattr(worker, "translated_title", "") or ""
        if title:
            self.reader.set_chapter_title(index, title)

    @Slot(int, str, str)
    def _reader_side_translated(self, index: int, url: str, html: str):
        self._note_translated_title(index)
        self._stop_reader_side()
        self._apply_reader_html(index, url, html)

    @Slot(int, str)
    def _reader_side_translate_error(self, index: int, msg: str):
        self._stop_reader_side()
        self.reader.set_status(msg)

    @Slot(int, str, str)
    def _reader_chapter_translated(self, index: int, url: str, html: str):
        self._note_translated_title(index)
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

    @Slot(str, str)
    def _on_reader_prefs(self, key: str, value: str):
        if key not in {"reader_theme", "reader_mode", "reader_face", "reader_leading", "reader_align"}:
            return
        self.session.settings[key] = value
        set_setting(key, value)

    @Slot(int)
    def _on_reader_bookmark(self, index: int):
        book = self.reader.book
        if book is None or not book.source_url or not (0 <= index < len(book.chapters)):
            return
        ch = book.chapters[index]
        toggle_bookmark(
            book.source_url,
            chapter_url=(ch.url or ch.key),
            chapter_index=index,
            scroll=self.reader.scroll_ratio(),
            data_dir=self.session.data_dir,
        )
        self.reader.set_bookmarks(get_bookmarks(book.source_url, data_dir=self.session.data_dir))

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
