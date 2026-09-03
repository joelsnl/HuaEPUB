"""Multi Download All: progress slot, Pause enable, worker start (no network)."""

from __future__ import annotations

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, QThread, Slot
    from PySide6.QtWidgets import QMainWindow
    from core.download_runner import DownloadControl, EpubBuildResult
    from core.library import LibraryStore
    from core.parser import Chapter, NovelInfo
    from gui.pages.multi_page import MultiPage
    from gui.pages.single_page import SinglePage
    from gui.widgets.progress_panel import ProgressPanel
    from gui.widgets.resume_banner import ResumeBanner
    from gui.workers.download_worker import MultiDownloadWorker, _live_status
    from gui.window.worker_host import WorkerHostMixin
except ImportError as exc:
    pytest.skip(f"Qt GUI unavailable: {exc}", allow_module_level=True)


class _Session:
    def __init__(self, tmp_path):
        self.data_dir = tmp_path
        self.settings = {}
        self.cache = None
        self.library_store = LibraryStore(tmp_path / "library.json")
        self.control = DownloadControl(data_dir=tmp_path)


class _Host(WorkerHostMixin, QMainWindow):
    def __init__(self, tmp_path):
        super().__init__()
        self._thread = None
        self._worker = None
        self._worker_busy = False
        self._worker_epoch = 0
        self._pending_drive_sync = False
        self.session = _Session(tmp_path)
        self.progress = ProgressPanel()
        self.single = SinglePage()
        self.multi = MultiPage()
        self.resume_banner = ResumeBanner()
        self.got_progress = []
        self.got_novel = []

    @Slot(float, str)
    @Slot(int, str)
    def _on_progress(self, fraction: float, status: str):
        self.got_progress.append((fraction, status))
        WorkerHostMixin._on_progress(self, fraction, status)

    @Slot(int, str, str)
    def _on_multi_novel_status(self, idx: int, status: str, _color: str = ""):
        self.got_novel.append((idx, status))
        self.multi.set_status(idx, status)


def _pump(qapp, ticks=80):
    for _ in range(ticks):
        qapp.processEvents()
        QThread.msleep(15)
    qapp.processEvents()


def _novel(n: int, parser):
    url = f"https://example.com/book{n}"
    info = NovelInfo(title=f"Book {n}", source_url=url)
    chapters = [
        Chapter(title=f"Ch {i}", url=f"{url}/{i}")
        for i in range(3)
    ]
    return {
        "url": url,
        "parser": parser,
        "info": info,
        "chapters": chapters,
        "status": "fetched",
        "translated_title": "",
    }


def test_progress_bar_shows_sliver_for_tiny_fraction(qapp):
    panel = ProgressPanel()
    panel.set_progress(0.00025, None)
    assert panel.bar.value() == 1
    panel.set_progress(0.0, None)
    assert panel.bar.value() == 0


def test_set_progress_replaces_starting_download(qapp):
    """Two-arg set_progress used to leave the footer on Starting download…."""
    panel = ProgressPanel()
    panel.set_progress(0.0, "Starting download…")
    assert panel.status.text() == "Starting download…"
    panel.set_progress(0.0, "Google · Translating: 0/51399 · 8 in flight")
    assert "Starting download" not in panel.status.text()
    assert "Google · Translating:" in panel.status.text()
    assert panel.bar.value() > 0


def test_on_progress_from_pool_thread_updates_footer(qapp, tmp_path):
    """Pool-thread emit used to call QLabel.setText off the GUI thread (no-op)."""
    host = _Host(tmp_path)
    host.progress.set_progress(0.0, "Starting download…")
    started = threading.Event()

    def go():
        started.set()
        WorkerHostMixin._on_progress(
            host, 0.51, "Google · Translating: 0/10 · 8 in flight"
        )

    t = threading.Thread(target=go)
    t.start()
    assert started.wait(2)
    t.join(2)
    for _ in range(80):
        qapp.processEvents()
        if "Google · Translating:" in host.progress.status.text():
            break
        QThread.msleep(15)
    qapp.processEvents()
    assert "Starting download" not in host.progress.status.text()
    assert "Google · Translating:" in host.progress.status.text()
    assert host.progress.bar.value() > 0


def test_live_status_keeps_novel_prefix():
    assert (
        _live_status("Novel 1/2 — ", "Google · Translating: 0/10 · 8 in flight")
        == "Novel 1/2 — Google · Translating: 0/10 · 8 in flight"
    )
    assert _live_status("Novel 1/2 — ", "") == ""


def test_set_downloading_enables_pause_and_replaces_fetch_status(qapp, tmp_path):
    host = _Host(tmp_path)
    host.progress.set_status("Fetched 4/4")
    host._set_downloading(True)
    qapp.processEvents()
    try:
        assert host.session.control.is_downloading is True
        assert host.progress.pause_btn.isEnabled()
        assert host.progress.cancel_btn.isEnabled()
        assert host.progress.pause_btn.text() == "Pause"
        assert host.progress.pause_btn.objectName() != "secondaryBtn"
        assert host.progress.status.text() == "Starting download…"
        assert host.progress.bar.value() == 0
        assert not host.multi.download_btn.isEnabled()
    finally:
        host._set_downloading(False)


def test_pause_looks_distinct_from_disabled(qapp):
    panel = ProgressPanel()
    panel.set_controls_active(False)
    assert not panel.pause_btn.isEnabled()
    assert panel.pause_btn.objectName() == "secondaryBtn"
    panel.set_controls_active(True, paused=False)
    assert panel.pause_btn.isEnabled()
    assert panel.cancel_btn.isEnabled()
    assert panel.pause_btn.objectName() != "secondaryBtn"


def test_multi_page_download_all_wiring(qapp):
    page = MultiPage()
    fired = []
    page.download_all_requested.connect(lambda: fired.append(True))
    info = NovelInfo(title="T", source_url="https://example.com/b")
    ch = [Chapter(title="C1", url="https://example.com/b/1")]
    page.begin_fetch(["https://example.com/b"])
    page.set_row(0, "T", 1, "Ready", {
        "url": "https://example.com/b",
        "parser": object(),
        "info": info,
        "chapters": ch,
        "status": "fetched",
        "translated_title": "",
    })
    page.set_busy(False)
    assert page.download_btn.isEnabled()
    page.download_btn.click()
    qapp.processEvents()
    assert fired == [True]


def test_multi_worker_emits_progress_before_slow_prepare(qapp, tmp_path, monkeypatch):
    """Regression: footer stayed on Fetched 4/4 while row 1 said Downloading."""
    started = threading.Event()
    release = threading.Event()
    parsers_requested = []

    class _FetchParser:
        """Would hang if the download reused the fetch-thread session."""
        def get_chapter_content(self, chapter):
            raise AssertionError("fetch-thread parser must not be used")

    def fake_get_parser(url):
        parsers_requested.append(url)

        class _Live:
            request_delay = 0

            def get_chapter_content(self, chapter):
                return "<p>ok</p>"

        return _Live()

    def fake_download(*args, **kwargs):
        kwargs["set_status"]("Preparing translation…")
        kwargs["set_progress"](0)
        started.set()
        assert release.wait(5)
        return [], EpubBuildResult(output_path=str(tmp_path / "x.epub"))

    monkeypatch.setattr(
        "gui.workers.download_worker.get_parser_for_url", fake_get_parser
    )
    monkeypatch.setattr(
        "gui.workers.download_worker._download_one_novel", fake_download
    )
    monkeypatch.setattr("gui.workers.download_worker.notify", lambda *_a, **_k: None)

    host = _Host(tmp_path)
    host.progress.set_status("Fetched 4/4")
    stale = _FetchParser()
    novels = [_novel(1, stale), _novel(2, stale)]
    host.multi.begin_fetch([n["url"] for n in novels])
    for i, n in enumerate(novels):
        host.multi.set_row(i, n["info"].title, len(n["chapters"]), "Ready", n)

    host._set_downloading(True)
    worker = MultiDownloadWorker(host.session, novels, {"output_dir": str(tmp_path)})
    assert host._bind_and_run(
        worker,
        (worker.progress, host._on_progress),
        (worker.novel_status, host._on_multi_novel_status),
    )
    try:
        for _ in range(120):
            qapp.processEvents()
            if started.is_set() and host.got_progress:
                break
            QThread.msleep(25)
        qapp.processEvents()
        assert started.is_set()
        assert host.got_novel and host.got_novel[0] == (0, "Downloading")
        texts = [s for _f, s in host.got_progress if s]
        assert any("Preparing" in s for s in texts)
        assert host.progress.status.text() != "Fetched 4/4"
        assert host.progress.pause_btn.isEnabled()
        assert host.progress.cancel_btn.isEnabled()
        assert parsers_requested
        assert all("example.com/book" in u for u in parsers_requested)
    finally:
        release.set()
        _pump(qapp, ticks=40)
        host._stop_thread(drain_pending_sync=False, wait_ms=2000)


def test_stale_finish_does_not_kill_new_download_worker(qapp, tmp_path, monkeypatch):
    """Fetch/Drive `_finish_worker_later` must not quit() Download All.

    The regression: cleanup is posted with QTimer.singleShot(0). Download All
    then `_run_worker`s a new thread. The leftover stop hits `self._thread`
    (now the download), wait()s on the GUI, and queued Preparing signals die.
    Footer stays on "Starting download…".
    """
    started = threading.Event()
    release = threading.Event()

    def fake_download(*args, **kwargs):
        kwargs["set_status"]("Preparing translation…")
        kwargs["set_progress"](0)
        started.set()
        assert release.wait(5)
        return [], EpubBuildResult(output_path=str(tmp_path / "x.epub"))

    monkeypatch.setattr(
        "gui.workers.download_worker.get_parser_for_url", lambda _u: object()
    )
    monkeypatch.setattr(
        "gui.workers.download_worker._download_one_novel", fake_download
    )
    monkeypatch.setattr("gui.workers.download_worker.notify", lambda *_a, **_k: None)

    host = _Host(tmp_path)
    dummy = QThread()
    dummy.start()
    host._thread = dummy
    host._worker = QObject()
    host._worker_busy = True
    host._worker_epoch = 1

    posted = threading.Event()

    def from_worker():
        host._finish_worker_later()
        posted.set()

    t = threading.Thread(target=from_worker)
    t.start()
    assert posted.wait(2)
    t.join(2)
    # Cleanup is queued; a new Download All is allowed (busy already false
    # after Fetch All's UI, or `_run_worker` will reap the leftover thread).
    host._worker_busy = False

    novels = [_novel(1, object())]
    host.multi.begin_fetch([novels[0]["url"]])
    host.multi.set_row(0, "Book 1", 3, "Ready", novels[0])
    host._set_downloading(True)
    worker = MultiDownloadWorker(
        host.session, novels, {"output_dir": str(tmp_path)}
    )
    assert host._bind_and_run(
        worker,
        (worker.progress, host._on_progress),
        (worker.novel_status, host._on_multi_novel_status),
    )
    try:
        for _ in range(160):
            qapp.processEvents()
            if started.is_set() and host.got_progress:
                break
            QThread.msleep(25)
        qapp.processEvents()
        assert started.is_set(), "download worker.run() never ran"
        texts = [s for _f, s in host.got_progress if s]
        assert any("Preparing" in s for s in texts)
        assert host.progress.status.text() != "Starting download…"
        assert host._thread is not None
        assert host._thread.isRunning()
        assert host._worker_busy is True
    finally:
        release.set()
        _pump(qapp, ticks=40)
        host._stop_thread(drain_pending_sync=False, wait_ms=2000)


def test_multi_worker_streams_live_fetch_and_translate_status(
    qapp, tmp_path, monkeypatch
):
    """Footer must show chapter/engine/in-flight copy, not Starting download…."""
    started = threading.Event()
    release = threading.Event()

    def fake_download(*args, **kwargs):
        kwargs["set_status"]("Fetching chapters [1/3]: Ch 0  (ETA 12s)")
        kwargs["set_progress"](0.2)
        kwargs["set_status"](
            "Google · Translating: 4/20 · 8 in flight · ch 1/3 Ch 0  (ETA 9s)"
        )
        kwargs["set_build_progress"](0.6, "Polishing English: 2/10  (ETA 8s)")
        kwargs["set_status"]("Writing EPUB file...")
        started.set()
        assert release.wait(5)
        return [], EpubBuildResult(output_path=str(tmp_path / "x.epub"))

    monkeypatch.setattr(
        "gui.workers.download_worker.get_parser_for_url", lambda _u: object()
    )
    monkeypatch.setattr(
        "gui.workers.download_worker._download_one_novel", fake_download
    )
    monkeypatch.setattr("gui.workers.download_worker.notify", lambda *_a, **_k: None)

    host = _Host(tmp_path)
    host.progress.set_status("Fetched 4/4")
    novels = [_novel(1, object()), _novel(2, object())]
    host.multi.begin_fetch([n["url"] for n in novels])
    for i, n in enumerate(novels):
        host.multi.set_row(i, n["info"].title, len(n["chapters"]), "Ready", n)

    host._set_downloading(True)
    worker = MultiDownloadWorker(
        host.session, novels, {"output_dir": str(tmp_path)}
    )
    assert host._bind_and_run(
        worker,
        (worker.progress, host._on_progress),
        (worker.novel_status, host._on_multi_novel_status),
    )
    try:
        for _ in range(160):
            qapp.processEvents()
            if started.is_set() and host.got_progress:
                break
            QThread.msleep(25)
        qapp.processEvents()
        assert started.is_set()
        texts = [s for _f, s in host.got_progress if s]
        assert any("Novel 1/2 — Fetching chapters [1/3]" in s for s in texts)
        assert any("Google · Translating:" in s for s in texts)
        assert any("in flight" in s for s in texts)
        assert any("ETA" in s for s in texts)
        assert any("Polishing English:" in s for s in texts)
        assert any("Writing EPUB file" in s for s in texts)
        assert host.progress.status.text() != "Starting download…"
        assert host.progress.status.text() != "Fetched 4/4"
    finally:
        release.set()
        _pump(qapp, ticks=40)
        host._stop_thread(drain_pending_sync=False, wait_ms=2000)


def test_multi_footer_shows_translate_before_first_http(
    qapp, tmp_path, monkeypatch
):
    """Translate 0/N must replace Starting download… before any GET returns."""
    started = threading.Event()
    release = threading.Event()

    def fake_download(*args, **kwargs):
        kwargs["set_status"](
            "Google · Translating: 0/51399 · 47278 unique requests · 8 in flight"
        )
        kwargs["set_build_progress"](
            0.51,
            "Google · Translating: 0/51399 · 47278 unique requests · 8 in flight",
        )
        started.set()
        assert release.wait(5)
        return [], EpubBuildResult(output_path=str(tmp_path / "x.epub"))

    monkeypatch.setattr(
        "gui.workers.download_worker.get_parser_for_url", lambda _u: object()
    )
    monkeypatch.setattr(
        "gui.workers.download_worker._download_one_novel", fake_download
    )
    monkeypatch.setattr("gui.workers.download_worker.notify", lambda *_a, **_k: None)

    host = _Host(tmp_path)
    host.progress.set_status("Fetched 4/4")
    novels = [_novel(1, object()), _novel(2, object())]
    host.multi.begin_fetch([n["url"] for n in novels])
    for i, n in enumerate(novels):
        host.multi.set_row(i, n["info"].title, len(n["chapters"]), "Ready", n)

    host._set_downloading(True)
    worker = MultiDownloadWorker(
        host.session, novels, {"output_dir": str(tmp_path)}
    )
    assert host._bind_and_run(
        worker,
        (worker.progress, host._on_progress),
        (worker.novel_status, host._on_multi_novel_status),
    )
    try:
        for _ in range(160):
            qapp.processEvents()
            if started.is_set() and host.got_progress:
                break
            QThread.msleep(25)
        qapp.processEvents()
        assert started.is_set()
        assert "Starting download" not in host.progress.status.text()
        assert "Google · Translating:" in host.progress.status.text()
        assert "in flight" in host.progress.status.text()
        assert host.progress.bar.value() > 0
        assert any("Novel 1/2 —" in s for _f, s in host.got_progress if s)
    finally:
        release.set()
        _pump(qapp, ticks=40)
        host._stop_thread(drain_pending_sync=False, wait_ms=2000)
