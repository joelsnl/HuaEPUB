"""FetchWorker status paths and worker-host cleanup (the fetch-busy regression)."""

from __future__ import annotations

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QThread, Slot
    from PySide6.QtWidgets import QMainWindow
    from core.parser import Chapter, NovelInfo, fetch_info_and_chapters
    from gui.workers.fetch_worker import FetchWorker
    from gui.window.worker_host import WorkerHostMixin, _is_gui_thread
    from parsers.generic import GenericParser
except ImportError as exc:
    pytest.skip(f"Qt GUI unavailable: {exc}", allow_module_level=True)


class _Cache:
    def put_chapter_list(self, url, chapters):
        pass


class _ParallelParser:
    def fetch_all_parallel(self, url):
        return (
            NovelInfo(title="P", source_url=url),
            [Chapter(title="C1", url=url + "/1")],
        )


class _TwoStepParser:
    def get_novel_info(self, url):
        return NovelInfo(title="T", source_url=url)

    def get_chapter_list(self, url):
        return [Chapter(title="C2", url=url + "/2")]


class _Host(WorkerHostMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self._thread = None
        self._worker = None
        self._worker_busy = False
        self._worker_epoch = 0
        self._pending_drive_sync = False
        self.got = None
        self.err = None
        self.statuses = []

    @Slot(object, list, object, str)
    def _done(self, info, chapters, parser, title=""):
        self.got = (getattr(info, "title", None), [c.title for c in chapters], title)
        self._finish_worker_later()

    @Slot(str)
    def _error(self, msg):
        self.err = msg
        self._finish_worker_later()

    @Slot(str)
    def _status(self, msg):
        self.statuses.append(msg)


def _cleanup_host(qapp, host, wait_ms=1000):
    try:
        host._stop_thread(drain_pending_sync=False, wait_ms=wait_ms)
    except Exception:
        pass
    try:
        host.deleteLater()
    except RuntimeError:
        pass
    qapp.processEvents()


def _pump(qapp, host, ticks=40):
    for _ in range(ticks):
        qapp.processEvents()
        if not host._worker_busy:
            return
        QThread.msleep(25)
    qapp.processEvents()


def test_generic_parser_has_no_fetch_all_parallel():
    assert not hasattr(GenericParser(), "fetch_all_parallel")


def test_fetch_info_and_chapters_matches_fetch_worker_paths():
    url = "https://example.com/book"
    assert fetch_info_and_chapters(_ParallelParser(), url)[0].title == "P"
    assert fetch_info_and_chapters(_TwoStepParser(), url)[1][0].title == "C2"


def test_fetch_worker_emits_parallel_status(qapp, monkeypatch):
    monkeypatch.setattr(
        "gui.workers.fetch_worker.get_parser_for_url", lambda _u: _ParallelParser()
    )
    statuses = []
    worker = FetchWorker("https://example.com/book", _Cache())
    try:
        worker.status.connect(statuses.append)
        worker.run()
        assert statuses == ["Fetching novel info & chapters (parallel)..."]
    finally:
        worker.deleteLater()
        qapp.processEvents()


def test_fetch_worker_emits_two_step_status(qapp, monkeypatch):
    monkeypatch.setattr(
        "gui.workers.fetch_worker.get_parser_for_url", lambda _u: _TwoStepParser()
    )
    statuses = []
    worker = FetchWorker("https://example.com/book", _Cache())
    try:
        worker.status.connect(statuses.append)
        worker.run()
        assert statuses == ["Fetching novel info...", "Fetching chapter list..."]
    finally:
        worker.deleteLater()
        qapp.processEvents()


def test_worker_busy_clears_after_finish(qapp, monkeypatch):
    monkeypatch.setattr(
        "gui.workers.fetch_worker.get_parser_for_url", lambda _u: _TwoStepParser()
    )
    host = _Host()
    try:
        worker = FetchWorker("https://example.com/book", _Cache())
        assert host._bind_and_run(
            worker,
            (worker.status, host._status),
            (worker.error, host._error),
            (worker.finished, host._done),
        )
        _pump(qapp, host)
        assert host.err is None
        assert host.got == ("T", ["C2"], "")
        assert host._worker_busy is False
        assert "Fetching novel info..." in host.statuses
        assert "Fetching chapter list..." in host.statuses
        worker2 = FetchWorker("https://example.com/book2", _Cache())
        assert host._bind_and_run(
            worker2,
            (worker2.status, host._status),
            (worker2.error, host._error),
            (worker2.finished, host._done),
        )
        _pump(qapp, host)
        assert host._worker_busy is False
    finally:
        _cleanup_host(qapp, host)


def test_finish_from_background_thread_clears_busy(qapp):
    """Drive-sync slots can run off the GUI thread; cleanup must still clear busy.

    The regression: QTimer.singleShot(0, _stop_thread) without a context QObject
    attached the timer to the worker thread, so busy never cleared and Fetch
    stayed on "Busy — wait for the current job to finish".
    """
    host = _Host()
    host._worker_busy = True
    started = threading.Event()

    def go():
        started.set()
        host._finish_worker_later()

    t = threading.Thread(target=go)
    t.start()
    try:
        assert started.wait(2)
        t.join(2)
        _pump(qapp, host, ticks=80)
        assert _is_gui_thread()
        assert host._worker_busy is False
    finally:
        t.join(2)
        _cleanup_host(qapp, host)
