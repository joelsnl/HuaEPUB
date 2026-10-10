# Author: joelsnl and Anthropic Claude
"""Add a book (Single) and Add several (Multi): look up a link, then build the EPUB."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.security import UnsafeURLError
from web import books
from web.context import ServerContext
from web.library_api import fetch_and_cache_cover, sniff_image
from web.preview import PreviewError
from web.tasks import Busy


class PreviewIn(BaseModel):
    url: str


class SingleIn(BaseModel):
    preview_id: str
    chapter_from: int
    chapter_to: int


class LookupIn(BaseModel):
    urls: List[str]


class MultiIn(BaseModel):
    preview_ids: List[str]


def build_router(ctx: ServerContext) -> APIRouter:
    r = APIRouter()
    session = ctx.session

    @r.post("/api/preview")
    def preview(body: PreviewIn):
        if ctx.tasks.is_busy():
            busy = ctx.tasks.active()
            raise Busy(busy.id if busy else "", busy.label if busy else "")
        try:
            item = ctx.preview_builder(body.url)
        except PreviewError as exc:
            payload = {"error": exc.code}
            if exc.detail:
                payload["detail"] = exc.detail
            return JSONResponse(payload, status_code=exc.status)
        if session.settings.get("translate", True):
            ctx.preview_translator(item, cache=session.cache)
        try:
            session.cache.put_chapter_list(item.url, item.chapters)
        except Exception:
            pass
        ctx.previews.put(item)
        payload = item.to_payload()
        source_url = item.info.source_url or item.url
        payload["in_library"] = session.library_store.get_library_entry(source_url) is not None
        payload["shelved"] = books.shelved_note(session.library_store, source_url, item.info.title)
        return payload

    @r.get("/api/preview/{preview_id}/cover")
    def preview_cover(preview_id: str):
        item = ctx.previews.get(preview_id)
        if item is None or not item.info.cover_url:
            return JSONResponse({"error": "no_cover"}, status_code=404)
        try:
            data = fetch_and_cache_cover(session.cache, item.info.cover_url,
                                         item.info.source_url or item.url, timeout=20)
        except UnsafeURLError:
            return JSONResponse({"error": "no_cover"}, status_code=404)
        except Exception:
            return JSONResponse({"error": "cover_failed"}, status_code=502)
        return Response(content=data, media_type=sniff_image(data),
                        headers={"Cache-Control": "private, max-age=1800"})

    @r.post("/api/single")
    def single(body: SingleIn):
        item = ctx.previews.get(body.preview_id)
        if item is None:
            return JSONResponse({"error": "preview_expired"}, status_code=404)
        if not (1 <= body.chapter_from <= body.chapter_to <= len(item.chapters)):
            return JSONResponse({"error": "bad_range"}, status_code=400)
        task = books.start_single(ctx.tasks, item, body.chapter_from, body.chapter_to)
        return JSONResponse({"task_id": task.id}, status_code=202)

    @r.post("/api/multi/lookup")
    def multi_lookup(body: LookupIn):
        from core.utils import extract_urls

        urls: List[str] = []
        for raw in body.urls:
            for u in extract_urls(raw or "") or [(raw or "").strip()]:
                if u and u not in urls:
                    urls.append(u)
        urls = [u for u in urls if u]
        if not urls:
            return JSONResponse({"error": "no_links"}, status_code=400)
        if len(urls) > books.MAX_MULTI:
            return JSONResponse({"error": "too_many_links", "max": books.MAX_MULTI},
                                status_code=400)
        task = books.start_lookup(ctx.tasks, urls, ctx.previews,
                                  preview_builder=ctx.preview_builder,
                                  preview_translator=ctx.preview_translator)
        return JSONResponse({"task_id": task.id}, status_code=202)

    @r.post("/api/multi/build")
    def multi_build(body: MultiIn):
        items = [ctx.previews.get(pid) for pid in body.preview_ids[: books.MAX_MULTI]]
        items = [p for p in items if p is not None]
        if not items:
            return JSONResponse({"error": "preview_expired"}, status_code=404)
        task = books.start_multi(ctx.tasks, items)
        return JSONResponse({"task_id": task.id}, status_code=202)

    return r
