# Author: joelsnl and Anthropic Claude
"""MainWindow mixin: the resume banner and picking an interrupted download back up."""

from __future__ import annotations

import traceback


from core.download_job import (
    clear_job, entries_from_job, load_job, novels_from_job, single_from_job,
)
from core.download_runner import (
    downloads_folder, epub_path,
)

from gui.dialogs import (
    ask_yes_no, show_error, show_warning,
)


class ResumeActionsMixin:
    def _check_resume_job(self):
        if self.session.control.is_downloading or self._serving():
            return
        job = load_job(self.session.data_dir)
        if not job:
            return
        self.session.control.active_job = job
        self.resume_banner.show_job(job, self.session.cache)
        self.progress.set_status("Incomplete download ready: resume available")

    def _on_discard_job(self):
        if not ask_yes_no(
            self, "Discard",
            "Remove the saved resume point?\nCached chapter text stays on this PC.",
        ):
            return
        clear_job(self.session.data_dir)
        self.session.control.active_job = None
        self.resume_banner.hide_banner()

    def _on_resume_job(self):
        job = self.session.control.active_job or load_job(self.session.data_dir)
        if not job:
            self.resume_banner.hide_banner()
            return
        kind = job.get("kind")
        try:
            if kind == "single":
                self._resume_single(job)
            elif kind == "multi":
                self._resume_multi(job)
            elif kind == "library_update":
                self._resume_library_update(job)
            elif kind == "library_update_all":
                self._resume_library_update_all(job)
            else:
                show_warning(self, "Resume", f"Unknown job type: {kind}")
                clear_job(self.session.data_dir)
        except Exception as e:
            traceback.print_exc()
            show_error(self, "Resume failed", str(e))

    def _resume_single(self, job: dict):
        self.tabs.setCurrentWidget(self.single)
        self.options.apply_snapshot(job.get("options") or {})
        resumed = single_from_job(job)
        if resumed is None:
            raise Exception("Saved download incomplete")
        url, parser, info, chapters = resumed
        self.single.translated_title = job.get("translated_title") or None
        self.single.set_url(url)
        self.single.show_novel(info, chapters, parser)
        out = job.get("output_path") or epub_path(
            downloads_folder(self.options.snapshot().get("output_dir", "")),
            self.single.translated_title or info.title,
        )
        self._begin_single_download(parser, info, chapters, out, self.single.translated_title, job)

    def _resume_multi(self, job: dict):
        self.tabs.setCurrentWidget(self.multi)
        self.options.apply_snapshot(job.get("options") or {})
        novels = novels_from_job(job)
        if not novels:
            clear_job(self.session.data_dir)
            raise Exception("No unfinished novels left")
        self.multi.begin_fetch([n["url"] for n in novels])
        for i, n in enumerate(novels):
            self.multi.set_row(
                i, n.get("translated_title") or n["info"].title,
                len(n["chapters"]), "Queued", n,
            )
        self.session.control.active_job = job
        self._start_multi_download_with(novels)

    def _resume_library_update(self, job: dict):
        self.tabs.setCurrentWidget(self.library)
        self.options.apply_snapshot(job.get("options") or {})
        entry = self.session.library_store.get_library_entry(job.get("source_url") or "")
        if not entry:
            raise Exception("Library entry missing — try Update from Library")
        self._start_library_update(entry)

    def _resume_library_update_all(self, job: dict):
        self.tabs.setCurrentWidget(self.library)
        self.options.apply_snapshot(job.get("options") or {})
        entries = entries_from_job(job, self.session.library_store)
        if not entries:
            clear_job(self.session.data_dir)
            raise Exception("No unfinished library novels")
        self.session.control.active_job = job
        self._run_library_update_all(entries)
