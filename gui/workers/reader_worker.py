# Author: joelsnl and Anthropic Claude
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from core.parser import Chapter, get_parser_for_url


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
            parser = get_parser_for_url(self.url) or get_parser_for_url(self.book_url)
            if not parser:
                self.error.emit(self.index, f"Unsupported site.\n{self.url}")
                return
            chapter = Chapter(title=self.title or "", url=self.url)
            html = parser.get_chapter_content(chapter)
            if not html or not str(html).strip():
                self.error.emit(self.index, "Chapter came back empty.")
                return
            try:
                self.cache.put_chapter(self.book_url, self.url, self.title or "", html)
            except Exception:
                pass
            self.finished.emit(self.index, self.url, html)
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
