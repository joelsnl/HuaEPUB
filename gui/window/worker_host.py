# Author: joelsnl and Anthropic Claude
"""MainWindow mixin: QThread worker host, progress, pause/cancel."""

from __future__ import annotations

import threading

from PySide6.QtCore import QCoreApplication, QEvent, QThread, QTimer, Qt, Slot
from PySide6.QtWidgets import QApplication

from core.download_job import clear_job

# Posted from a worker QThread; payload lives on the window (`_gui_calls`),
# not on the QEvent — PySide can slice custom QEvent subclasses.
_CALL_ON_GUI_TYPE = QEvent.Type(QEvent.registerEventType())
_gui_queue_lock = threading.Lock()


def _is_gui_thread(obj=None) -> bool:
    """True when the caller is on the GUI thread (or *obj*'s thread).

    Use ``==`` not ``is``: PySide may wrap the same C++ QThread twice.
    """
    current = QThread.currentThread()
    if obj is not None:
        return current == obj.thread()
    app = QApplication.instance()
    if app is None:
        return False
    return current == app.thread()


def _reap_qthread(thread, worker, wait_ms: int) -> None:
    """Quit/wait/delete a QThread. Ignore already-deleted C++ wrappers."""
    if thread is None:
        return
    try:
        if thread.isRunning():
            thread.quit()
            if not thread.wait(max(0, int(wait_ms))):
                thread.terminate()
                thread.wait(min(1000, max(0, int(wait_ms))))
    except RuntimeError:
        return
    try:
        if worker is not None:
            worker.deleteLater()
    except RuntimeError:
        pass
    try:
        thread.deleteLater()
    except RuntimeError:
        pass


class WorkerHostMixin:
    def event(self, ev):
        if ev is not None and ev.type() == _CALL_ON_GUI_TYPE:
            with _gui_queue_lock:
                fns = list(getattr(self, "_gui_calls", None) or ())
                self._gui_calls = []
            for fn in fns:
                if callable(fn):
                    fn()
            return True
        return super().event(ev)

    def _call_on_gui(self, fn) -> None:
        """Run *fn* on this window's thread (GUI), even if called from a worker.

        Always deferred so `_finish_worker_later` does not deleteLater the
        sender while its finished slot is still on the stack.

        On the GUI thread, `QTimer.singleShot(0, self, fn)` is safe (same
        thread as the parent). From a worker QThread that same call constructs
        a QSingleShotTimer and `setParent(self)` — MainWindow lives on the GUI
        thread → "Cannot set parent, new parent is in a different thread".
        """
        if _is_gui_thread(self):
            QTimer.singleShot(0, self, fn)
            return
        with _gui_queue_lock:
            q = getattr(self, "_gui_calls", None)
            if q is None:
                self._gui_calls = q = []
            q.append(fn)
        QCoreApplication.postEvent(self, QEvent(_CALL_ON_GUI_TYPE))

    def _stop_thread(
        self,
        drain_pending_sync: bool = True,
        wait_ms: int = 5000,
        *,
        thread=None,
        worker=None,
        epoch: int | None = None,
    ):
        """Stop background worker. Must only be called from the GUI thread.

        Deferred calls (from `_finish_worker_later`) pass the thread/epoch they
        meant to stop. A later Download All must not be quit() by Fetch All's
        leftover cleanup timer — that dropped progress signals and left the
        footer on "Starting download…".
        """
        if not _is_gui_thread(self):
            # Never wait() from inside the worker thread. Snapshot the job
            # here; `self._thread` may already be a newer worker when the
            # GUI actually runs this.
            target_thread = (
                thread if thread is not None else getattr(self, "_thread", None)
            )
            target_worker = (
                worker if worker is not None else getattr(self, "_worker", None)
            )
            target_epoch = (
                epoch
                if epoch is not None
                else int(getattr(self, "_worker_epoch", 0) or 0)
            )
            self._call_on_gui(
                lambda: self._stop_thread(
                    drain_pending_sync,
                    wait_ms,
                    thread=target_thread,
                    worker=target_worker,
                    epoch=target_epoch,
                )
            )
            return

        current_epoch = int(getattr(self, "_worker_epoch", 0) or 0)
        current_thread = getattr(self, "_thread", None)
        stale = (
            (epoch is not None and epoch != current_epoch)
            or (thread is not None and current_thread is not None and thread is not current_thread)
        )
        if stale:
            _reap_qthread(
                thread if thread is not None else None,
                worker if worker is not None else None,
                wait_ms,
            )
            return

        target_thread = thread if thread is not None else current_thread
        target_worker = worker if worker is not None else getattr(self, "_worker", None)
        had_job = (
            target_thread is not None
            or target_worker is not None
            or bool(getattr(self, "_worker_busy", False))
        )
        self._thread = None
        self._worker = None
        self._worker_busy = False
        if had_job:
            self._worker_epoch = current_epoch + 1
        _reap_qthread(target_thread, target_worker, wait_ms)
        if drain_pending_sync and self._pending_drive_sync:
            self._call_on_gui(self._start_drive_sync_silent)

    def _run_worker(self, worker) -> bool:
        """
        Start a QObject worker on a QThread. Returns False if another job is busy
        (caller should show a message). Always invoke from the GUI thread.

        Connect worker signals to @Slot methods on this window *before* calling
        this. Bare lambdas/partials have no QObject receiver, so Qt may invoke
        them on the worker thread (cross-thread UI = crash).
        """
        if not _is_gui_thread(self):
            return False
        if self._is_check_running() and not self._worker_is_drive(worker):
            return False
        if self._worker_busy and self._thread and self._thread.isRunning():
            return False
        self._stop_thread(drain_pending_sync=False)
        self._worker_epoch = int(getattr(self, "_worker_epoch", 0) or 0) + 1
        self._thread = QThread()  # no parent — avoids cross-thread parenting issues
        self._worker = worker
        self._worker_busy = True
        worker.moveToThread(self._thread)
        # Queued: run() after exec() starts so quit() is not lost, and so a
        # leftover Direct-connected started handler cannot steal the GUI thread.
        self._thread.started.connect(
            worker.run, Qt.ConnectionType.QueuedConnection
        )
        self._thread.start()
        return True

    def _bind_and_run(self, worker, *pairs) -> bool:
        """Connect (signal, slot) pairs on the GUI thread, then start the worker."""
        for signal, slot in pairs:
            signal.connect(slot, Qt.ConnectionType.QueuedConnection)
        return self._run_worker(worker)

    def _worker_is_drive(self, worker) -> bool:
        try:
            from gui.workers.drive_workers import DriveConnectWorker, DriveSyncWorker
        except Exception:
            return False
        return isinstance(worker, (DriveSyncWorker, DriveConnectWorker))

    def _is_check_running(self) -> bool:
        thread = getattr(self, "_check_thread", None)
        if not getattr(self, "_check_busy", False) or thread is None:
            return False
        try:
            return bool(thread.isRunning())
        except RuntimeError:
            return False

    def _stop_check_thread(self, wait_ms: int = 5000, drain_pending_sync: bool = False):
        """Stop the library-check QThread. Must be called from the GUI thread."""
        if not _is_gui_thread(self):
            self._call_on_gui(
                lambda: self._stop_check_thread(wait_ms, drain_pending_sync)
            )
            return
        thread = getattr(self, "_check_thread", None)
        worker = getattr(self, "_check_worker", None)
        self._check_thread = None
        self._check_worker = None
        self._check_busy = False
        _reap_qthread(thread, worker, wait_ms)
        if drain_pending_sync and getattr(self, "_pending_drive_sync", False) and not (
            self._worker_busy and self._thread and self._thread.isRunning()
        ):
            self._call_on_gui(self._start_drive_sync_silent)

    def _run_check_worker(self, worker) -> bool:
        """Start Library Check on its own thread (does not wait for Drive)."""
        if not _is_gui_thread(self):
            return False
        if self.session.control.is_downloading:
            return False
        if self._is_check_running():
            return False
        self._stop_check_thread(drain_pending_sync=False)
        self._check_thread = QThread()
        self._check_worker = worker
        self._check_busy = True
        worker.moveToThread(self._check_thread)
        self._check_thread.started.connect(
            worker.run, Qt.ConnectionType.QueuedConnection
        )
        self._check_thread.start()
        return True

    def _bind_and_run_check(self, worker, *pairs) -> bool:
        for signal, slot in pairs:
            signal.connect(slot, Qt.ConnectionType.QueuedConnection)
        return self._run_check_worker(worker)

    def _finish_check_worker_later(self):
        self._call_on_gui(lambda: self._stop_check_thread(5000, True))

    def _finish_worker_later(self):
        """Cleanup after a worker finished signal (safe from worker or GUI)."""
        thread = getattr(self, "_thread", None)
        worker = getattr(self, "_worker", None)
        epoch = int(getattr(self, "_worker_epoch", 0) or 0)
        self._call_on_gui(
            lambda: self._stop_thread(
                True, 5000, thread=thread, worker=worker, epoch=epoch
            )
        )

    # ------------------------------------------------------------------
    # Progress / pause
    # ------------------------------------------------------------------

    @Slot(float, str)
    @Slot(int, str)
    def _on_progress(self, fraction: float, status: str):
        """Bar + footer. Always paint non-empty status (do not rely on set_progress).

        Mixin @Slot methods are easy for PySide to invoke on the worker thread.
        QLabel.setText is then ignored; the footer stays on Starting download….
        """
        frac = float(fraction)
        text = status or ""
        if not _is_gui_thread(self):
            self._call_on_gui(lambda f=frac, s=text: self._on_progress(f, s))
            return
        if frac >= 0:
            self.progress.set_progress(frac, text or None)
        if text:
            self.progress.set_status(text)

    def _toggle_pause(self):
        ctrl = self.session.control
        if not ctrl.is_downloading:
            return
        paused = ctrl.toggle_pause()
        ctrl.persist_job(force=True)
        self.progress.set_controls_active(True, paused=paused)
        if paused:
            self.progress.set_status("Paused — click Resume to continue (safe to close the app)")
        else:
            self.progress.set_status("Resuming…")

    def _cancel_download(self):
        self.session.control.request_cancel()
        clear_job(self.session.data_dir)
        self.session.control.active_job = None
        self.resume_banner.hide_banner()
        self.progress.set_status("Cancelling...")

    def _set_downloading(self, on: bool):
        self.session.control.is_downloading = on
        self.session.control.cancel_requested = False
        if on:
            self.session.control.is_paused = False
            # Replace leftover Fetch All copy ("Fetched 4/4") immediately.
            # Google starts scraping on the next signal; LibreTranslate may
            # still emit Preparing translation… first.
            self.progress.set_progress(0.0, "Starting download…")
        self.progress.set_controls_active(on, paused=False)
        self.single.set_fetch_enabled(not on)
        self.multi.set_busy(on)
        self.progress.set_download_enabled(bool(self.single.chapters) and not on)
