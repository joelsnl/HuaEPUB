# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal, Slot

from core.parser import get_parser_for_url


class FetchWorker(QObject):
    finished = Signal(object, list, object, str)  # info, chapters, parser, translated_title
    error = Signal(str)
    status = Signal(str)

    def __init__(
        self,
        url: str,
        cache,
        translate_title: bool = False,
        backend: str = "google",
        libretranslate_url: str = "https://libretranslate.com",
        parent=None,
    ):
        super().__init__(parent)
        self.url = url
        self.cache = cache
        self.translate_title = translate_title
        self.backend = backend
        self.libretranslate_url = libretranslate_url

    @Slot()
    def run(self):
        try:
            parser = get_parser_for_url(self.url)
            if not parser:
                self.error.emit(f"Unsupported site.\n{self.url}")
                return
            if hasattr(parser, "fetch_all_parallel"):
                self.status.emit("Fetching novel info & chapters (parallel)...")
                info, chapters = parser.fetch_all_parallel(self.url)
            else:
                self.status.emit("Fetching novel info...")
                info = parser.get_novel_info(self.url)
                self.status.emit("Fetching chapter list...")
                chapters = parser.get_chapter_list(self.url)
            try:
                self.cache.put_chapter_list(self.url, chapters)
            except Exception:
                pass

            translated = ""
            if self.translate_title and info and info.title:
                translated = self._translate_title(info.title) or ""
            self.finished.emit(info, chapters, parser, translated)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error.emit(str(e))

    def _translate_title(self, title: str) -> Optional[str]:
        try:
            self.status.emit("Translating title…")
            from core.download_runner import make_translator

            return make_translator(
                cache=self.cache,
                max_workers=1,
                backend=self.backend,
                libretranslate_url=self.libretranslate_url,
            ).translate_text(title)
        except Exception:
            return None
