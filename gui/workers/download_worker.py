# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, Signal, Slot

from core.download_job import clear_job, save_job
from core.download_runner import (
    DownloadCancelled,
    download_chapters_with_cache,
    downloads_folder,
    epub_path,
    epub_translate_kwargs,
    record_successful_download,
    run_single_download,
    translator_backend_kwargs,
)
from core.library import new_chapters_since
from core.notify import notify
from core.parser import Chapter, NovelInfo, get_parser_for_url


class SingleDownloadWorker(QObject):
    progress = Signal(float, str)
    finished_ok = Signal(str, list)  # output_path, failed_titles
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
        ctrl = self.session.control
        try:
            def set_status(s):
                self.progress.emit(-1, s)

            def set_progress(f):
                self.progress.emit(f, "")

            failed = run_single_download(
                control=ctrl,
                cache=self.session.cache,
                library_store=self.session.library_store,
                parser=self.parser,
                info=self.info,
                chapters=self.chapters,
                output_path=self.output_path,
                translated_title=self.translated_title,
                use_cache=bool(self.options.get("use_cache", True)),
                clean=bool(self.options.get("clean", True)),
                translate=bool(self.options.get("translate", True)),
                workers=int(self.options.get("workers", 200)),
                **epub_translate_kwargs(self.session.settings, self.options),
                set_status=set_status,
                set_progress=set_progress,
            )
            clear_job(self.session.data_dir)
            ctrl.active_job = None
            title = self.translated_title or (self.info.title if self.info else "Novel")
            notify("Download complete", f"{title}\nSaved to {Path(self.output_path).name}")
            self.finished_ok.emit(self.output_path, failed)
        except DownloadCancelled:
            self.finished_cancel.emit()
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                ctrl.persist_job(force=True)
            except Exception:
                pass
            self.finished_error.emit(str(e))


class MultiDownloadWorker(QObject):
    progress = Signal(float, str)
    novel_status = Signal(int, str, str)  # index, text, color-ish key
    finished_ok = Signal(str)  # summary
    finished_cancel = Signal()

    def __init__(self, session, novels: list, options: dict, parent=None):
        super().__init__(parent)
        self.session = session
        self.novels = novels
        self.options = options

    @Slot()
    def run(self):
        ctrl = self.session.control
        results = []
        folder = downloads_folder(self.options.get("output_dir", ""))
        total = len(self.novels)
        try:
            for ni, novel in enumerate(self.novels):
                ctrl.wait_while_paused(lambda s: self.progress.emit(-1, s))
                if ctrl.cancel_requested:
                    raise DownloadCancelled()
                info = novel["info"]
                chapters = novel["chapters"]
                parser = novel["parser"]
                title = novel.get("translated_title") or info.title
                self.novel_status.emit(ni, "Downloading", "orange")
                preferred = ""
                entry = self.session.library_store.get_library_entry(info.source_url)
                if entry:
                    preferred = entry.epub_filename or entry.output_path or ""
                out = epub_path(
                    folder, title,
                    preferred_name=Path(preferred).name if preferred else "",
                    preferred_path=preferred,
                )

                def set_status(s, _ni=ni, _tn=total):
                    self.progress.emit(-1, f"Novel {_ni + 1}/{_tn} — {s}")

                def set_progress(f, _ni=ni, _tn=total):
                    self.progress.emit((_ni + f / 2) / _tn, "")

                try:
                    failed = download_chapters_with_cache(
                        control=ctrl,
                        cache=self.session.cache,
                        parser=parser,
                        chapters=chapters,
                        book_key=info.source_url,
                        use_cache=bool(self.options.get("use_cache", True)),
                        set_status=set_status,
                        set_progress=set_progress,
                    )
                    from core.download_runner import build_epub

                    def set_prog_b(f, _ni=ni, _tn=total):
                        self.progress.emit((_ni + 0.5 + f * 0.5) / _tn, "")

                    build_epub(
                        control=ctrl,
                        cache=self.session.cache,
                        info=info,
                        chapters=chapters,
                        output_path=out,
                        clean=bool(self.options.get("clean", True)),
                        translate=bool(self.options.get("translate", True)),
                        workers=int(self.options.get("workers", 200)),
                        **epub_translate_kwargs(self.session.settings, self.options),
                        set_status=set_status,
                        set_progress=set_prog_b,
                    )
                    record_successful_download(
                        self.session.library_store, info, chapters,
                        novel.get("translated_title"), out,
                    )
                    if ctrl.active_job and ctrl.active_job.get("kind") == "multi":
                        for n in ctrl.active_job.get("novels") or []:
                            if n.get("source_url") == info.source_url:
                                n["done"] = True
                        ctrl.persist_job(force=True)
                    results.append((title, out, True, None, len(failed)))
                    self.novel_status.emit(
                        ni,
                        "Done" if not failed else f"Done ({len(failed)} ch. failed)",
                        "green",
                    )
                except DownloadCancelled:
                    raise
                except Exception as e:
                    results.append((title, "", False, str(e), 0))
                    self.novel_status.emit(ni, "Failed", "red")

            if ctrl.cancel_requested:
                raise DownloadCancelled()
            success = [r for r in results if r[2]]
            if ctrl.active_job:
                pending = [n for n in ctrl.active_job.get("novels") or [] if not n.get("done")]
                if pending:
                    ctrl.persist_job(force=True)
                else:
                    clear_job(self.session.data_dir)
                    ctrl.active_job = None
            summary = f"Completed: {len(success)}/{len(results)} novels\n\n"
            for title, path, ok, err, failed_ch in results:
                if ok:
                    line = Path(path).name
                    if failed_ch:
                        line += f" ({failed_ch} failed)"
                    summary += f"  • {line}\n"
                else:
                    summary += f"  • {title[:40]}: {err}\n"
            notify("Multi-download complete", f"{len(success)}/{len(results)} novels saved")
            self.finished_ok.emit(summary)
        except DownloadCancelled:
            self.finished_cancel.emit()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished_ok.emit(f"Multi-download ended with error:\n{e}")


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
        ctrl = self.session.control
        entry = self.entry
        display = entry.translated_title or entry.title or "Novel"
        try:
            parser = get_parser_for_url(entry.source_url)
            if not parser:
                raise Exception("Unsupported site")
            self.progress.emit(0, f"Checking: {display[:40]}...")
            if hasattr(parser, "fetch_all_parallel"):
                info, chapters = parser.fetch_all_parallel(entry.source_url)
            else:
                info = parser.get_novel_info(entry.source_url)
                chapters = parser.get_chapter_list(entry.source_url)
            if not chapters:
                raise Exception("No chapters found")
            new_only, _ = new_chapters_since(
                chapters, entry.last_chapter_url, entry.chapter_count
            )
            if not new_only:
                self.up_to_date.emit(display)
                return
            translated_title = entry.translated_title or info.title
            if self.options.get("translate") and not entry.translated_title:
                try:
                    from core.download_runner import make_translator
                    translated_title = (
                        make_translator(
                            cache=self.session.cache, max_workers=1,
                            **translator_backend_kwargs(
                                self.session.settings, self.options
                            ),
                        ).translate_text(info.title)
                        or info.title
                    )
                except Exception:
                    translated_title = info.title
            out = epub_path(
                downloads_folder(self.options.get("output_dir", "")),
                translated_title,
                preferred_name=entry.epub_filename or "",
                preferred_path=entry.output_path or "",
            )
            from core.download_job import chapters_to_job, novel_info_to_job
            job = {
                "kind": "library_update",
                "status": "running",
                "source_url": entry.source_url,
                "title": info.title or entry.title or "",
                "translated_title": translated_title or "",
                "info": novel_info_to_job(info),
                "chapters": chapters_to_job(chapters),
                "output_path": out,
                "options": self.options,
            }
            ctrl.active_job = job
            save_job(job, self.session.data_dir)

            def set_status(s):
                self.progress.emit(-1, s)

            def set_progress(f):
                self.progress.emit(f / 2, "")

            failed = download_chapters_with_cache(
                control=ctrl,
                cache=self.session.cache,
                parser=parser,
                chapters=chapters,
                book_key=info.source_url or entry.source_url,
                use_cache=bool(self.options.get("use_cache", True)),
                set_status=set_status,
                set_progress=set_progress,
            )
            from core.download_runner import build_epub

            def set_prog_b(f):
                self.progress.emit(0.5 + f * 0.5, "")

            build_epub(
                control=ctrl,
                cache=self.session.cache,
                info=info,
                chapters=chapters,
                output_path=out,
                clean=bool(self.options.get("clean", True)),
                translate=bool(self.options.get("translate", True)),
                workers=int(self.options.get("workers", 200)),
                **epub_translate_kwargs(self.session.settings, self.options),
                set_status=set_status,
                set_progress=set_prog_b,
            )
            record_successful_download(
                self.session.library_store, info, chapters, translated_title, out
            )
            clear_job(self.session.data_dir)
            ctrl.active_job = None
            msg = f"Updated {display}\n+{len(new_only)} new · {len(chapters)} total\n{out}"
            if failed:
                msg += f"\n\n{len(failed)} chapter(s) failed."
            notify("Library update complete", f"{display}: +{len(new_only)} chapters")
            self.finished_ok.emit(msg)
        except DownloadCancelled:
            self.finished_cancel.emit()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished_error.emit(str(e))


class LibraryCheckWorker(QObject):
    progress = Signal(int, int, str)  # idx, total, title
    entry_done = Signal(str, dict)  # url, status dict
    finished = Signal(int, int)  # with_updates, total

    def __init__(self, session, entries: list, parent=None):
        super().__init__(parent)
        self.session = session
        self.entries = entries

    def _refresh_cover(self, entry, info, parser) -> bool:
        """Download cover into cache and update library metadata. Returns True if updated."""
        cover_url = (getattr(info, "cover_url", None) or "").strip()
        if not cover_url:
            return False
        try:
            session = getattr(parser, "session", None)
            if session is None:
                from core.parser import create_http_session
                session = create_http_session()
            r = session.get(cover_url, timeout=20)
            if not getattr(r, "ok", False) or not r.content:
                return False
            ctype = ""
            try:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            except Exception:
                pass
            # Store under cover URL and source URL so grid lookup always finds it
            self.session.cache.put_cover(
                r.content,
                cover_url=cover_url,
                source_url=entry.source_url or "",
                content_type=ctype,
            )
            self.session.cache.put_cover(
                r.content,
                source_url=entry.source_url or "",
                content_type=ctype,
            )
            self.session.library_store.update_metadata(
                entry.source_url,
                title=(info.title or "").strip(),
                author=(info.author or "").strip(),
                cover_url=cover_url,
            )
            return True
        except Exception as e:
            print(f"Cover refresh failed for {entry.source_url}: {e}")
            return False

    @Slot()
    def run(self):
        with_updates = 0
        total = len(self.entries)
        for idx, entry in enumerate(self.entries):
            title = entry.translated_title or entry.title or entry.source_url
            self.progress.emit(idx, total, title)
            try:
                parser = get_parser_for_url(entry.source_url)
                if not parser:
                    raise Exception("Unsupported site")
                info = None
                if hasattr(parser, "fetch_all_parallel"):
                    info, chapters = parser.fetch_all_parallel(entry.source_url)
                else:
                    info = parser.get_novel_info(entry.source_url)
                    chapters = parser.get_chapter_list(entry.source_url)
                try:
                    self.session.cache.put_chapter_list(entry.source_url, chapters)
                except Exception:
                    pass
                cover_refreshed = False
                if info is not None:
                    cover_refreshed = self._refresh_cover(entry, info, parser)
                new_only, _ = new_chapters_since(
                    chapters, entry.last_chapter_url, entry.chapter_count
                )
                if new_only:
                    with_updates += 1
                    st = {
                        "state": "update",
                        "new_count": len(new_only),
                        "total": len(chapters),
                        "error": "",
                        "cover_refreshed": cover_refreshed,
                    }
                else:
                    st = {
                        "state": "current",
                        "new_count": 0,
                        "total": len(chapters),
                        "error": "",
                        "cover_refreshed": cover_refreshed,
                    }
            except Exception as e:
                st = {
                    "state": "error",
                    "new_count": 0,
                    "total": 0,
                    "error": str(e),
                    "cover_refreshed": False,
                }
            self.entry_done.emit(entry.source_url, st)
            import time
            if idx < total - 1:
                time.sleep(0.5)
        self.finished.emit(with_updates, total)


class LibraryUpdateAllWorker(QObject):
    progress = Signal(float, str)
    finished_ok = Signal(str)

    def __init__(self, session, entries: list, options: dict, parent=None):
        super().__init__(parent)
        self.session = session
        self.entries = entries
        self.options = options

    @Slot()
    def run(self):
        ctrl = self.session.control
        results = []
        total = len(self.entries)
        try:
            for idx, entry in enumerate(self.entries):
                ctrl.wait_while_paused(lambda s: self.progress.emit(-1, s))
                if ctrl.cancel_requested:
                    raise DownloadCancelled()
                display = entry.translated_title or entry.title or "Novel"
                self.progress.emit(idx / max(total, 1), f"Update All [{idx + 1}/{total}]: {display[:40]}")
                try:
                    w = LibraryUpdateWorker(self.session, entry, self.options)
                    # Run inline (same thread) by calling run logic via temporary signals collection
                    # Simpler: duplicate minimal path
                    parser = get_parser_for_url(entry.source_url)
                    if not parser:
                        raise Exception("Unsupported site")
                    if hasattr(parser, "fetch_all_parallel"):
                        info, chapters = parser.fetch_all_parallel(entry.source_url)
                    else:
                        info = parser.get_novel_info(entry.source_url)
                        chapters = parser.get_chapter_list(entry.source_url)
                    new_only, _ = new_chapters_since(
                        chapters, entry.last_chapter_url, entry.chapter_count
                    )
                    if not new_only:
                        results.append((display, True, "Already up to date"))
                        if ctrl.active_job and ctrl.active_job.get("kind") == "library_update_all":
                            for e in ctrl.active_job.get("entries") or []:
                                if e.get("source_url") == entry.source_url:
                                    e["done"] = True
                            ctrl.persist_job(force=True)
                        continue
                    translated_title = entry.translated_title or info.title
                    out = epub_path(
                        downloads_folder(self.options.get("output_dir", "")),
                        translated_title,
                        preferred_name=entry.epub_filename or "",
                        preferred_path=entry.output_path or "",
                    )

                    def set_status(s, _i=idx, _t=total):
                        self.progress.emit(-1, f"Update All [{_i + 1}/{_t}] — {s}")

                    def set_progress(f, _i=idx, _t=total):
                        self.progress.emit((_i + f / 2) / _t, "")

                    failed = download_chapters_with_cache(
                        control=ctrl,
                        cache=self.session.cache,
                        parser=parser,
                        chapters=chapters,
                        book_key=info.source_url or entry.source_url,
                        use_cache=bool(self.options.get("use_cache", True)),
                        set_status=set_status,
                        set_progress=set_progress,
                    )
                    from core.download_runner import build_epub

                    def set_prog_b(f, _i=idx, _t=total):
                        self.progress.emit((_i + 0.5 + f * 0.5) / _t, "")

                    build_epub(
                        control=ctrl,
                        cache=self.session.cache,
                        info=info,
                        chapters=chapters,
                        output_path=out,
                        clean=bool(self.options.get("clean", True)),
                        translate=bool(self.options.get("translate", True)),
                        workers=int(self.options.get("workers", 200)),
                        **epub_translate_kwargs(self.session.settings, self.options),
                        set_status=set_status,
                        set_progress=set_prog_b,
                    )
                    record_successful_download(
                        self.session.library_store, info, chapters, translated_title, out
                    )
                    if ctrl.active_job and ctrl.active_job.get("kind") == "library_update_all":
                        for e in ctrl.active_job.get("entries") or []:
                            if e.get("source_url") == entry.source_url:
                                e["done"] = True
                        ctrl.persist_job(force=True)
                    detail = f"+{len(new_only)} → {Path(out).name}"
                    if failed:
                        detail += f" ({len(failed)} failed)"
                    results.append((display, True, detail))
                except DownloadCancelled:
                    raise
                except Exception as e:
                    results.append((display, False, str(e)))

            ok = [r for r in results if r[1]]
            if ctrl.active_job and ctrl.active_job.get("kind") == "library_update_all":
                pending = [e for e in ctrl.active_job.get("entries") or [] if not e.get("done")]
                if pending and not ctrl.cancel_requested:
                    ctrl.persist_job(force=True)
                else:
                    clear_job(self.session.data_dir)
                    ctrl.active_job = None
            summary = f"Update All: {len(ok)}/{len(results)} succeeded"
            notify("Update All complete", summary)
            self.finished_ok.emit(summary)
        except DownloadCancelled:
            self.finished_ok.emit("Update All cancelled")
        except Exception as e:
            self.finished_ok.emit(f"Update All failed: {e}")
