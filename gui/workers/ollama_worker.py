# Author: joelsnl and Anthropic Claude
"""Background Ollama model pull (loopback only)."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot


class OllamaPullWorker(QObject):
    progress = Signal(int, str)  # 0–100, or -1 = busy; status text
    finished = Signal(bool, str)  # ok, error

    def __init__(self, model: str, ollama_url: str, parent=None):
        super().__init__(parent)
        self.model = model
        self.ollama_url = ollama_url
        self._cancel = False

    @Slot()
    def cancel(self):
        self._cancel = True

    @Slot()
    def run(self):
        from core.translator import pull_ollama_model

        try:
            print(f"Ollama pull worker started: {self.model}")
            pull_ollama_model(
                self.model,
                self.ollama_url,
                progress_callback=self._emit_progress,
                cancel_check=lambda: self._cancel,
            )
            if self._cancel:
                self.finished.emit(False, "Download cancelled")
                return
            self.finished.emit(True, "")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished.emit(False, str(e) or "Ollama download failed")

    def _emit_progress(self, pct: int, status: str):
        self.progress.emit(pct, status)
