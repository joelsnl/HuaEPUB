# Author: joelsnl and Anthropic Claude
"""HTTP routes for HuaEPUB Simple (FastAPI). Chapter work runs on a job thread."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi import Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.branding import SIMPLE_TITLE
from core.parser import create_http_session
from core.security import UnsafeURLError, fetch_cover_bytes
from web.jobs import TERMINAL_STATES, JobBusy, JobFinished, JobManager
from web.preview import PreviewError, PreviewStore, build_preview, translate_preview

STATIC_DIR = Path(__file__).resolve().parent / "static"
ACCESS_COOKIE = "huaepub_simple_code"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class PreviewIn(BaseModel):
    url: str


class JobIn(BaseModel):
    preview_id: str
    chapter_from: int
    chapter_to: int


def _host_name(host_header: str) -> str:
    host = (host_header or "").strip()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _host_allowed(host_header: str, lan: bool) -> bool:
    name = _host_name(host_header).lower()
    if name in LOCAL_HOSTS:
        return True
    if lan:
        try:
            addr = ipaddress.ip_address(name)
        except ValueError:
            return False
        return addr.is_private and not addr.is_loopback
    return False


def _same(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def create_app(
    *,
    manager: JobManager,
    previews: Optional[PreviewStore] = None,
    version: str = "",
    lan_code: Optional[str] = None,
    preview_builder=build_preview,
    cache: Any = None,
    preview_translator=translate_preview,
) -> FastAPI:
    app = FastAPI(title=SIMPLE_TITLE, docs_url=None, redoc_url=None, openapi_url=None)
    previews = previews or PreviewStore()
    lan = bool(lan_code)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not _host_allowed(request.headers.get("host", ""), lan):
            return PlainTextResponse("Unknown host", status_code=421)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                return JSONResponse({"error": "bad_origin"}, status_code=403)
        if lan_code:
            cookie = request.cookies.get(ACCESS_COOKIE) or ""
            if not cookie or not _same(cookie, lan_code):
                supplied = request.query_params.get("code") or ""
                if request.url.path == "/" and supplied and _same(supplied, lan_code):
                    resp = RedirectResponse("/", status_code=303)
                    resp.set_cookie(
                        ACCESS_COOKIE, lan_code, httponly=True, samesite="strict",
                        max_age=24 * 3600,
                    )
                    return resp
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"error": "access_code_required"}, status_code=401)
                return PlainTextResponse(
                    "Open the link printed in the terminal on the computer running HuaEPUB Simple.",
                    status_code=401,
                )
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return {"name": SIMPLE_TITLE, "version": version}

    @app.post("/api/preview")
    def preview(body: PreviewIn):
        busy = manager.active()
        if busy:
            return JSONResponse({"error": "busy", "job_id": busy.id}, status_code=409)
        try:
            item = preview_builder(body.url)
        except PreviewError as exc:
            payload = {"error": exc.code}
            if exc.detail:
                payload["detail"] = exc.detail
            return JSONResponse(payload, status_code=exc.status)
        preview_translator(item, cache=cache)
        previews.put(item)
        return item.to_payload()

    @app.get("/api/preview/{preview_id}/cover")
    def preview_cover(preview_id: str):
        item = previews.get(preview_id)
        if item is None or not item.info.cover_url:
            return JSONResponse({"error": "no_cover"}, status_code=404)
        session = create_http_session()
        try:
            data = fetch_cover_bytes(session, item.info.cover_url)
        except UnsafeURLError:
            return JSONResponse({"error": "no_cover"}, status_code=404)
        except Exception:
            return JSONResponse({"error": "cover_failed"}, status_code=502)
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        kind = "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            kind = "image/png"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            kind = "image/gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = "image/webp"
        return Response(
            content=data, media_type=kind,
            headers={"Cache-Control": "private, max-age=1800"},
        )

    @app.post("/api/jobs")
    def start_job(body: JobIn):
        item = previews.get(body.preview_id)
        if item is None:
            return JSONResponse({"error": "preview_expired"}, status_code=404)
        total = len(item.chapters)
        if not (1 <= body.chapter_from <= body.chapter_to <= total):
            return JSONResponse({"error": "bad_range"}, status_code=400)
        try:
            job = manager.start(item, body.chapter_from, body.chapter_to)
        except JobBusy as exc:
            return JSONResponse({"error": "busy", "job_id": exc.job_id}, status_code=409)
        return JSONResponse({"job_id": job.id, "state": job.state}, status_code=202)

    @app.get("/api/jobs/{job_id}")
    def job_state(job_id: str):
        job = manager.get(job_id)
        if job is None:
            return JSONResponse({"error": "unknown_job"}, status_code=404)
        return job.snapshot()

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str):
        job = manager.get(job_id)
        if job is None:
            return JSONResponse({"error": "unknown_job"}, status_code=404)

        async def stream():
            last = -1
            quiet = 0.0
            while True:
                snap = job.snapshot()
                if snap["rev"] != last:
                    last = snap["rev"]
                    quiet = 0.0
                    yield f"data: {json.dumps(snap)}\n\n"
                if snap["state"] in TERMINAL_STATES:
                    return
                await asyncio.sleep(0.4)
                quiet += 0.4
                if quiet >= 15:
                    quiet = 0.0
                    yield ": keepalive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        try:
            job = manager.cancel(job_id)
        except JobFinished:
            return JSONResponse({"error": "already_finished"}, status_code=409)
        if job is None:
            return JSONResponse({"error": "unknown_job"}, status_code=404)
        return JSONResponse({"state": "cancelling"}, status_code=202)

    @app.get("/api/jobs/{job_id}/epub")
    def job_epub(job_id: str):
        job = manager.epub_for(job_id)
        if job is None:
            return JSONResponse({"error": "not_ready"}, status_code=404)
        return FileResponse(
            job.path, media_type="application/epub+zip", filename=job.filename or "book.epub"
        )

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        try:
            found = manager.clear(job_id)
        except JobBusy:
            return JSONResponse({"error": "running"}, status_code=409)
        if not found:
            return JSONResponse({"error": "unknown_job"}, status_code=404)
        return JSONResponse(None, status_code=204)

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
