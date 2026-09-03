"""Drive worker finish / library refresh must parent QObjects on the GUI thread."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot, qInstallMessageHandler
    from PySide6.QtWidgets import QListWidget, QMainWindow, QTabWidget
    from core.library import LibraryStore
    from gui.pages.library_page import LibraryPage
    from gui.widgets.progress_panel import ProgressPanel
    from gui.window.drive_actions import DriveActionsMixin
    from gui.window.worker_host import WorkerHostMixin, _is_gui_thread
except ImportError as exc:
    pytest.skip(f"Qt GUI unavailable: {exc}", allow_module_level=True)


class _FakeCache:
    def get_cover(self, **kwargs):
        return None


class _FakeDriveWorker(QObject):
    finished = Signal(str, str)
    progress = Signal(str)

    @Slot()
    def run(self):
        self.progress.emit("Syncing library.json…")
        self.finished.emit("Synced 3 novel(s)", "")


class _Session:
    def __init__(self, tmp_path):
        self.library_store = LibraryStore(tmp_path / "library.json")
        for i in range(1, 4):
            self.library_store.upsert_library(
                source_url=f"http://example.test/book{i}",
                title=f"Book {i}",
                chapter_count=i,
            )
        self.settings = {
            "library_view": "grid",
            "library_filter": "all",
            "drive_sync_enabled": True,
            "drive_sync_library": True,
            "drive_sync_epubs": True,
        }
        self.cache = _FakeCache()


class _DriveHost(WorkerHostMixin, DriveActionsMixin, QMainWindow):
    """Mirrors MainWindow: real QObject slots wrap mixin implementations."""

    def __init__(self, tmp_path):
        super().__init__()
        self._thread = None
        self._worker = None
        self._worker_busy = False
        self._worker_epoch = 0
        self._pending_drive_sync = False
        self._drive_sync_silent = True
        self.session = _Session(tmp_path)
        self.library = LibraryPage(self.session)
        self.progress = ProgressPanel()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.library, "Library")
        self.drive_start_threads = []

    def _persist_settings(self):
        pass

    @Slot()
    def _start_drive_sync_silent(self):
        self.drive_start_threads.append(QThread.currentThread())
        DriveActionsMixin._start_drive_sync_silent(self)

    @Slot(str)
    def _on_drive_sync_progress(self, msg: str):
        DriveActionsMixin._on_drive_sync_progress(self, msg)

    @Slot(str, str)
    def _on_drive_sync_finished(self, summary: str, err: str):
        DriveActionsMixin._on_drive_sync_finished(self, summary, err)


class _MixinOnlyHost(WorkerHostMixin, DriveActionsMixin, QMainWindow):
    """No MainWindow slot redeclare — mixin @Slot may run on the worker QThread."""

    def __init__(self, tmp_path):
        super().__init__()
        self._thread = None
        self._worker = None
        self._worker_busy = False
        self._worker_epoch = 0
        self._pending_drive_sync = False
        self._drive_sync_silent = True
        self.session = _Session(tmp_path)
        self.library = LibraryPage(self.session)
        self.progress = ProgressPanel()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.library, "Library")

    def _persist_settings(self):
        pass


def _pump(qapp, host=None, ticks=80):
    for _ in range(ticks):
        qapp.processEvents()
        if host is not None and not host._worker_busy:
            qapp.processEvents()
            return
        QThread.msleep(20)
    qapp.processEvents()


def _qt_parent_warnings():
    found = []

    def handler(_mode, _ctx, msg):
        found.append(str(msg))

    prev = qInstallMessageHandler(handler)
    return found, prev


def test_drive_finished_parents_library_items_on_gui(qapp, tmp_path, monkeypatch):
    """DriveSyncWorker.finished → library.refresh() addItem must be on the GUI thread."""
    add_threads = []
    orig = QListWidget.addItem

    def wrapped(self, *args, **kwargs):
        add_threads.append(QThread.currentThread())
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(QListWidget, "addItem", wrapped)

    host = _DriveHost(tmp_path)
    warnings, prev = _qt_parent_warnings()
    try:
        worker = _FakeDriveWorker()
        assert host._bind_and_run(
            worker,
            (worker.progress, host._on_drive_sync_progress),
            (worker.finished, host._on_drive_sync_finished),
        )
        _pump(qapp, host)
        assert host._worker_busy is False
        assert host.library.grid.count() == 3
        assert add_threads
        assert all(t == host.thread() for t in add_threads)
        assert not any("Cannot set parent" in m for m in warnings)
    finally:
        qInstallMessageHandler(prev)
        host._stop_thread(drain_pending_sync=False, wait_ms=1000)


def test_drive_finished_direct_from_qthread_marshals_refresh(
    qapp, tmp_path, monkeypatch
):
    """Mixin slot invoked on the worker QThread must not parent tiles off-GUI."""
    add_threads = []
    orig = QListWidget.addItem

    def wrapped(self, *args, **kwargs):
        add_threads.append(QThread.currentThread())
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(QListWidget, "addItem", wrapped)

    host = _MixinOnlyHost(tmp_path)
    warnings, prev = _qt_parent_warnings()

    class _Caller(QObject):
        @Slot()
        def run(self):
            host._on_drive_sync_finished("Synced 3 novel(s)", "")

    thread = QThread()
    caller = _Caller()
    caller.moveToThread(thread)
    thread.started.connect(caller.run, Qt.ConnectionType.QueuedConnection)
    try:
        thread.start()
        for _ in range(80):
            qapp.processEvents()
            if add_threads:
                break
            QThread.msleep(20)
        qapp.processEvents()
        assert host.library.grid.count() == 3
        assert add_threads
        assert all(t == host.thread() for t in add_threads)
        assert _is_gui_thread(host)
        assert not any("Cannot set parent" in m for m in warnings)
        assert host._worker_busy is False
    finally:
        thread.quit()
        thread.wait(1000)
        qInstallMessageHandler(prev)
        host._stop_thread(drain_pending_sync=False, wait_ms=1000)


def test_stop_thread_from_qthread_starts_drive_on_gui(qapp, tmp_path):
    """`_stop_thread(drain_pending_sync)` must not setParent a QTimer off-GUI."""
    host = _DriveHost(tmp_path)
    host._pending_drive_sync = True
    started = []

    def _silent():
        started.append(QThread.currentThread())

    host._start_drive_sync_silent = _silent
    warnings, prev = _qt_parent_warnings()

    class _Caller(QObject):
        @Slot()
        def run(self):
            host._stop_thread(drain_pending_sync=True, wait_ms=500)

    thread = QThread()
    caller = _Caller()
    caller.moveToThread(thread)
    thread.started.connect(caller.run, Qt.ConnectionType.QueuedConnection)
    try:
        thread.start()
        for _ in range(80):
            qapp.processEvents()
            if started:
                break
            QThread.msleep(20)
        qapp.processEvents()
        assert started
        assert started[0] == host.thread()
        assert not any("Cannot set parent" in m for m in warnings)
        assert host._worker_busy is False
    finally:
        thread.quit()
        thread.wait(1000)
        qInstallMessageHandler(prev)
        host._stop_thread(drain_pending_sync=False, wait_ms=1000)
