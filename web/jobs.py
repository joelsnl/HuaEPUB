# Author: joelsnl and Anthropic Claude
"""One in-memory job at a time: worker thread, cancel, staging folder."""

from __future__ import annotations

import dataclasses
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.download_runner import DownloadCancelled, DownloadControl
from core.security import is_allowed_epub_path, safe_epub_basename
from core.utils import safe_filename
from web.pipeline import run_pipeline
from web.preview import Preview

TERMINAL_STATES = ("done", "error", "cancelled")

PHASE_LABELS = {
    "fetching": "Fetching chapters",
    "translating": "Translating",
    "writing": "Writing EPUB",
    "done": "Saved",
    "error": "Failed",
    "cancelled": "Cancelled",
}


class JobBusy(Exception):
    def __init__(self, job_id: str):
        super().__init__(job_id)
        self.job_id = job_id


class JobFinished(Exception):
    """Cancel or delete asked of a job in the wrong state."""


@dataclass
class Job:
    id: str
    directory: Path
    total: int
    control: DownloadControl
    state: str = "fetching"
    phase: str = PHASE_LABELS["fetching"]
    current: int = 0
    fraction: float = 0.0
    message: str = ""
    warnings: bool = False
    filename: Optional[str] = None
    notes: str = ""
    flagged: List[int] = field(default_factory=list)
    error: str = ""
    path: Optional[Path] = None
    rev: int = 0
    thread: Optional[threading.Thread] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.id,
                "state": self.state,
                "phase": self.phase,
                "current": self.current,
                "total": self.total,
                "fraction": round(self.fraction, 4),
                "message": self.message,
                "warnings": self.warnings,
                "filename": self.filename,
                "notes": self.notes,
                "flagged": list(self.flagged),
                "error": self.error,
                "rev": self.rev,
            }

    def update(self, **fields: Any) -> None:
        with self._lock:
            if self.state in TERMINAL_STATES:
                return
            for key, value in fields.items():
                setattr(self, key, value)
            self.rev += 1

    def finish(self, state: str, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)
            self.state = state
            self.phase = PHASE_LABELS[state] if state != "done" else self.phase
            self.rev += 1


def _download_filename(preview: Preview, frm: int, to: int, title: str) -> str:
    total = len(preview.chapters)
    suffix = "" if (frm == 1 and to == total) else f" ({frm}-{to})"
    base = f"{safe_filename(title)}{suffix}.epub"
    return safe_epub_basename(base) or "book.epub"


class JobManager:
    def __init__(
        self,
        staging_root: Path,
        cache: Any,
        *,
        runner: Callable[..., Any] = run_pipeline,
        workers: int = 200,
    ):
        self.staging_root = Path(staging_root)
        self._cache = cache
        self._runner = runner
        self._workers = workers
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self.sweep()

    def sweep(self) -> None:
        """Remove every staged EPUB left by an earlier run."""
        if self.staging_root.is_dir():
            for child in self.staging_root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def active(self) -> Optional[Job]:
        with self._lock:
            for job in self._jobs.values():
                if job.state not in TERMINAL_STATES:
                    return job
        return None

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, preview: Preview, chapter_from: int, chapter_to: int) -> Job:
        with self._lock:
            for job in self._jobs.values():
                if job.state not in TERMINAL_STATES:
                    raise JobBusy(job.id)
            # A new job replaces any finished one (and its staged file).
            for old in list(self._jobs.values()):
                shutil.rmtree(old.directory, ignore_errors=True)
            self._jobs.clear()
            # Fresh copies: a second build from one preview must not see filled bodies.
            chapters = [
                dataclasses.replace(ch) for ch in preview.chapters[chapter_from - 1:chapter_to]
            ]
            job = Job(
                id=secrets.token_urlsafe(9),
                directory=self.staging_root / secrets.token_hex(8),
                total=len(chapters),
                control=DownloadControl(),
            )
            job.directory.mkdir(parents=True, exist_ok=True)
            job.control.is_downloading = True
            self._jobs[job.id] = job
        job.thread = threading.Thread(
            target=self._run,
            args=(job, preview, chapters, chapter_from, chapter_to),
            name=f"simple-job-{job.id}",
            daemon=True,
        )
        job.thread.start()
        return job

    def _report(self, job: Job) -> Callable[[str, Optional[float], str], None]:
        def report(state: str, fraction: Optional[float], message: str) -> None:
            fields: Dict[str, Any] = {"state": state, "phase": PHASE_LABELS.get(state, state)}
            if message:
                fields["message"] = message[:240]
            if fraction is not None:
                frac = min(max(float(fraction), 0.0), 1.0)
                fields["fraction"] = frac
                fields["current"] = int(round(frac * job.total))
            job.update(**fields)

        return report

    def _run(self, job: Job, preview: Preview, chapters, frm: int, to: int) -> None:
        output = job.directory / "book.epub"
        try:
            result = self._runner(
                control=job.control,
                cache=self._cache,
                parser=preview.parser,
                info=dataclasses.replace(preview.info),
                chapters=chapters,
                output_path=str(output),
                report=self._report(job),
                workers=self._workers,
            )
        except DownloadCancelled:
            self._cancelled(job)
        except Exception as exc:
            if job.control.cancel_requested and not output.exists():
                self._cancelled(job)
            else:
                shutil.rmtree(job.directory, ignore_errors=True)
                job.finish("error", error=" ".join(str(exc).split())[:200] or exc.__class__.__name__)
        else:
            title = preview.info.title
            job.finish(
                "done",
                phase="Saved with warnings" if result.has_warnings else "Saved",
                fraction=1.0,
                current=job.total,
                warnings=bool(result.has_warnings),
                notes=result.notes,
                flagged=list(result.flagged),
                filename=_download_filename(preview, frm, to, title),
                path=Path(result.path),
                message="",
            )
        finally:
            job.control.is_downloading = False

    def _cancelled(self, job: Job) -> None:
        shutil.rmtree(job.directory, ignore_errors=True)
        job.finish("cancelled")

    def cancel(self, job_id: str) -> Optional[Job]:
        job = self.get(job_id)
        if job is None:
            return None
        if job.state in TERMINAL_STATES:
            raise JobFinished(job_id)
        job.control.request_cancel()
        return job

    def clear(self, job_id: str) -> bool:
        """Forget a finished job and delete its staged EPUB."""
        job = self.get(job_id)
        if job is None:
            return False
        if job.state not in TERMINAL_STATES:
            raise JobBusy(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        shutil.rmtree(job.directory, ignore_errors=True)
        return True

    def epub_for(self, job_id: str) -> Optional[Job]:
        """The job, only when it is done and its file is under the staging root."""
        job = self.get(job_id)
        if job is None or job.state != "done" or job.path is None:
            return None
        if not job.path.is_file() or not is_allowed_epub_path(job.path, [self.staging_root]):
            return None
        return job

    def wait(self, job_id: str, timeout: float = 30.0) -> None:
        job = self.get(job_id)
        deadline = time.monotonic() + timeout
        while job and job.state not in TERMINAL_STATES and time.monotonic() < deadline:
            time.sleep(0.02)
