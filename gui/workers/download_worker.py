# Author: joelsnl and Anthropic Claude
"""Qt workers that wrap download_runner on a background QThread."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from core.download_job import clear_job, save_job
from core.download_runner import (
    DownloadCancelled,
    backend_prefetches_during_fetch,
    download_chapters_with_cache,
    downloads_folder,
    engines_for_chapter_fetch,
    epub_path,
    epub_translate_kwargs,
    format_completion_notes,
    record_successful_download,
    run_single_download,
    translator_backend_kwargs,
)
from core.library import new_chapters_since
from core.notify import notify
from core.parser import fetch_info_and_chapters, get_parser_for_url


def _live_status(prefix: str, status: str) -> str:
    """Keep Novel/Update index on the bar when a phase string is present."""
    return f"{prefix}{status}" if status else ""


def _emit_bar(owner, fraction: float, status: str = "") -> None:
    """Always float+str so MainWindow @Slot(float, str) matches (never int -1)."""
    owner.progress.emit(float(fraction), status or "")


def _parser_on_this_thread(url: str, fallback=None):
    """New HTTP session on this QThread.

    Fetch All stores the parser created on the fetch worker. curl_cffi
    sessions are not safe to reuse after that thread is destroyed — chapter
    GET can hang forever with no further progress signals.
    """
    src = (url or "").strip()
    if src:
        fresh = get_parser_for_url(src)
        if fresh is not None:
            return fresh
    return fallback


def _download_one_novel(
    session,
    parser,
    info,
    chapters,
    output_path: str,
    translated_title,
    options: dict,
    *,
    book_key: str,
    set_status,
    set_progress,
    set_build_progress,
):
    """Fetch chapters, then build EPUB. Raises DownloadCancelled.

    Google/Microsoft start scraping immediately. LibreTranslate still
    builds the translator first so prefetch can overlap request_delay.
    """
    from core.download_runner import build_epub

    translating = bool(options.get("translate", True))
    kw = epub_translate_kwargs(session.settings, options)
    if translating and backend_prefetches_during_fetch(kw["backend"]):
        set_status("Preparing translation…")
        try:
            set_progress(0)
        except TypeError:
            pass
    translator, cleaner = engines_for_chapter_fetch(
        cache=session.cache,
        workers=int(options.get("workers", 200) or 200),
        clean=bool(options.get("clean", True)),
        translate=translating,
        backend=kw["backend"],
        libretranslate_url=kw["libretranslate_url"],
        ollama_url=kw["ollama_url"],
        ollama_model=kw["ollama_model"],
        glossary_mode=kw.get("glossary_mode", "auto"),
        novel_info=info,
        chapters=chapters,
    )
    failed = download_chapters_with_cache(
        control=session.control,
        cache=session.cache,
        parser=parser,
        chapters=chapters,
        book_key=book_key,
        use_cache=bool(options.get("use_cache", True)),
        set_status=set_status,
        set_progress=set_progress,
        translator=translator,
        cleaner=cleaner,
    )
    build_result = build_epub(
        control=session.control,
        cache=session.cache,
        info=info,
        chapters=chapters,
        output_path=output_path,
        clean=bool(options.get("clean", True)),
        translate=bool(options.get("translate", True)),
        workers=int(options.get("workers", 200) or 200),
        **kw,
        set_status=set_status,
        set_progress=set_build_progress,
        translator=translator,
        cleaner=cleaner,
    )
    record_successful_download(
        session.library_store, info, chapters, translated_title, output_path
    )
    return failed, build_result


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
        ctrl = self.session.control
        try:
            def set_status(s):
                _emit_bar(self, -1.0, s)

            def set_progress(f, status=""):
                _emit_bar(self, f, status)

            failed, build_result = run_single_download(
                control=ctrl,
                cache=self.session.cache,
                library_store=self.session.library_store,
                parser=_parser_on_this_thread(
                    getattr(self.info, "source_url", "") or "",
                    self.parser,
                ),
                info=self.info,
                chapters=self.chapters,
                output_path=self.output_path,
                translated_title=self.translated_title,
                use_cache=bool(self.options.get("use_cache", True)),
                clean=bool(self.options.get("clean", True)),
                translate=bool(self.options.get("translate", True)),
                workers=int(self.options.get("workers", 200) or 200),
                **epub_translate_kwargs(self.session.settings, self.options),
                set_status=set_status,
                set_progress=set_progress,
            )
            clear_job(self.session.data_dir)
            ctrl.active_job = None
            title = self.translated_title or (self.info.title if self.info else "Novel")
            notify("Download complete", f"{title}\nSaved to {Path(self.output_path).name}")
            self.finished_ok.emit(
                self.output_path,
                failed,
                build_result.translation_warnings,
                build_result.polish_cancelled,
                build_result.heuristic_chapters,
            )
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
    finished_ok = Signal(str, list)  # summary, [{title, path, source_url}, ...]
    finished_cancel = Signal()

    def __init__(self, session, novels: list, options: dict, parent=None):
        super().__init__(parent)
        self.session = session
        self.novels = novels
        self.options = options

    @Slot()
    def run(self):
        print("Multi-download worker started")
        ctrl = self.session.control
        results = []
        folder = downloads_folder(self.options.get("output_dir", ""))
        total = len(self.novels)
        try:
            if total:
                _emit_bar(self, 0.0, f"Novel 1/{total} — Starting download…")
            for ni, novel in enumerate(self.novels):
                ctrl.wait_while_paused(lambda s: _emit_bar(self, -1.0, s))
                if ctrl.cancel_requested:
                    raise DownloadCancelled()
                info = novel["info"]
                chapters = novel["chapters"]
                title = novel.get("translated_title") or info.title
                self.novel_status.emit(ni, "Downloading", "orange")
                _emit_bar(
                    self,
                    ni / max(total, 1),
                    f"Novel {ni + 1}/{total} — Starting download…",
                )
                parser = _parser_on_this_thread(
                    novel.get("url") or getattr(info, "source_url", "") or "",
                    novel.get("parser"),
                )
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
                    _emit_bar(self, -1.0, f"Novel {_ni + 1}/{_tn} — {s}")

                def set_progress(f, status="", _ni=ni, _tn=total):
                    _emit_bar(
                        self,
                        (_ni + f / 2) / _tn,
                        _live_status(f"Novel {_ni + 1}/{_tn} — ", status),
                    )

                def set_prog_b(f, status="", _ni=ni, _tn=total):
                    _emit_bar(
                        self,
                        (_ni + 0.5 + f * 0.5) / _tn,
                        _live_status(f"Novel {_ni + 1}/{_tn} — ", status),
                    )

                try:
                    failed, build_result = _download_one_novel(
                        self.session,
                        parser,
                        info,
                        chapters,
                        out,
                        novel.get("translated_title"),
                        self.options,
                        book_key=info.source_url,
                        set_status=set_status,
                        set_progress=set_progress,
                        set_build_progress=set_prog_b,
                    )
                    if ctrl.active_job and ctrl.active_job.get("kind") == "multi":
                        for n in ctrl.active_job.get("novels") or []:
                            if n.get("source_url") == info.source_url:
                                n["done"] = True
                        ctrl.persist_job(force=True)
                    notes = format_completion_notes(
                        failed, build_result.translation_warnings,
                        build_result.polish_cancelled,
                        build_result.heuristic_chapters,
                    )
                    results.append(
                        (title, out, True, notes, len(failed), info.source_url or "")
                    )
                    self.novel_status.emit(
                        ni,
                        "Done" if not failed else f"Done ({len(failed)} ch. failed)",
                        "green",
                    )
                    if build_result.polish_cancelled:
                        break
                except DownloadCancelled:
                    raise
                except Exception as e:
                    results.append(
                        (title, "", False, str(e), 0, info.source_url or "")
                    )
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
            for title, path, ok, err, failed_ch, _url in results:
                if ok:
                    line = Path(path).name
                    if failed_ch:
                        line += f" ({failed_ch} failed)"
                    if err:
                        line += f"\n    {err}"
                    summary += f"  • {line}\n"
                else:
                    summary += f"  • {title[:40]}: {err}\n"
            notify("Multi-download complete", f"{len(success)}/{len(results)} novels saved")
            previews = [
                {"title": title, "path": path, "source_url": url}
                for title, path, ok, _err, _failed, url in results
                if ok and path
            ]
            self.finished_ok.emit(summary, previews)
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
        ctrl = self.session.control
        entry = self.entry
        display = entry.translated_title or entry.title or "Novel"
        try:
            parser = get_parser_for_url(entry.source_url)
            if not parser:
                raise Exception("Unsupported site")
            _emit_bar(self, 0.0, f"Checking: {display[:40]}...")
            info, chapters = fetch_info_and_chapters(parser, entry.source_url)
            if not chapters:
                raise Exception("No chapters found")
            try:
                self.session.cache.put_chapter_list(entry.source_url, chapters)
            except Exception:
                pass
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
                    translator = make_translator(
                            cache=self.session.cache, max_workers=1,
                            **translator_backend_kwargs(
                                self.session.settings, self.options
                            ),
                        )
                    cfg = getattr(translator, "configure_glossary", None)
                    if callable(cfg):
                        cfg(info, chapters)
                    translated_title = (
                        translator.translate_text(info.title)
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
                _emit_bar(self, -1.0, s)

            def set_progress(f, status=""):
                _emit_bar(self, f / 2, status)

            def set_prog_b(f, status=""):
                _emit_bar(self, 0.5 + f * 0.5, status)

            failed, build_result = _download_one_novel(
                self.session,
                parser,
                info,
                chapters,
                out,
                translated_title,
                self.options,
                book_key=info.source_url or entry.source_url,
                set_status=set_status,
                set_progress=set_progress,
                set_build_progress=set_prog_b,
            )
            clear_job(self.session.data_dir)
            ctrl.active_job = None
            msg = f"Updated {display}\n+{len(new_only)} new · {len(chapters)} total\n{out}"
            notes = format_completion_notes(
                failed, build_result.translation_warnings, build_result.polish_cancelled,
                build_result.heuristic_chapters,
            )
            if notes:
                msg += "\n\n" + notes
            notify("Library update complete", f"{display}: +{len(new_only)} chapters")
            self.finished_ok.emit(msg)
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
        ctrl = self.session.control
        results = []
        total = len(self.entries)
        try:
            for idx, entry in enumerate(self.entries):
                ctrl.wait_while_paused(lambda s: _emit_bar(self, -1.0, s))
                if ctrl.cancel_requested:
                    raise DownloadCancelled()
                display = entry.translated_title or entry.title or "Novel"
                _emit_bar(
                    self,
                    idx / max(total, 1),
                    f"{self.label} [{idx + 1}/{total}]: {display[:40]}",
                )
                try:
                    parser = get_parser_for_url(entry.source_url)
                    if not parser:
                        raise Exception("Unsupported site")
                    info, chapters = fetch_info_and_chapters(parser, entry.source_url)
                    try:
                        self.session.cache.put_chapter_list(entry.source_url, chapters)
                    except Exception:
                        pass
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
                        _emit_bar(self, -1.0, f"{self.label} [{_i + 1}/{_t}] — {s}")

                    def set_progress(f, status="", _i=idx, _t=total):
                        _emit_bar(
                            self,
                            (_i + f / 2) / _t,
                            _live_status(f"{self.label} [{_i + 1}/{_t}] — ", status),
                        )

                    def set_prog_b(f, status="", _i=idx, _t=total):
                        _emit_bar(
                            self,
                            (_i + 0.5 + f * 0.5) / _t,
                            _live_status(f"{self.label} [{_i + 1}/{_t}] — ", status),
                        )

                    failed, build_result = _download_one_novel(
                        self.session,
                        parser,
                        info,
                        chapters,
                        out,
                        translated_title,
                        self.options,
                        book_key=info.source_url or entry.source_url,
                        set_status=set_status,
                        set_progress=set_progress,
                        set_build_progress=set_prog_b,
                    )
                    if ctrl.active_job and ctrl.active_job.get("kind") == "library_update_all":
                        for e in ctrl.active_job.get("entries") or []:
                            if e.get("source_url") == entry.source_url:
                                e["done"] = True
                        ctrl.persist_job(force=True)
                    detail = f"+{len(new_only)} → {Path(out).name}"
                    notes = format_completion_notes(
                        failed, build_result.translation_warnings,
                        build_result.polish_cancelled,
                        build_result.heuristic_chapters,
                    )
                    if notes:
                        detail += f" ({notes.splitlines()[0]})"
                    results.append((display, True, detail))
                    if build_result.polish_cancelled:
                        break
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
            summary = f"{self.label}: {len(ok)}/{len(results)} succeeded"
            notify(f"{self.label} complete", summary)
            self.finished_ok.emit(summary)
        except DownloadCancelled:
            self.finished_ok.emit(f"{self.label} cancelled")
        except Exception as e:
            self.finished_ok.emit(f"{self.label} failed: {e}")
