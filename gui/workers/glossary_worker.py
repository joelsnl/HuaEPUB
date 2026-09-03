# Author: joelsnl
from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from core.translation.qwen_glossary import polish_glossaries_with_qwen


class GlossaryQwenWorker(QObject):
    """Run the local Qwen glossary classify pass (blocks the UI via a modal)."""

    progress = Signal(str)
    finished_ok = Signal(object)
    finished_error = Signal(str)

    def __init__(self, books: list | None = None, cache=None, parent=None):
        super().__init__(parent)
        self.books = list(books or [])
        self.cache = cache
        self._cancel = False

    @Slot()
    def request_cancel(self) -> None:
        self._cancel = True

    @Slot()
    def run(self):
        try:
            result = polish_glossaries_with_qwen(
                books=self.books,
                cache=self.cache,
                cancelled=lambda: self._cancel,
                log=lambda msg: self.progress.emit(str(msg)),
                apply=False,
                allow_download=False,
            )
            if result.get("cancelled"):
                self.finished_error.emit(result.get("message") or "Cancelled.")
                return
            self.finished_ok.emit(result)
        except Exception as exc:
            self.finished_error.emit(str(exc))
