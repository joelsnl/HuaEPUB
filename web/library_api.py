# Author: joelsnl and Anthropic Claude
"""Library routes: list, covers, check, update, remove, reset, Download EPUB."""

from __future__ import annotations

import html
import re
from typing import List

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from core.cache import english_chapter_title
from core.cleaner import is_chinese
from core.parser import create_http_session
from core.reader import find_local_epub, load_epub_chapters
from core.reading import reading_marks
from core.security import fetch_cover_bytes
from web import books
from web.context import ServerContext
from web.tasks import Busy

MAX_SELECTION = 500
# The card's second line is the Chinese title. Longer than this, it is a
# blurb (or a runaway translation) and would stretch the shelf.
_ORIGINAL_TITLE_MAX = 120
_DESCRIPTION_MAX = 800


class UrlsIn(BaseModel):
    urls: List[str]


class UrlIn(BaseModel):
    url: str


def fetch_and_cache_cover(cache, cover_url: str, source_url: str, *, timeout: float) -> bytes:
    """Fetch a cover through the SSRF guard and keep it in the cover cache. Raises on failure."""
    http = create_http_session()
    try:
        data = fetch_cover_bytes(http, cover_url, timeout=timeout)
    finally:
        close = getattr(http, "close", None)
        if callable(close):
            close()
    try:
        cache.put_cover(data, cover_url=cover_url, source_url=source_url)
    except Exception:
        pass
    return data


def sniff_image(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def card_original_title(title: str, translated: str) -> str:
    """Source title for the library card, or "" when it should not be shown.

    ``title`` is sometimes a second English rendering of ``translated``
    (the pipeline translation diverged, often into a repeated paragraph).
    That string is not the Chinese title, and drawing it opens the card
    to the height of the whole paragraph.
    """
    raw = " ".join((title or "").split())
    shown = " ".join((translated or "").split())
    if not raw or not shown or raw == shown or not is_chinese(raw):
        return ""
    if len(raw) > _ORIGINAL_TITLE_MAX:
        return raw[:_ORIGINAL_TITLE_MAX].rstrip()
    return raw


_BREAK = re.compile(r"<br\s*/?>|</p\s*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def card_description(text: str) -> str:
    """Synopsis for the detail view: site markup becomes plain lines, capped so a
    runaway paragraph stays in the dialog."""
    plain = html.unescape(_TAG.sub("", _BREAK.sub("\n", text or "")))
    lines = (" ".join(line.split()) for line in plain.split("\n"))
    raw = "\n".join(line for line in lines if line)
    if len(raw) > _DESCRIPTION_MAX:
        return raw[:_DESCRIPTION_MAX].rstrip()
    return raw


def _entry_payload(ctx: ServerContext, e, mark=None) -> dict:
    read_index, read_at = mark if mark else (-1, 0.0)
    st = ctx.check_status.get(e.source_url) or {}
    local = find_local_epub(
        output_path=e.output_path or "",
        epub_filename=e.epub_filename or "",
        output_dir=ctx.session.output_dir or "",
    )
    return {
        "url": e.source_url,
        "title": e.translated_title or e.title or e.source_url,
        "title_original": card_original_title(e.title, e.translated_title or ""),
        "description": card_description(getattr(e, "description", "") or ""),
        "author": e.author or "",
        "chapters": int(e.chapter_count or 0),
        "last_chapter": e.last_chapter_title or "",
        "updated_at": float(e.last_downloaded_at or 0),
        "has_cover": bool(e.cover_url),
        "has_epub": local is not None,
        "status": st.get("state") or "",
        "new_count": int(st.get("new_count") or 0),
        "status_error": st.get("error") or "",
        # 1-based chapter the reader is on (0 = not opened yet), from reading.json.
        "read_chapter": read_index + 1 if read_index >= 0 else 0,
        "read_at": float(read_at) if read_index >= 0 else 0.0,
    }


def _toc_rows(session, source_url: str) -> List[dict]:
    try:
        return list(session.cache.get_chapter_list(source_url) or [])
    except Exception:
        return []


def _stored_english(session, source_url: str):
    getter = getattr(session.cache, "get_english_chapter_titles", None)
    if not callable(getter):
        return None
    try:
        return getter(source_url)
    except Exception:
        return None


def _epub_titles(session, entry) -> List[str]:
    local = find_local_epub(
        output_path=entry.output_path or "",
        epub_filename=entry.epub_filename or "",
        output_dir=session.output_dir or "",
    )
    if local is None:
        return []
    try:
        return [ch.title for ch in load_epub_chapters(local, bodies=False)]
    except Exception:
        return []


def _backfill_english_from_epub(session, entry, toc: List[dict]):
    """Read a local translated EPUB once and keep its English titles by chapter URL."""
    if not toc:
        return None
    local = find_local_epub(
        output_path=entry.output_path or "",
        epub_filename=entry.epub_filename or "",
        output_dir=session.output_dir or "",
    )
    if local is None:
        return None
    try:
        chapters = load_epub_chapters(local, bodies=False)
    except Exception:
        return None
    if len(chapters) != len(toc):
        return None
    rows = []
    for item, chapter in zip(toc, chapters):
        url = (item.get("url") or "").strip()
        if not url:
            return None
        rows.append({"url": url, "title": english_chapter_title(chapter.title)})
    put = getattr(session.cache, "put_english_chapter_titles", None)
    if callable(put):
        try:
            put(entry.source_url, rows)
        except Exception:
            pass
    return rows


def chapter_rows(session, entry) -> List[dict]:
    """Titles for the detail list. English names when a translated build stored them."""
    toc = _toc_rows(session, entry.source_url)
    stored = _stored_english(session, entry.source_url)
    if stored is None and toc:
        stored = _backfill_english_from_epub(session, entry, toc)
    by_url = {}
    if stored:
        for row in stored:
            title = (row.get("title") or "").strip()
            url = (row.get("url") or "").strip()
            if url and title:
                by_url[url] = title
    if toc:
        titles = [
            by_url.get((item.get("url") or "").strip()) or (item.get("title") or "").strip()
            for item in toc
        ]
    elif stored and any((row.get("title") or "").strip() for row in stored):
        titles = [(row.get("title") or "").strip() for row in stored]
    else:
        titles = _epub_titles(session, entry)
    return [{"n": i + 1, "title": title or f"Chapter {i + 1}"} for i, title in enumerate(titles)]


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
        marks = reading_marks(data_dir=session.data_dir)
        return {
            "entries": [_entry_payload(ctx, e, marks.get(e.source_url)) for e in entries],
            "checking": bool(check and check.kind == "check"),
        }

    @r.get("/chapters")
    def chapters(u: str = Query(..., max_length=2048)):
        entry = session.library_store.get_library_entry(u)
        if entry is None:
            return JSONResponse({"error": "unknown_book"}, status_code=404)
        return {"chapters": chapter_rows(session, entry)}

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
            try:
                data = fetch_and_cache_cover(session.cache, entry.cover_url, entry.source_url,
                                             timeout=15)
            except Exception:
                data = None
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

        task = ctx.tasks.start("check", "Checking for new chapters", body)
        return JSONResponse({"task_id": task.id}, status_code=202)

    @r.post("/update")
    def update(body: UrlsIn):
        entries = _entries_for(ctx, body.urls)
        if not entries:
            return JSONResponse({"error": "unknown_book"}, status_code=404)
        if len(entries) == 1:
            task = books.start_library_update(ctx.tasks, entries[0])
        else:
            task = books.start_library_update_many(ctx.tasks, entries, label="Update")
        return JSONResponse({"task_id": task.id}, status_code=202)

    @r.post("/update-all")
    def update_all():
        entries = [
            e for e in session.library_store.get_library()
            if (ctx.check_status.get(e.source_url) or {}).get("state") == "update"
        ]
        if not entries:
            return JSONResponse({"error": "nothing_to_update"}, status_code=400)
        task = books.start_library_update_many(ctx.tasks, entries, label="Update All")
        return JSONResponse({"task_id": task.id}, status_code=202)

    @r.post("/remove")
    def remove(body: UrlsIn):
        if ctx.tasks.is_busy():
            raise Busy()
        from core.library import remove_and_purge

        urls = [(u or "").strip() for u in body.urls[:MAX_SELECTION] if (u or "").strip()]
        if not urls:
            return JSONResponse({"error": "nothing_selected"}, status_code=400)
        removed = remove_and_purge(session.library_store, urls, cache=session.cache,
                                   output_dir=session.output_dir, data_dir=session.data_dir)
        for url in urls:
            ctx.check_status.pop(url, None)
        return {"removed": removed}

    @r.post("/reset")
    def reset():
        if ctx.tasks.is_busy():
            raise Busy()
        session.library_store.clear(clear_library=True, clear_history=False)
        ctx.check_status.clear()
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


__all__ = [
    "build_router", "card_description", "card_original_title", "chapter_rows",
    "fetch_and_cache_cover", "sniff_image",
]
