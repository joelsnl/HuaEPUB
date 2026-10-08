# Author: joelsnl and Anthropic Claude
"""Read in the browser: the same book resolution and position file as the desktop.

A local EPUB wins; otherwise cached chapter HTML. A missing cached chapter is
fetched once (honouring the site's ``request_delay``), live-translated when
Translate is on, and the next chapter is prefetched in the background. The
position goes to the same ``reading.json`` the desktop Read tab uses.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.parser import get_parser_for_url
from core.reader import (
    KIND_CACHE,
    fetch_reader_chapter,
    html_needs_live_translate,
    live_translate_chapter,
    next_cache_prefetch_index,
    reader_fetch_waits,
    resolve_reader_book,
    resume_index,
    sanitize_reader_html,
)
from core.reading import get_bookmarks, get_position, set_position, toggle_bookmark
from web.tasks import Busy

MAX_OPEN_BOOKS = 6


@dataclass
class OpenBook:
    id: str
    book: object
    last_fetch: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class ReaderStore:
    def __init__(self, limit: int = MAX_OPEN_BOOKS):
        self._limit = limit
        self._items: "OrderedDict[str, OpenBook]" = OrderedDict()
        self._lock = threading.Lock()

    def put(self, book) -> OpenBook:
        item = OpenBook(id=secrets.token_urlsafe(9), book=book)
        with self._lock:
            self._items[item.id] = item
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        return item

    def get(self, book_id: str) -> Optional[OpenBook]:
        with self._lock:
            item = self._items.get(book_id)
            if item is not None:
                self._items.move_to_end(book_id)
            return item


class OpenIn(BaseModel):
    url: str = ""
    preview_id: str = ""


class PositionIn(BaseModel):
    index: int
    scroll: float = 0.0


def _site_delay(url: str, source_url: str) -> float:
    parser = get_parser_for_url(url) or get_parser_for_url(source_url)
    try:
        return float(getattr(parser, "request_delay", 2.0) or 2.0)
    except (TypeError, ValueError):
        return 2.0


def build_router(ctx) -> APIRouter:
    r = APIRouter(prefix="/api/read")
    session = ctx.session

    def fetch_chapter(item: OpenBook, index: int) -> str:
        """Fetch one chapter body from the site, waiting out the site delay."""
        book = item.book
        ch = book.chapters[index]
        delay = _site_delay(ch.url, book.source_url)
        if item.last_fetch:
            wait = delay - (time.monotonic() - item.last_fetch)
            if wait > 0:
                time.sleep(min(wait, 10.0))
        try:
            html = fetch_reader_chapter(session.cache, book.source_url, ch.url, ch.title)
        finally:
            item.last_fetch = time.monotonic()
        ch.html = html
        return html

    def live_translate(book, index: int) -> None:
        from web.options import job_options

        options = job_options(session.settings)
        if not options.get("translate"):
            return
        ch = book.chapters[index]
        if not html_needs_live_translate(ch.html or ""):
            return
        html, title = live_translate_chapter(
            ch.html or "", cache=session.cache, options=options, novel_title=book.title or "",
            detect_text=" ".join([book.title or ""] + [c.title or "" for c in book.chapters[:40]]),
            chapter_title=ch.title or "", chapter_url=ch.url or "", source_url=book.source_url or "",
        )
        ch.html = html
        if title and title != ch.title:
            ch.title = title

    def fetch_slot(chapter_url: str):
        """Hold the download slot only when this site is already being scraped."""
        job = session.control.active_job
        if reader_fetch_waits(job, session.control.is_downloading, chapter_url):
            return ctx.tasks.exclusive("Reading")
        return ctx.tasks.reader_turn("Reading")

    def prefetch_next(item: OpenBook, index: int) -> None:
        nxt = next_cache_prefetch_index(item.book, index)
        if nxt is None:
            return
        chapter_url = item.book.chapters[nxt].url or ""

        def run():
            try:
                with fetch_slot(chapter_url):
                    with item.lock:
                        if (item.book.chapters[nxt].html or "").strip():
                            return
                        fetch_chapter(item, nxt)
            except Busy:
                pass
            except Exception as exc:
                print(f"Reader prefetch skipped: {exc}")

        threading.Thread(target=run, name="reader-prefetch", daemon=True).start()

    @r.post("/open")
    def open_book(body: OpenIn):
        url = (body.url or "").strip()
        extra = None
        title = ""
        if body.preview_id:
            preview = ctx.previews.get(body.preview_id)
            if preview is None:
                return JSONResponse({"error": "preview_expired"}, status_code=404)
            url = preview.info.source_url or preview.url
            extra = preview.chapters
            title = preview.title_en or preview.info.title
        if not url:
            return JSONResponse({"error": "no_book"}, status_code=400)
        entry = session.library_store.get_library_entry(url)
        if entry is not None:
            title = entry.translated_title or entry.title or title
        kwargs = dict(
            source_url=url,
            title=title,
            output_path=(entry.output_path if entry else "") or "",
            epub_filename=(entry.epub_filename if entry else "") or "",
            output_dir=session.output_dir or "",
            cache=session.cache,
            extra_chapters=extra,
        )
        result = resolve_reader_book(**kwargs)
        if result.error or result.book is None:
            return JSONResponse({"error": "nothing_to_read",
                                 "detail": result.error or "Nothing to read yet."}, status_code=404)
        book = result.book
        item = ctx.readers.put(book)
        pos = get_position(book.source_url, data_dir=session.data_dir)
        idx = resume_index(book, pos)
        return {
            "book_id": item.id,
            "url": book.source_url,
            "title": book.title,
            "kind": book.kind,
            "index": idx,
            "scroll": float((pos or {}).get("scroll") or 0.0) if pos else 0.0,
            "chapters": [{"index": c.index, "title": c.title, "ready": bool((c.html or "").strip())}
                         for c in book.chapters],
            "bookmarks": get_bookmarks(book.source_url, data_dir=session.data_dir),
        }

    @r.get("/{book_id}/chapter/{index}")
    def chapter(book_id: str, index: int):
        item = ctx.readers.get(book_id)
        if item is None:
            return JSONResponse({"error": "book_closed"}, status_code=404)
        book = item.book
        if not 0 <= index < len(book.chapters):
            return JSONResponse({"error": "bad_index"}, status_code=400)
        ch = book.chapters[index]
        note = ""
        if not (ch.html or "").strip():
            if book.kind != KIND_CACHE or not ch.url:
                return JSONResponse({"error": "not_in_epub",
                                     "detail": "This chapter is not in the EPUB."}, status_code=404)
            try:
                with fetch_slot(ch.url or ""):
                    with item.lock:
                        if not (ch.html or "").strip():
                            fetch_chapter(item, index)
            except Busy as exc:
                return JSONResponse({"error": "busy", "label": exc.label,
                                     "detail": "Can't load this chapter yet."},
                                    status_code=409)
            except Exception as exc:
                return JSONResponse({"error": "fetch_failed", "detail": str(exc)[:200]},
                                    status_code=502)
        if book.kind == KIND_CACHE and html_needs_live_translate(ch.html or ""):
            try:
                with ctx.tasks.reader_turn("Translating chapter"):
                    with item.lock:
                        live_translate(book, index)
            except Busy:
                note = "Shown untranslated while another job runs."
            except Exception as exc:
                note = f"Translation failed: {str(exc)[:120]}"
        prefetch_next(item, index)
        return {"index": index, "title": ch.title, "html": sanitize_reader_html(ch.html or ""),
                "note": note}

    @r.post("/{book_id}/position")
    def position(book_id: str, body: PositionIn):
        item = ctx.readers.get(book_id)
        if item is None:
            return JSONResponse({"error": "book_closed"}, status_code=404)
        book = item.book
        if not book.source_url or not 0 <= body.index < len(book.chapters):
            return JSONResponse({"error": "bad_index"}, status_code=400)
        ch = book.chapters[body.index]
        set_position(book.source_url, chapter_url=(ch.url or ch.key), chapter_index=body.index,
                     scroll=min(1.0, max(0.0, float(body.scroll or 0.0))),
                     data_dir=session.data_dir)
        return {"ok": True}

    @r.post("/{book_id}/bookmark")
    def bookmark(book_id: str, body: PositionIn):
        item = ctx.readers.get(book_id)
        if item is None:
            return JSONResponse({"error": "book_closed"}, status_code=404)
        book = item.book
        if not book.source_url or not 0 <= body.index < len(book.chapters):
            return JSONResponse({"error": "bad_index"}, status_code=400)
        ch = book.chapters[body.index]
        marked = toggle_bookmark(
            book.source_url, chapter_url=(ch.url or ch.key), chapter_index=body.index,
            scroll=min(1.0, max(0.0, float(body.scroll or 0.0))), data_dir=session.data_dir,
        )
        return {"marked": marked, "bookmarks": get_bookmarks(book.source_url, data_dir=session.data_dir)}

    return r
