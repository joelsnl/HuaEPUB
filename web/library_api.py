# Author: joelsnl and Anthropic Claude
"""Library routes: list, covers, check, update, remove, reset, Download EPUB."""

from __future__ import annotations

from pathlib import Path
from typing import List

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from core.download_runner import downloads_folder, epub_path
from core.reader import find_local_epub
from web import books
from web.context import ServerContext
from web.tasks import Busy

MAX_SELECTION = 500


class UrlsIn(BaseModel):
    urls: List[str]


class UrlIn(BaseModel):
    url: str


def busy_response(exc: Busy) -> JSONResponse:
    return JSONResponse({"error": "busy", "task_id": exc.task_id, "label": exc.label},
                        status_code=409)


def sniff_image(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _entry_payload(ctx: ServerContext, e) -> dict:
    st = ctx.check_status.get(e.source_url) or {}
    local = find_local_epub(
        output_path=e.output_path or "",
        epub_filename=e.epub_filename or "",
        output_dir=ctx.session.output_dir or "",
    )
    return {
        "url": e.source_url,
        "title": e.translated_title or e.title or e.source_url,
        "title_original": e.title if (e.translated_title and e.title != e.translated_title) else "",
        "author": e.author or "",
        "chapters": int(e.chapter_count or 0),
        "last_chapter": e.last_chapter_title or "",
        "updated_at": float(e.last_downloaded_at or 0),
        "has_cover": bool(e.cover_url),
        "has_epub": local is not None,
        "on_drive": bool(e.drive_file_id),
        "status": st.get("state") or "",
        "new_count": int(st.get("new_count") or 0),
        "status_error": st.get("error") or "",
    }


def _entries_for(ctx: ServerContext, urls: List[str]):
    out = []
    for url in urls[:MAX_SELECTION]:
        entry = ctx.session.library_store.get_library_entry((url or "").strip())
        if entry:
            out.append(entry)
    return out


def build_router(ctx: ServerContext) -> APIRouter:
    r = APIRouter(prefix="/api/library")
    session = ctx.session

    @r.get("")
    def listing():
        entries = session.library_store.get_library()
        check = ctx.tasks.active()
        return {
            "entries": [_entry_payload(ctx, e) for e in entries],
            "checking": bool(check and check.kind == "check"),
            "drive_connected": _drive_connected(ctx),
        }

    @r.get("/cover")
    def cover(u: str = Query(..., max_length=2048)):
        entry = session.library_store.get_library_entry(u)
        if entry is None:
            return JSONResponse({"error": "unknown_book"}, status_code=404)
        data = None
        try:
            data = session.cache.get_cover(cover_url=entry.cover_url or "", source_url=entry.source_url)
        except Exception:
            data = None
        if not data and entry.cover_url:
            from core.parser import create_http_session
            from core.security import fetch_cover_bytes

            http = create_http_session()
            try:
                data = fetch_cover_bytes(http, entry.cover_url, timeout=15)
                try:
                    session.cache.put_cover(data, cover_url=entry.cover_url,
                                            source_url=entry.source_url)
                except Exception:
                    pass
            except Exception:
                data = None
            finally:
                close = getattr(http, "close", None)
                if callable(close):
                    close()
        if not data:
            return JSONResponse({"error": "no_cover"}, status_code=404)
        return Response(content=data, media_type=sniff_image(data),
                        headers={"Cache-Control": "private, max-age=3600"})

    @r.post("/check")
    def check():
        entries = session.library_store.get_library()
        if not entries:
            return JSONResponse({"error": "empty_library"}, status_code=400)

        def body(tctx):
            from core.library_check import run_library_check

            with ctx.check_lock:
                for e in entries:
                    ctx.check_status[e.source_url] = {"state": "checking"}
            total = len(entries)

            def on_progress(current, total_, title):
                tctx.task.update(phase="checking", fraction=(current - 1) / max(total_, 1),
                                 message=f"Checking {current}/{total_}: {title[:40]}")

            def on_entry(url, st):
                with ctx.check_lock:
                    ctx.check_status[url] = dict(st)
                tctx.task.update()

            with_updates, n = run_library_check(entries, session.cache, force=True,
                                                on_progress=on_progress, on_entry=on_entry)
            msg = (f"{with_updates}/{n} have new chapters" if with_updates
                   else f"{n} novel{'s' if n != 1 else ''} up to date")
            try:
                from core.notify import notify

                if with_updates:
                    notify("Library updates available", msg)
            except Exception:
                pass
            return {"notes": msg, "with_updates": with_updates, "total": total}

        try:
            task = ctx.tasks.start("check", "Checking for new chapters", body)
        except Busy as exc:
            return busy_response(exc)
        return JSONResponse({"task_id": task.id}, status_code=202)

    @r.post("/update")
    def update(body: UrlsIn):
        entries = _entries_for(ctx, body.urls)
        if not entries:
            return JSONResponse({"error": "unknown_book"}, status_code=404)
        try:
            if len(entries) == 1:
                task = books.start_library_update(ctx.tasks, entries[0])
            else:
                task = books.start_library_update_many(ctx.tasks, entries, label="Update")
        except Busy as exc:
            return busy_response(exc)
        return JSONResponse({"task_id": task.id}, status_code=202)

    @r.post("/update-all")
    def update_all():
        entries = [
            e for e in session.library_store.get_library()
            if (ctx.check_status.get(e.source_url) or {}).get("state") == "update"
        ]
        if not entries:
            return JSONResponse({"error": "nothing_to_update"}, status_code=400)
        try:
            task = books.start_library_update_many(ctx.tasks, entries, label="Update All")
        except Busy as exc:
            return busy_response(exc)
        return JSONResponse({"task_id": task.id}, status_code=202)

    @r.post("/remove")
    def remove(body: UrlsIn):
        if ctx.tasks.is_busy():
            return busy_response(Busy())
        from core.library import purge_novel_artifacts
        from core.settings import get_default_books_dir

        urls = [(u or "").strip() for u in body.urls[:MAX_SELECTION] if (u or "").strip()]
        if not urls:
            return JSONResponse({"error": "nothing_selected"}, status_code=400)
        extra_dirs = [get_default_books_dir()]
        custom = (session.output_dir or "").strip()
        if custom:
            extra_dirs.append(Path(custom))
        removed = 0
        for url in urls:
            before = session.library_store.get_library_entry(url)
            gone = session.library_store.remove_library(url)
            target = gone or before
            if target:
                removed += 1
                purge_novel_artifacts(target, cache=session.cache, extra_dirs=extra_dirs,
                                      data_dir=session.data_dir)
            ctx.check_status.pop(url, None)
        _sync_drive_quietly(ctx)
        return {"removed": removed}

    @r.post("/reset")
    def reset():
        if ctx.tasks.is_busy():
            return busy_response(Busy())
        session.library_store.clear(clear_library=True, clear_history=False)
        ctx.check_status.clear()
        _sync_drive_quietly(ctx)
        return {"ok": True}

    @r.post("/epub")
    def epub(body: UrlIn):
        entry = session.library_store.get_library_entry((body.url or "").strip())
        if entry is None:
            return JSONResponse({"error": "unknown_book"}, status_code=404)
        local = find_local_epub(output_path=entry.output_path or "",
                                epub_filename=entry.epub_filename or "",
                                output_dir=session.output_dir or "")
        if local is not None:
            file = ctx.tasks.register_file(str(local))
            if file:
                return {"file": file}
        if entry.drive_file_id and _drive_connected(ctx):
            folder = downloads_folder(session.output_dir or "")
            dest = epub_path(folder, entry.title or "book",
                             preferred_name=entry.epub_filename or "")
            try:
                with ctx.tasks.exclusive("Downloading from Drive"):
                    saved = session.drive_sync.download_epub(entry.drive_file_id, dest,
                                                             allowed_root=folder)
            except Busy as exc:
                return busy_response(exc)
            except Exception as exc:
                return JSONResponse({"error": "drive_failed", "detail": str(exc)[:200]},
                                    status_code=502)
            file = ctx.tasks.register_file(str(saved))
            if file:
                return {"file": file}
        return JSONResponse({"error": "no_epub"}, status_code=404)

    @r.get("/history")
    def history():
        return {
            "items": [
                {"url": h.source_url, "title": h.translated_title or h.title,
                 "chapters": h.chapter_count, "at": h.downloaded_at}
                for h in session.library_store.get_history()[:20]
            ]
        }

    return r


def _drive_connected(ctx: ServerContext) -> bool:
    if not ctx.session.settings.get("drive_sync_enabled"):
        return False
    ds = getattr(ctx.session, "drive_sync", None)
    try:
        return bool(ds is not None and ds.is_connected())
    except Exception:
        return False


def _sync_drive_quietly(ctx: ServerContext) -> None:
    """Desktop rule: removing a book syncs Drive right away when it is on."""
    if not _drive_connected(ctx):
        return
    try:
        books.start_drive_sync(ctx.tasks)
    except Busy:
        pass


__all__ = ["build_router", "sniff_image", "busy_response"]
