# Author: joelsnl and Anthropic Claude
"""Qt workers that run ``core.tasks`` on a background QThread.

The download bodies live in ``core/tasks.py`` so server mode runs the same
steps. These classes only turn callbacks into Qt signals.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from core import tasks
from core.download_runner import DownloadCancelled


def _emit_bar(owner, fraction: float, status: str = "") -> None:
    """Always float+str so MainWindow @Slot(float, str) matches (never int -1)."""
    owner.progress.emit(float(fraction), status or "")


class SingleDownloadWorker(QObject):
    progress = Signal(float, str)
    finished_ok = Signal(str, list, list, bool, list)  # path, failed, warnings, polish_cancelled, heuristic
    finished_cancel = Signal()
    finished_error = Signal(str)

    def __init__(self, session, parser, info, chapters, output_path, translated_title, options, parent=None):
        super().__init__(parent)
        self.session = session
        self.parser = parser
        self.info = info
        self.chapters = chapters
        self.output_path = output_path
        self.translated_title = translated_title
        self.options = options

    @Slot()
    def run(self):
        try:
            result = tasks.run_single(
                self.session,
                self.parser,
                self.info,
                self.chapters,
                self.output_path,
                self.translated_title,
                self.options,
                emit=lambda f, s: _emit_bar(self, f, s),
            )
            self.finished_ok.emit(
                result.path,
                result.failed,
                result.warnings,
                result.polish_cancelled,
                result.heuristic,
            )
        except DownloadCancelled:
            self.finished_cancel.emit()
        except Exception as e:
            self.finished_error.emit(str(e))


class MultiDownloadWorker(QObject):
    progress = Signal(float, str)
    novel_status = Signal(int, str, str)  # index, text, color-ish key
    finished_ok = Signal(str, list)  # summary, [{title, path, source_url}, ...]
    finished_cancel = Signal()

    def __init__(self, session, novels: list, options: dict, parent=None):
        super().__init__(parent)
        self.session = session
        self.novels = novels
        self.options = options

    @Slot()
    def run(self):
        try:
            result = tasks.run_multi(
                self.session,
                self.novels,
                self.options,
                emit=lambda f, s: _emit_bar(self, f, s),
                on_novel_status=lambda i, text, color: self.novel_status.emit(i, text, color),
            )
            self.finished_ok.emit(result.summary, result.previews)
        except DownloadCancelled:
            self.finished_cancel.emit()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished_ok.emit(f"Multi-download ended with error:\n{e}", [])


class LibraryUpdateWorker(QObject):
    progress = Signal(float, str)
    finished_ok = Signal(str)
    finished_cancel = Signal()
    finished_error = Signal(str)
    up_to_date = Signal(str)

    def __init__(self, session, entry, options, parent=None):
        super().__init__(parent)
        self.session = session
        self.entry = entry
        self.options = options

    @Slot()
    def run(self):
        try:
            result = tasks.run_library_update(
                self.session, self.entry, self.options,
                emit=lambda f, s: _emit_bar(self, f, s),
            )
            if result.up_to_date:
                self.up_to_date.emit(result.display)
            else:
                self.finished_ok.emit(result.message)
        except DownloadCancelled:
            self.finished_cancel.emit()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished_error.emit(str(e))


class LibraryCheckWorker(QObject):
    progress = Signal(int, int, str)  # current (1-based), total, title
    entry_done = Signal(str, dict)  # url, status dict
    finished = Signal(int, int)  # with_updates, total

    def __init__(
        self,
        session,
        entries: list,
        options: dict | None = None,
        parent=None,
        *,
        force: bool = False,
    ):
        super().__init__(parent)
        self.session = session
        self.entries = entries
        self.options = options or {}
        self.force = bool(force or (self.options or {}).get("force_check"))

    @Slot()
    def run(self):
        from core.library_check import run_library_check

        def on_progress(current: int, total: int, title: str):
            self.progress.emit(current, total, title)

        def on_entry(url: str, st: dict):
            self.entry_done.emit(url, st)

        with_updates, total = run_library_check(
            self.entries,
            self.session.cache,
            force=self.force,
            on_progress=on_progress,
            on_entry=on_entry,
        )
        self.finished.emit(with_updates, total)


class LibraryUpdateAllWorker(QObject):
    progress = Signal(float, str)
    finished_ok = Signal(str)

    def __init__(
        self, session, entries: list, options: dict, parent=None, *, label: str = "Update All"
    ):
        super().__init__(parent)
        self.session = session
        self.entries = entries
        self.options = options
        self.label = label or "Update All"

    @Slot()
    def run(self):
        summary = tasks.run_library_update_all(
            self.session, self.entries, self.options,
            emit=lambda f, s: _emit_bar(self, f, s),
            label=self.label,
        )
        self.finished_ok.emit(summary)
