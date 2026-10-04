# Author: joelsnl and Anthropic Claude
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from core.reader import (
    UnsupportedSite,
    fetch_reader_chapter,
    html_needs_live_translate,
    live_translate_html,
)


class ReaderChapterFetchWorker(QObject):
    """Fetch one missing chapter for the in-app reader (no EPUB rebuild)."""

    finished = Signal(int, str, str)  # index, url, html
    error = Signal(int, str)
    status = Signal(str)

    def __init__(
        self,
        index: int,
        url: str,
        title: str,
        book_url: str,
        cache,
        delay: float = 0.0,
        parent=None,
    ):
        super().__init__(parent)
        self.index = index
        self.url = url
        self.title = title
        self.book_url = book_url
        self.cache = cache
        self.delay = max(0.0, float(delay or 0.0))

    @Slot()
    def run(self):
        try:
            if self.delay:
                self.status.emit("Waiting for site delay…")
                time.sleep(self.delay)
            self.status.emit(f"Fetching chapter: {(self.title or self.url)[:40]}")
            html = fetch_reader_chapter(self.cache, self.book_url, self.url, self.title)
            self.finished.emit(self.index, self.url, html)
        except UnsupportedSite:
            self.error.emit(self.index, f"Unsupported site.\n{self.url}")
        except Exception as exc:
            self.error.emit(self.index, str(exc))


class ReaderTranslateWorker(QObject):
    """Packed-translate one cache chapter for the reader (no site fetch)."""

    finished = Signal(int, str, str)  # index, url, html
    error = Signal(int, str)
    status = Signal(str)

    def __init__(
        self,
        index: int,
        url: str,
        html: str,
        cache,
        options: dict | None = None,
        novel_title: str = "",
        detect_text: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.index = index
        self.url = url
        self.html = html or ""
        self.cache = cache
        self.options = options or {}
        self.novel_title = novel_title or ""
        self.detect_text = detect_text or ""

    @Slot()
    def run(self):
        try:
            if not html_needs_live_translate(self.html):
                self.finished.emit(self.index, self.url, self.html)
                return
            self.status.emit("Translating chapter…")
            out = live_translate_html(
                self.html, cache=self.cache, options=self.options,
                novel_title=self.novel_title, detect_text=self.detect_text,
            )
            self.finished.emit(self.index, self.url, out)
        except Exception as exc:
            self.error.emit(self.index, str(exc))


class DriveEpubDownloadWorker(QObject):
    finished = Signal(str)
    error = Signal(str)
    status = Signal(str)

    def __init__(self, drive_sync, file_id: str, dest_path: str, allowed_root: Path, parent=None):
        super().__init__(parent)
        self.drive_sync = drive_sync
        self.file_id = file_id
        self.dest_path = dest_path
        self.allowed_root = Path(allowed_root)

    @Slot()
    def run(self):
        try:
            self.status.emit("Downloading EPUB from Drive…")
            dest = self.drive_sync.download_epub(
                self.file_id,
                self.dest_path,
                allowed_root=self.allowed_root,
            )
            self.finished.emit(str(dest))
        except Exception as exc:
            self.error.emit(str(exc))
