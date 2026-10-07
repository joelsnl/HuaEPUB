# Author: joelsnl and Anthropic Claude
"""
Download, library-update and Drive-sync bodies with no Qt and no web code.

The desktop workers (``gui/workers``) and server mode (``web/tasks.py``) both
run these, so a book built from a phone and one built at the PC go through
the same steps. ``session`` is anything with ``cache``, ``control``,
``settings``, ``library_store``, ``data_dir``, ``drive_sync`` and
``output_dir`` (see ``core.session.AppSession``).

``emit(fraction, status)`` drives a progress bar; fraction ``-1.0`` means
"status text only". ``detail`` (optional) gets per-novel stage progress:
``{"novel": i, "novels": n, "stage": "fetch"|"build", "fraction": f}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.cache import remember_english_chapter_titles
from core.download_job import book_job, clear_job, save_job
from core.download_runner import (
    DownloadCancelled,
    backend_prefetches_during_fetch,
    build_epub,
    download_chapters_with_cache,
    downloads_folder,
    engines_for_chapter_fetch,
    epub_path,
    epub_translate_kwargs,
    format_completion_notes,
    library_epub_path,
    record_successful_download,
    translator_backend_kwargs,
)
from core.library import new_chapters_since
from core.notify import notify
from core.parser import fetch_info_and_chapters, get_parser_for_url
from core.utils import format_count, format_ratio, plural

EmitFn = Callable[[float, str], None]
DetailFn = Callable[[Dict[str, Any]], None]

DRIVE_SYNC_CANCELLED = "Drive sync cancelled"


class DriveSyncCancelled(Exception):
    """Raised when a Drive sync stops at a cancel checkpoint."""


def live_status(prefix: str, status: str) -> str:
    """Keep Novel/Update index on the bar when a phase string is present."""
    return f"{prefix}{status}" if status else ""


def _detail(detail: Optional[DetailFn], novel: int, novels: int, stage: str, fraction) -> None:
    if detail is None:
        return
    try:
        frac = float(fraction)
    except (TypeError, ValueError):
        return
    if frac < 0:
        return
    detail({"novel": novel, "novels": novels, "stage": stage, "fraction": min(frac, 1.0)})


def _reporters(emit: EmitFn, detail: Optional[DetailFn], prefix: str, novel: int = 0,
               novels: int = 1):
    """``set_status`` / ``set_progress`` / ``set_build_progress`` for book ``novel`` of ``novels``.

    Fetch fills the first half of that book's share of the bar, the build the
    second half. ``prefix`` (e.g. "Novel 2/5 — ") heads every status line.
    """

    def set_status(s):
        emit(-1.0, f"{prefix}{s}")

    def set_progress(f, status=""):
        emit((novel + f / 2) / novels, live_status(prefix, status))
        _detail(detail, novel, novels, "fetch", f)

    def set_build_progress(f, status=""):
        emit((novel + 0.5 + f * 0.5) / novels, live_status(prefix, status))
        _detail(detail, novel, novels, "build", f)

    return {"set_status": set_status, "set_progress": set_progress,
            "set_build_progress": set_build_progress}


def _notes(failed, build_result) -> str:
    return format_completion_notes(
        failed, build_result.translation_warnings, build_result.polish_cancelled,
        build_result.heuristic_chapters,
    )


def parser_for(url: str, fallback=None):
    """New HTTP session on the calling thread.

    Fetch All stores the parser created on the fetch thread. curl_cffi
    sessions are not safe to reuse after that thread is gone: chapter GET can
    hang forever with no further progress.
    """
    src = (url or "").strip()
    if src:
        fresh = get_parser_for_url(src)
        if fresh is not None:
            return fresh
    return fallback


def download_one_novel(
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
    remember_english_chapter_titles(session.cache, info.source_url, chapters)
    return failed, build_result


# ----------------------------------------------------------------------
# Single
# ----------------------------------------------------------------------


@dataclass
class SingleResult:
    path: str
    failed: List[str] = field(default_factory=list)
    warnings: list = field(default_factory=list)
    polish_cancelled: bool = False
    heuristic: List[str] = field(default_factory=list)


def run_single(
    session,
    parser,
    info,
    chapters,
    output_path: str,
    translated_title,
    options: dict,
    *,
    emit: EmitFn,
    detail: Optional[DetailFn] = None,
) -> SingleResult:
    """One book, start to finish. Clears the resume point on success.

    Raises DownloadCancelled. Any other failure persists the resume point and
    re-raises.
    """
    ctrl = session.control
    try:
        failed, build_result = download_one_novel(
            session,
            parser_for(getattr(info, "source_url", "") or "", parser),
            info,
            chapters,
            output_path,
            translated_title,
            options,
            book_key=info.source_url if info else "",
            **_reporters(emit, detail, ""),
        )
    except DownloadCancelled:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            ctrl.persist_job(force=True)
        except Exception:
            pass
        raise
    clear_job(session.data_dir)
    ctrl.active_job = None
    title = translated_title or (info.title if info else "Novel")
    notify("Download complete", f"{title}\nSaved to {Path(output_path).name}")
    return SingleResult(
        path=output_path,
        failed=list(failed),
        warnings=list(build_result.translation_warnings),
        polish_cancelled=bool(build_result.polish_cancelled),
        heuristic=list(build_result.heuristic_chapters),
    )


# ----------------------------------------------------------------------
# Multi
# ----------------------------------------------------------------------


@dataclass
class MultiResult:
    summary: str
    previews: List[dict] = field(default_factory=list)  # [{title, path, source_url}]
    rows: List[dict] = field(default_factory=list)  # [{title, path, ok, notes, failed}]


def run_multi(
    session,
    novels: list,
    options: dict,
    *,
    emit: EmitFn,
    on_novel_status: Callable[[int, str, str], None] = lambda *_a: None,
    detail: Optional[DetailFn] = None,
) -> MultiResult:
    """Download every novel in turn. Raises DownloadCancelled."""
    print("Multi-download started")
    ctrl = session.control
    results = []
    total = len(novels)
    if total:
        emit(0.0, f"Novel {format_ratio(1, total)} — Starting download…")
    for ni, novel in enumerate(novels):
        ctrl.wait_while_paused(lambda s: emit(-1.0, s))
        if ctrl.cancel_requested:
            raise DownloadCancelled()
        info = novel["info"]
        chapters = novel["chapters"]
        title = novel.get("translated_title") or info.title
        on_novel_status(ni, "Downloading", "orange")
        emit(ni / max(total, 1), f"Novel {format_ratio(ni + 1, total)} — Starting download…")
        parser = parser_for(
            novel.get("url") or getattr(info, "source_url", "") or "",
            novel.get("parser"),
        )
        out = library_epub_path(
            session.library_store, title, info.source_url, options.get("output_dir", "")
        )
        try:
            failed, build_result = download_one_novel(
                session,
                parser,
                info,
                chapters,
                out,
                novel.get("translated_title"),
                options,
                book_key=info.source_url,
                **_reporters(emit, detail, f"Novel {format_ratio(ni + 1, total)} — ", ni, total),
            )
            if ctrl.active_job and ctrl.active_job.get("kind") == "multi":
                for n in ctrl.active_job.get("novels") or []:
                    if n.get("source_url") == info.source_url:
                        n["done"] = True
                ctrl.persist_job(force=True)
            notes = _notes(failed, build_result)
            results.append((title, out, True, notes, len(failed), info.source_url or ""))
            on_novel_status(
                ni,
                "Done" if not failed else f"Done ({len(failed)} ch. failed)",
                "green",
            )
            if build_result.polish_cancelled:
                break
        except DownloadCancelled:
            raise
        except Exception as e:
            results.append((title, "", False, str(e), 0, info.source_url or ""))
            on_novel_status(ni, "Failed", "red")

    if ctrl.cancel_requested:
        raise DownloadCancelled()
    success = [r for r in results if r[2]]
    if ctrl.active_job:
        pending = [n for n in ctrl.active_job.get("novels") or [] if not n.get("done")]
        if pending:
            ctrl.persist_job(force=True)
        else:
            clear_job(session.data_dir)
            ctrl.active_job = None
    summary = f"Completed: {format_ratio(len(success), len(results))} novels\n\n"
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
    notify(
        "Multi-download complete",
        f"{format_ratio(len(success), len(results))} novels saved",
    )
    previews = [
        {"title": title, "path": path, "source_url": url}
        for title, path, ok, _err, _failed, url in results
        if ok and path
    ]
    rows = [
        {"title": title, "path": path, "ok": ok, "notes": err, "failed": failed_ch}
        for title, path, ok, err, failed_ch, _url in results
    ]
    return MultiResult(summary=summary, previews=previews, rows=rows)


# ----------------------------------------------------------------------
# Library
# ----------------------------------------------------------------------


@dataclass
class LibraryUpdateResult:
    up_to_date: bool
    display: str
    message: str = ""
    path: str = ""


def _fetch_entry(session, entry, *, require_chapters: bool = False):
    """``(parser, info, chapters, new_only)`` for a tracked novel; saves the TOC snapshot."""
    parser = get_parser_for_url(entry.source_url)
    if not parser:
        raise Exception("Unsupported site")
    info, chapters = fetch_info_and_chapters(parser, entry.source_url)
    if require_chapters and not chapters:
        raise Exception("No chapters found")
    try:
        session.cache.put_chapter_list(entry.source_url, chapters)
    except Exception:
        pass
    new_only, _ = new_chapters_since(chapters, entry.last_chapter_url, entry.chapter_count)
    return parser, info, chapters, new_only


def _entry_epub_path(entry, title: str, options: dict) -> str:
    """Rebuild over the entry's own EPUB name/path when it has one."""
    return epub_path(
        downloads_folder(options.get("output_dir", "")),
        title,
        preferred_name=entry.epub_filename or "",
        preferred_path=entry.output_path or "",
    )


def run_library_update(
    session,
    entry,
    options: dict,
    *,
    emit: EmitFn,
    detail: Optional[DetailFn] = None,
) -> LibraryUpdateResult:
    """Pull new chapters for one tracked novel and rebuild its EPUB.

    Raises DownloadCancelled, or any fetch/build error.
    """
    ctrl = session.control
    display = entry.translated_title or entry.title or "Novel"
    emit(0.0, f"Checking: {display[:40]}...")
    parser, info, chapters, new_only = _fetch_entry(session, entry, require_chapters=True)
    if not new_only:
        return LibraryUpdateResult(up_to_date=True, display=display)
    translated_title = entry.translated_title or info.title
    if options.get("translate") and not entry.translated_title:
        try:
            from core.download_runner import make_translator

            translator = make_translator(
                cache=session.cache, max_workers=1,
                **translator_backend_kwargs(session.settings, options),
            )
            cfg = getattr(translator, "configure_glossary", None)
            if callable(cfg):
                cfg(info, chapters)
            translated_title = translator.translate_text(info.title) or info.title
        except Exception:
            translated_title = info.title
    out = _entry_epub_path(entry, translated_title, options)
    job = book_job(
        "library_update", info, chapters, out, options,
        translated_title=translated_title, source_url=entry.source_url,
        title=info.title or entry.title or "",
    )
    ctrl.active_job = job
    save_job(job, session.data_dir)
    failed, build_result = download_one_novel(
        session,
        parser,
        info,
        chapters,
        out,
        translated_title,
        options,
        book_key=info.source_url or entry.source_url,
        **_reporters(emit, detail, ""),
    )
    clear_job(session.data_dir)
    ctrl.active_job = None
    msg = (
        f"Updated {display}\n"
        f"+{format_count(len(new_only))} new · {format_count(len(chapters))} total\n"
        f"{out}"
    )
    notes = _notes(failed, build_result)
    if notes:
        msg += "\n\n" + notes
    notify("Library update complete", f"{display}: +{format_count(len(new_only))} chapters")
    return LibraryUpdateResult(up_to_date=False, display=display, message=msg, path=out)


def run_library_update_all(
    session,
    entries: list,
    options: dict,
    *,
    emit: EmitFn,
    label: str = "Update All",
    detail: Optional[DetailFn] = None,
) -> str:
    """Update each entry in turn. Returns a one-line summary (never raises)."""
    ctrl = session.control
    label = label or "Update All"
    results: List[Tuple[str, bool, str]] = []
    total = len(entries)
    try:
        for idx, entry in enumerate(entries):
            ctrl.wait_while_paused(lambda s: emit(-1.0, s))
            if ctrl.cancel_requested:
                raise DownloadCancelled()
            display = entry.translated_title or entry.title or "Novel"
            emit(idx / max(total, 1), f"{label} [{format_ratio(idx + 1, total)}]: {display[:40]}")
            try:
                parser, info, chapters, new_only = _fetch_entry(session, entry)
                if not new_only:
                    results.append((display, True, "Already up to date"))
                    _mark_entry_done(ctrl, entry.source_url)
                    continue
                translated_title = entry.translated_title or info.title
                out = _entry_epub_path(entry, translated_title, options)
                failed, build_result = download_one_novel(
                    session,
                    parser,
                    info,
                    chapters,
                    out,
                    translated_title,
                    options,
                    book_key=info.source_url or entry.source_url,
                    **_reporters(
                        emit, detail, f"{label} [{format_ratio(idx + 1, total)}] — ", idx, total
                    ),
                )
                _mark_entry_done(ctrl, entry.source_url)
                detail_line = f"+{format_count(len(new_only))} → {Path(out).name}"
                notes = _notes(failed, build_result)
                if notes:
                    detail_line += f" ({notes.splitlines()[0]})"
                results.append((display, True, detail_line))
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
                clear_job(session.data_dir)
                ctrl.active_job = None
        summary = f"{label}: {format_ratio(len(ok), len(results))} succeeded"
        notify(f"{label} complete", summary)
        return summary
    except DownloadCancelled:
        return f"{label} cancelled"
    except Exception as e:
        return f"{label} failed: {e}"


def _mark_entry_done(ctrl, source_url: str) -> None:
    if ctrl.active_job and ctrl.active_job.get("kind") == "library_update_all":
        for e in ctrl.active_job.get("entries") or []:
            if e.get("source_url") == source_url:
                e["done"] = True
        ctrl.persist_job(force=True)


# ----------------------------------------------------------------------
# Google Drive
# ----------------------------------------------------------------------


def run_drive_sync(
    session,
    *,
    progress: Callable[[str], None] = lambda _s: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> str:
    """Sync library.json and EPUBs with the connected Drive folder.

    Returns the summary line. Raises DriveSyncCancelled at a cancel
    checkpoint, and RuntimeError when Drive is not usable.
    """
    import time

    from core.settings import save_settings

    ds = session.drive_sync
    if cancelled():
        raise DriveSyncCancelled()
    if not ds.is_connected():
        progress("Restoring Drive session…")
        if not ds.try_restore_session():
            raise RuntimeError("Not connected")
    if cancelled():
        raise DriveSyncCancelled()
    summary_parts = []
    novel_count = 0
    target = ds.inspect_sync_folder()
    folder_name = target.get("name") or "Drive folder"
    if target.get("error"):
        raise RuntimeError(f"Drive folder not usable: {target['error']}")

    if cancelled():
        raise DriveSyncCancelled()
    if session.settings.get("drive_sync_library", True):
        progress(f"Syncing library.json in “{folder_name}”…")
        merged = ds.sync_library_with_store(session.library_store)
        novel_count = len(merged.library) if merged else 0
        summary_parts.append(f"library ({plural(novel_count, 'novel')})")
    if cancelled():
        raise DriveSyncCancelled()
    if session.settings.get("drive_sync_epubs", True):
        progress("Listing remote EPUBs…")
        from core.drive_sync import local_epub_needs_push
        from core.security import is_allowed_epub_path, safe_epub_basename
        from core.settings import get_default_books_dir

        remote = ds.list_remote_books()
        uploaded = 0
        updated = 0
        library = session.library_store.get_library()
        out = getattr(session, "output_dir", "") or ""
        roots = [get_default_books_dir(), downloads_folder(out)]
        if out:
            roots.append(Path(out))
        # Link remote EPUBs onto entries missing drive_file_id
        for entry in library:
            name = safe_epub_basename(
                entry.epub_filename
                or (Path(entry.output_path).name if entry.output_path else "")
            )
            if name and name in remote and not entry.drive_file_id:
                try:
                    session.library_store.update_drive_file(
                        entry.source_url,
                        drive_file_id=remote[name].id,
                        epub_filename=name,
                    )
                except Exception:
                    pass
        pending = []
        for entry in library:
            path = entry.output_path or ""
            name = safe_epub_basename(entry.epub_filename or (Path(path).name if path else ""))
            if not (
                path
                and name
                and Path(path).is_file()
                and is_allowed_epub_path(Path(path), roots)
            ):
                continue
            info = remote.get(name)
            if local_epub_needs_push(Path(path), info):
                pending.append((path, name, info is not None))
        for i, (path, name, is_update) in enumerate(pending):
            if cancelled():
                raise DriveSyncCancelled()
            action = "Updating" if is_update else "Uploading"
            progress(
                f"{action} EPUB {format_count(i + 1)}/{format_count(len(pending))}: {name[:40]}"
            )
            try:
                file_id = ds.upload_epub(path, name)
                if is_update:
                    updated += 1
                else:
                    uploaded += 1
                # Keep Drive id on the library entry
                for entry in library:
                    en = entry.epub_filename or (
                        Path(entry.output_path).name if entry.output_path else ""
                    )
                    if en == name:
                        session.library_store.update_drive_file(
                            entry.source_url,
                            drive_file_id=file_id,
                            epub_filename=name,
                        )
                        break
            except Exception as e:
                print(f"Drive upload failed for {name}: {e}")
        summary_parts.append(
            f"epubs(remote={len(remote)}, uploaded={uploaded}, updated={updated})"
        )
    session.settings["drive_last_synced_at"] = time.time()
    summary = (
        f"Synced “{folder_name}”: " + ", ".join(summary_parts)
        if summary_parts
        else "Sync done"
    )
    if novel_count == 0 and session.settings.get("drive_sync_library", True):
        summary += (
            " — still 0 novels. This folder has no library.json this app can "
            "read. On the other PC: Open folder, confirm library.json, use that "
            "same OAuth client JSON here, then Change folder with that URL."
        )
    session.settings["drive_last_sync_summary"] = summary
    save_settings(session.settings)
    progress(summary)
    return summary
