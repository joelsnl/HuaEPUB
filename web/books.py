# Author: joelsnl and Anthropic Claude
"""Task bodies for server mode: Single, Multi, Library updates and Resume.

Each body runs on the task thread and calls ``core.tasks`` (the same code the
desktop workers run), so a book built from a phone lands in the same books
folder and Library as one built at the PC.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional

from core import tasks as core_tasks
from core.download_job import (
    book_job,
    clear_job,
    entries_from_job,
    job_display_title,
    library_update_all_job,
    load_job,
    multi_job,
    novels_from_job,
    save_job,
    single_from_job,
)
from core.download_runner import (
    completion_has_warnings,
    downloads_folder,
    epub_path,
    format_completion_notes,
    library_epub_path,
)
from web.options import job_options
from web.preview import Preview, PreviewError, build_preview, translate_preview
from web.tasks import TaskContext, TaskManager

MAX_MULTI = 50


def _flagged(chapters, titles) -> List[int]:
    wanted = set(titles or [])
    return [pos for pos, ch in enumerate(chapters) if ch.title in wanted]


def _single_result(ctx: TaskContext, result, chapters) -> Dict[str, Any]:
    notes = format_completion_notes(
        result.failed, result.warnings, result.polish_cancelled, result.heuristic,
    )
    flagged_titles = list(result.failed) + [t for t, _n in result.warnings] + list(result.heuristic)
    file = ctx.offer_file(result.path)
    return {
        "files": [file] if file else [],
        "notes": notes,
        "warnings": completion_has_warnings(notes),
        "flagged": _flagged(chapters, flagged_titles),
        "translated": bool(ctx.task.result.get("translated")),
    }


# ----------------------------------------------------------------------
# Single
# ----------------------------------------------------------------------


def start_single(manager: TaskManager, preview: Preview, chapter_from: int, chapter_to: int):
    session = manager.session
    options = job_options(session.settings)
    chapters = [dataclasses.replace(ch) for ch in preview.chapters[chapter_from - 1:chapter_to]]
    info = dataclasses.replace(preview.info)
    translated_title = (preview.title_en or None) if options["translate"] else None
    out = library_epub_path(session.library_store, translated_title or info.title,
                            info.source_url, options.get("output_dir", ""))
    job = book_job("single", info, chapters, out, options, translated_title=translated_title or "")
    label = translated_title or info.title or "Novel"
    return _start_single_job(manager, preview.parser, info, chapters, out, translated_title, job,
                             label=label)


def _start_single_job(manager, parser, info, chapters, out, translated_title, job, *, label):
    session = manager.session
    options = job.get("options") or job_options(session.settings)

    def body(ctx: TaskContext) -> Dict[str, Any]:
        ctx.task.update(result={"translated": bool(options.get("translate"))})
        session.control.active_job = job
        save_job(job, session.data_dir)
        result = core_tasks.run_single(
            session, parser, info, chapters, out, translated_title, options,
            emit=ctx.emit, detail=ctx.detail,
        )
        return _single_result(ctx, result, chapters)

    return manager.start("single", label, body, chapters=len(chapters))


# ----------------------------------------------------------------------
# Multi
# ----------------------------------------------------------------------


def start_lookup(manager: TaskManager, urls: List[str], store, *, preview_builder=build_preview,
                 preview_translator=translate_preview):
    rows = [{"url": u, "status": "Waiting", "preview_id": "", "title": "", "title_en": "",
             "chapters": 0, "error": ""} for u in urls]
    session = manager.session

    def body(ctx: TaskContext) -> Dict[str, Any]:
        total = len(urls)
        for i, url in enumerate(urls):
            if session.control.cancel_requested:
                return {"cancelled": True}
            ctx.task.update(phase="looking", novel=i, novels=total,
                            message=f"Reading {i + 1}/{total}: {url[:60]}",
                            fraction=i / max(total, 1))
            ctx.task.set_row(i, status="Reading…")
            try:
                item = preview_builder(url)
            except PreviewError as exc:
                ctx.task.set_row(i, status="Failed", error=exc.detail or exc.code)
                continue
            except Exception as exc:
                ctx.task.set_row(i, status="Failed", error=" ".join(str(exc).split())[:160])
                continue
            if job_options(session.settings)["translate"]:
                preview_translator(item, cache=session.cache)
            store.put(item)
            ctx.task.set_row(
                i, status="Ready", preview_id=item.id, title=item.info.title,
                title_en=item.title_en, chapters=len(item.chapters),
            )
        ready = sum(1 for r in ctx.task.rows if r.get("status") == "Ready")
        return {"ready": ready, "total": total}

    return manager.start("lookup", f"Reading {len(urls)} links", body, rows=rows, novels=len(urls))


def start_multi(manager: TaskManager, previews: List[Preview]):
    session = manager.session
    options = job_options(session.settings)
    novels = []
    for p in previews:
        translated = (p.title_en or "") if options["translate"] else ""
        novels.append({
            "url": p.url,
            "parser": p.parser,
            "info": dataclasses.replace(p.info),
            "chapters": [dataclasses.replace(ch) for ch in p.chapters],
            "status": "fetched",
            "translated_title": translated,
        })
    return _start_multi_job(manager, novels, multi_job(novels, options))


def _start_multi_job(manager, novels, job):
    session = manager.session
    options = job.get("options") or job_options(session.settings)
    rows = [
        {"title": n.get("translated_title") or n["info"].title, "chapters": len(n["chapters"]),
         "status": "Queued", "url": n["url"]}
        for n in novels
    ]

    def body(ctx: TaskContext) -> Dict[str, Any]:
        ctx.task.update(result={"translated": bool(options.get("translate"))})
        session.control.active_job = job
        save_job(job, session.data_dir)
        result = core_tasks.run_multi(
            session, novels, options,
            emit=ctx.emit,
            on_novel_status=lambda i, text, _c: ctx.task.set_row(i, status=text),
            detail=ctx.detail,
        )
        files = [f for f in (ctx.offer_file(p["path"]) for p in result.previews) if f]
        return {
            "files": files,
            "notes": result.summary,
            "warnings": completion_has_warnings(result.summary),
            "translated": bool(options.get("translate")),
        }

    return manager.start("multi", f"Multi-download ({len(novels)} novels)", body,
                         rows=rows, novels=len(novels))


# ----------------------------------------------------------------------
# Library updates
# ----------------------------------------------------------------------


def start_library_update(manager: TaskManager, entry):
    session = manager.session
    options = job_options(session.settings)
    display = entry.translated_title or entry.title or "Novel"

    def body(ctx: TaskContext) -> Dict[str, Any]:
        ctx.task.update(phase="checking", result={"translated": bool(options.get("translate"))})
        result = core_tasks.run_library_update(
            session, entry, options, emit=ctx.emit, detail=ctx.detail,
        )
        if result.up_to_date:
            return {"notes": f"No new chapters for {result.display}.", "up_to_date": True,
                    "files": []}
        file = ctx.offer_file(result.path)
        return {
            "notes": result.message,
            "warnings": completion_has_warnings(result.message),
            "files": [file] if file else [],
            "translated": bool(options.get("translate")),
        }

    return manager.start("library_update", f"Update — {display}", body, library_change=True)


def start_library_update_many(manager: TaskManager, entries: list, *, label: str = "Update All",
                              job: Optional[dict] = None):
    session = manager.session
    options = (job or {}).get("options") or job_options(session.settings)
    job = job or library_update_all_job(entries, options)
    rows = [{"title": e.translated_title or e.title or e.source_url, "status": "Queued",
             "url": e.source_url} for e in entries]

    def body(ctx: TaskContext) -> Dict[str, Any]:
        ctx.task.update(result={"translated": bool(options.get("translate"))})
        session.control.active_job = job
        save_job(job, session.data_dir)

        def detail(info):
            ctx.detail(info)
            idx = int(info.get("novel", 0))
            for i in range(len(ctx.task.rows)):
                if i < idx and ctx.task.rows[i].get("status") in ("Queued", "Updating"):
                    ctx.task.set_row(i, status="Finished")
            ctx.task.set_row(idx, status="Updating")

        summary = core_tasks.run_library_update_all(
            session, entries, options, emit=ctx.emit, label=label, detail=detail,
        )
        if summary.endswith("cancelled"):
            return {"cancelled": True, "notes": summary}
        for i in range(len(ctx.task.rows)):
            if ctx.task.rows[i].get("status") in ("Queued", "Updating"):
                ctx.task.set_row(i, status="Finished")
        files = []
        for e in entries:
            fresh = session.library_store.get_library_entry(e.source_url)
            if fresh and fresh.output_path:
                f = ctx.offer_file(fresh.output_path)
                if f:
                    files.append(f)
        return {"notes": summary, "warnings": completion_has_warnings(summary), "files": files,
                "translated": bool(options.get("translate"))}

    return manager.start("library_update_all", label, body, rows=rows, novels=len(entries),
                         library_change=True)


def drive_sync_after_change(ctx: TaskContext) -> None:
    """Desktop rule: a successful Library update queues a quiet Drive sync."""
    session = ctx.session
    if not session.settings.get("drive_sync_enabled"):
        return
    ds = getattr(session, "drive_sync", None)
    if ds is None:
        return
    try:
        connected = ds.is_connected() or ds.try_restore_session()
    except Exception:
        connected = False
    if not connected:
        return
    ctx.task.update(phase="syncing", message="Syncing Google Drive…")
    try:
        summary = core_tasks.run_drive_sync(session, progress=ctx.status)
        print(f"Server: {summary}")
    except Exception as exc:
        print(f"Server: Drive sync failed: {exc}")


def start_drive_sync(manager: TaskManager):
    def body(ctx: TaskContext) -> Dict[str, Any]:
        drive_sync_after_change(ctx)
        return {"notes": "Drive sync finished."}

    return manager.start("sync", "Sync Google Drive", body)


# ----------------------------------------------------------------------
# Resume
# ----------------------------------------------------------------------


def resume_payload(data_dir) -> Optional[Dict[str, Any]]:
    job = load_job(data_dir)
    if not job:
        return None
    return {"kind": job.get("kind") or "", "title": job_display_title(job),
            "status": job.get("status") or ""}


class ResumeError(Exception):
    pass


def start_resume(manager: TaskManager):
    session = manager.session
    job = load_job(session.data_dir)
    if not job:
        raise ResumeError("There is no unfinished download.")
    kind = job.get("kind")
    if kind == "single":
        resumed = single_from_job(job)
        if resumed is None:
            raise ResumeError("Saved download incomplete")
        _url, parser, info, chapters = resumed
        translated = job.get("translated_title") or None
        out = job.get("output_path") or epub_path(
            downloads_folder((job.get("options") or {}).get("output_dir", "")),
            translated or info.title,
        )
        return _start_single_job(manager, parser, info, chapters, out, translated, job,
                                 label=translated or info.title)
    if kind == "multi":
        novels = novels_from_job(job)
        if not novels:
            clear_job(session.data_dir)
            raise ResumeError("No unfinished novels left")
        return _start_multi_job(manager, novels, job)
    if kind == "library_update":
        entry = session.library_store.get_library_entry(job.get("source_url") or "")
        if not entry:
            raise ResumeError("Library entry missing. Try Update from Library.")
        return start_library_update(manager, entry)
    if kind == "library_update_all":
        entries = entries_from_job(job, session.library_store)
        if not entries:
            clear_job(session.data_dir)
            raise ResumeError("No unfinished library novels")
        return start_library_update_many(manager, entries, job=job)
    clear_job(session.data_dir)
    raise ResumeError(f"Unknown job type: {kind}")
