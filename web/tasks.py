# Author: joelsnl and Anthropic Claude
"""One piece of server work at a time: downloads, checks, lookups, Drive sync.

Every browser shares one task slot, the same rule the desktop applies to its
worker thread. Reader fetches take the same slot for a few seconds (see
``exclusive``), so a chapter fetch never overlaps a download from the same
site. Chapter downloads stay sequential inside each task.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from core.download_runner import DownloadCancelled
from core.security import is_allowed_epub_path

TERMINAL = ("done", "error", "cancelled")

PHASES = {
    "starting": "Starting",
    "looking": "Reading the book page",
    "checking": "Checking for new chapters",
    "fetching": "Fetching chapters",
    "translating": "Translating",
    "polishing": "Polishing English",
    "writing": "Writing EPUB",
    "syncing": "Syncing Google Drive",
    "paused": "Paused",
}


class Busy(Exception):
    def __init__(self, task_id: str = "", label: str = ""):
        super().__init__(label or "busy")
        self.task_id = task_id
        self.label = label


def phase_from_status(status: str, fallback: str) -> str:
    low = (status or "").lower()
    if "paused" in low:
        return "paused"
    if "polish" in low:
        return "polishing"
    if "writing epub" in low or low.startswith("writing"):
        return "writing"
    if "translat" in low:
        return "translating"
    if "drive" in low:
        return "syncing"
    if "checking" in low:
        return "checking"
    if "fetch" in low or "chapter" in low or "download" in low:
        return "fetching"
    return fallback


@dataclass
class Task:
    id: str
    kind: str
    label: str
    state: str = "running"
    phase: str = "starting"
    fraction: float = 0.0
    message: str = ""
    novel: int = 0
    novels: int = 1
    chapters: int = 0
    fetched: float = 0.0
    built: float = 0.0
    rows: List[Dict[str, Any]] = field(default_factory=list)
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    rev: int = 0
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "label": self.label,
                "state": self.state,
                "phase": self.phase,
                "phase_label": PHASES.get(self.phase, self.phase.title()),
                "fraction": round(self.fraction, 4),
                "message": self.message,
                "novel": self.novel,
                "novels": self.novels,
                "chapters": self.chapters,
                "fetched": round(self.fetched, 4),
                "built": round(self.built, 4),
                "rows": [dict(r) for r in self.rows],
                "result": dict(self.result),
                "error": self.error,
                "rev": self.rev,
            }

    def update(self, **fields: Any) -> None:
        with self._lock:
            if self.state in TERMINAL:
                return
            for key, value in fields.items():
                setattr(self, key, value)
            self.rev += 1

    def set_row(self, index: int, **fields: Any) -> None:
        with self._lock:
            if 0 <= index < len(self.rows):
                self.rows[index].update(fields)
                self.rev += 1

    def finish(self, state: str, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)
            self.state = state
            self.rev += 1


class TaskContext:
    """What a task body gets: progress reporting and the shared session."""

    def __init__(self, manager: "TaskManager", task: Task):
        self.manager = manager
        self.task = task
        self.session = manager.session

    @property
    def control(self):
        return self.session.control

    def emit(self, fraction: float, status: str = "") -> None:
        fields: Dict[str, Any] = {}
        if status:
            fields["message"] = status[:300]
            phase = phase_from_status(status, self.task.phase)
            if self.session.control.is_paused and phase != "paused":
                phase = "paused"
            fields["phase"] = phase
        try:
            frac = float(fraction)
        except (TypeError, ValueError):
            frac = -1.0
        if frac >= 0:
            fields["fraction"] = min(frac, 1.0)
        if fields:
            self.task.update(**fields)

    def detail(self, info: Dict[str, Any]) -> None:
        novel = int(info.get("novel", 0))
        fields: Dict[str, Any] = {"novel": novel, "novels": int(info.get("novels", 1) or 1)}
        frac = float(info.get("fraction", 0.0))
        if novel != self.task.novel:
            fields["fetched"] = 0.0
            fields["built"] = 0.0
        if info.get("stage") == "build":
            fields["fetched"] = 1.0
            fields["built"] = frac
            if self.task.phase == "fetching":
                fields["phase"] = "translating"
        else:
            fields["fetched"] = frac
        self.task.update(**fields)

    def status(self, text: str) -> None:
        self.emit(-1.0, text)

    def offer_file(self, path: str) -> Optional[Dict[str, str]]:
        return self.manager.register_file(path)


TaskBody = Callable[[TaskContext], Dict[str, Any]]


class TaskManager:
    def __init__(
        self,
        session,
        *,
        file_roots: Callable[[], List[Path]],
        after_library_change: Optional[Callable[[TaskContext], None]] = None,
    ):
        self.session = session
        self._file_roots = file_roots
        self._after_library_change = after_library_change
        self._lock = threading.Lock()
        self._task: Optional[Task] = None
        self._exclusive = False
        self._files: Dict[str, Path] = {}
        self._thread: Optional[threading.Thread] = None
        self.rev = 0

    # -- state -------------------------------------------------------------
    def current(self) -> Optional[Task]:
        with self._lock:
            return self._task

    def active(self) -> Optional[Task]:
        with self._lock:
            task = self._task
            if task is not None and task.state not in TERMINAL:
                return task
        return None

    def is_busy(self) -> bool:
        with self._lock:
            if self._exclusive:
                return True
            return self._task is not None and self._task.state not in TERMINAL

    def snapshot(self) -> Optional[Dict[str, Any]]:
        task = self.current()
        return task.snapshot() if task else None

    # -- files -------------------------------------------------------------
    def register_file(self, path: str) -> Optional[Dict[str, str]]:
        p = Path(path or "")
        if not p.is_file() or not is_allowed_epub_path(p, self._file_roots()):
            return None
        token = secrets.token_urlsafe(12)
        with self._lock:
            if len(self._files) > 200:
                self._files.clear()
            self._files[token] = p.resolve()
        return {"token": token, "name": p.name}

    def file_for(self, token: str) -> Optional[Path]:
        with self._lock:
            path = self._files.get(token or "")
        if path is None or not path.is_file():
            return None
        if not is_allowed_epub_path(path, self._file_roots()):
            return None
        return path

    # -- running -----------------------------------------------------------
    def start(
        self,
        kind: str,
        label: str,
        body: TaskBody,
        *,
        rows: Optional[List[Dict[str, Any]]] = None,
        novels: int = 1,
        chapters: int = 0,
        library_change: bool = False,
    ) -> Task:
        with self._lock:
            if self._exclusive or (self._task is not None and self._task.state not in TERMINAL):
                busy = self._task
                raise Busy(busy.id if busy else "", busy.label if busy else "Reading")
            task = Task(
                id=secrets.token_urlsafe(9), kind=kind, label=label,
                rows=list(rows or []), novels=max(1, novels), chapters=max(0, chapters),
            )
            self._task = task
            ctrl = self.session.control
            ctrl.cancel_requested = False
            ctrl.is_paused = False
            ctrl.is_downloading = kind not in ("lookup", "check", "sync")
        thread = threading.Thread(
            target=self._run, args=(task, body, library_change),
            name=f"server-task-{kind}", daemon=True,
        )
        self._thread = thread
        thread.start()
        return task

    def _run(self, task: Task, body: TaskBody, library_change: bool) -> None:
        ctx = TaskContext(self, task)
        ctrl = self.session.control
        try:
            result = body(ctx) or {}
            if library_change and self._after_library_change is not None and not ctrl.cancel_requested:
                try:
                    self._after_library_change(ctx)
                except Exception as exc:
                    print(f"Server: after-update step failed: {exc}")
            state = "cancelled" if result.get("cancelled") else "done"
            task.finish(state, result=result, fraction=1.0 if state == "done" else task.fraction)
        except DownloadCancelled:
            task.finish("cancelled")
        except Exception as exc:
            traceback.print_exc()
            task.finish("error", error=" ".join(str(exc).split())[:300] or exc.__class__.__name__)
        finally:
            ctrl.is_downloading = False
            ctrl.is_paused = False
            ctrl.translator = None

    @contextlib.contextmanager
    def exclusive(self, label: str = "Reading") -> Iterator[None]:
        """Hold the slot for a short synchronous job (a reader fetch)."""
        with self._lock:
            if self._exclusive or (self._task is not None and self._task.state not in TERMINAL):
                busy = self._task
                raise Busy(busy.id if busy and busy.state not in TERMINAL else "",
                           busy.label if busy and busy.state not in TERMINAL else label)
            self._exclusive = True
        try:
            yield
        finally:
            with self._lock:
                self._exclusive = False

    def pause(self) -> bool:
        task = self.active()
        ctrl = self.session.control
        if task is None or not ctrl.is_downloading:
            return False
        paused = ctrl.toggle_pause()
        try:
            ctrl.persist_job(force=True)
        except Exception:
            pass
        task.update(
            state="paused" if paused else "running",
            phase="paused" if paused else ("fetching" if task.built == 0 else "translating"),
            message="Paused. Resume to continue (safe to stop the server)." if paused else "Resuming…",
        )
        return paused

    def cancel(self) -> bool:
        task = self.active()
        if task is None:
            return False
        ctrl = self.session.control
        ctrl.request_cancel()
        if ctrl.is_downloading:
            # Same as the desktop Cancel button: the resume point goes too.
            from core.download_job import clear_job

            clear_job(self.session.data_dir)
            ctrl.active_job = None
        task.update(message="Cancelling…")
        return True

    def wait(self, timeout: float = 30.0) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def shutdown(self, timeout: float = 15.0) -> None:
        """Stop the running task, keeping its resume point (like closing the app)."""
        task = self.active()
        ctrl = self.session.control
        if task is None:
            return
        try:
            if ctrl.active_job:
                ctrl.active_job["status"] = "paused"
                ctrl.persist_job(force=True)
        except Exception:
            pass
        keep = ctrl.active_job
        ctrl.request_cancel()
        self.wait(timeout)
        if keep is not None:
            from core.download_job import save_job

            try:
                keep["status"] = "paused"
                save_job(keep, self.session.data_dir)
            except Exception:
                pass
