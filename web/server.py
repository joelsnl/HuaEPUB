# Author: joelsnl and Anthropic Claude
"""HTTP app for HuaEPUB server mode (FastAPI).

Two modes, chosen by the desktop app:

- ``lan``: plain HTTP, only loopback and private-network clients, an access
  code (QR link) for sign-in.
- ``remote``: HTTPS only, any client, a password for sign-in.

Both use signed session cookies, sign-in rate limits, an Origin check plus a
custom header on every state-changing request, and a strict CSP. Work runs on
one shared task slot (``web/tasks.py``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi import Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.branding import APP_TITLE
from core.security import UnsafeURLError
from web import books
from web.auth import COOKIE_NAME, LAN_COOKIE_DAYS, REMOTE_COOKIE_DAYS
from web.context import ServerContext
from web.library_api import build_router as library_router
from web.library_api import fetch_and_cache_cover, sniff_image
from web.options import SettingsError, apply_settings, settings_payload
from web.preview import PreviewError
from web.reader_api import build_router as reader_router
from web.tasks import Busy

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
MAX_BODY_BYTES = 256 * 1024
CSRF_HEADER = "x-huaepub"

CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

# Reachable without a session: the sign-in page and what it needs.
PUBLIC_PATHS = {
    "/login", "/api/login", "/api/session",
    "/static/login.js", "/static/app.css", "/static/theme.js", "/static/icon.svg",
}


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


class InstallIn(BaseModel):
    what: str


class LoginIn(BaseModel):
    secret: str


def _host_name(host_header: str) -> str:
    host = (host_header or "").strip()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _is_private_or_local(ip_text: str) -> bool:
    try:
        addr = ipaddress.ip_address((ip_text or "").split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return bool(addr.is_loopback or addr.is_private) and not addr.is_unspecified


def _lan_host_allowed(host_header: str) -> bool:
    """Block DNS rebinding: in LAN mode the Host must be an IP or localhost."""
    name = _host_name(host_header).lower()
    if name in LOCAL_HOSTS:
        return True
    return _is_private_or_local(name)


def create_app(ctx: ServerContext) -> FastAPI:
    app = FastAPI(title=APP_TITLE, docs_url=None, redoc_url=None, openapi_url=None)
    session = ctx.session

    def signed_in(request: Request) -> bool:
        return ctx.secrets.cookie_valid(request.cookies.get(COOKIE_NAME) or "", ctx.mode)

    def set_session_cookie(resp: Response) -> None:
        days = REMOTE_COOKIE_DAYS if ctx.remote else LAN_COOKIE_DAYS
        resp.set_cookie(
            COOKIE_NAME, ctx.secrets.make_cookie(ctx.mode), httponly=True, samesite="strict",
            secure=ctx.https, max_age=days * 86400, path="/",
        )

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else ""

    @app.exception_handler(Busy)
    async def busy(_request: Request, exc: Busy):
        # Routes that need the task slot raise Busy; this is the one answer for all of them.
        return JSONResponse({"error": "busy", "task_id": exc.task_id, "label": exc.label},
                            status_code=409)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        ip = client_ip(request)
        if not ctx.remote:
            if not _is_private_or_local(ip):
                return PlainTextResponse("This server only answers devices on its own network.",
                                         status_code=403)
            if not _lan_host_allowed(request.headers.get("host", "")):
                return PlainTextResponse("Unknown host", status_code=421)
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return JSONResponse({"error": "too_large"}, status_code=413)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                return JSONResponse({"error": "bad_origin"}, status_code=403)
            if request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"error": "missing_header"}, status_code=403)
        path = request.url.path
        if path not in PUBLIC_PATHS and not path.startswith("/open/") and not signed_in(request):
            code = request.query_params.get("code") or ""
            if path == "/" and code and not ctx.remote:
                verdict = ctx.limiter.check(ip)
                if verdict.allowed and ctx.secrets.check_code(code):
                    ctx.limiter.succeeded(ip)
                    resp = RedirectResponse("/", status_code=303)
                    set_session_cookie(resp)
                    return _harden(resp, request)
                if verdict.allowed:
                    ctx.limiter.failed(ip)
                    print(f"Server: wrong access code from {ip}")
            if path.startswith("/api/"):
                return _harden(JSONResponse({"error": "sign_in_required"}, status_code=401), request)
            return _harden(RedirectResponse("/login", status_code=303), request)
        response = await call_next(request)
        return _harden(response, request)

    def _harden(response: Response, request: Request) -> Response:
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if ctx.hsts:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        path = request.url.path
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif path in ("/", "/login") or path.startswith("/static/"):
            # Revalidate (ETag) on every load so an app update never leaves a phone on stale pages.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # -- sign-in -------------------------------------------------------------

    @app.get("/api/session")
    def session_info(request: Request):
        return {"name": APP_TITLE, "mode": ctx.mode, "signed_in": signed_in(request),
                "version": ctx.version if signed_in(request) else ""}

    @app.post("/api/login")
    def login(body: LoginIn, request: Request):
        ip = client_ip(request)
        verdict = ctx.limiter.check(ip)
        if not verdict.allowed:
            return JSONResponse({"error": "too_many_tries", "retry_after": verdict.retry_after},
                                status_code=429, headers={"Retry-After": str(verdict.retry_after)})
        secret = (body.secret or "")[:256]
        ok = ctx.secrets.check_password(secret) if ctx.remote else ctx.secrets.check_code(secret)
        if not ok:
            ctx.limiter.failed(ip)
            print(f"Server: failed sign-in from {ip}")
            return JSONResponse({"error": "wrong_secret"}, status_code=401)
        ctx.limiter.succeeded(ip)
        resp = JSONResponse({"ok": True})
        set_session_cookie(resp)
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/open/{token}")
    def open_link(token: str, request: Request):
        if not ctx.open_links.redeem(token):
            return RedirectResponse("/login", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        set_session_cookie(resp)
        return resp

    @app.get("/login")
    def login_page():
        return FileResponse(STATIC_DIR / "login.html")

    # -- shared state --------------------------------------------------------

    def state_payload():
        return {
            "task": ctx.tasks.snapshot(),
            "busy": ctx.tasks.is_busy(),
            "resume": None if ctx.tasks.is_busy() else books.resume_payload(session.data_dir),
        }

    @app.get("/api/state")
    def state():
        payload = state_payload()
        payload.update({"version": ctx.version, "mode": ctx.mode})
        return payload

    @app.get("/api/events")
    async def events():
        async def stream():
            last = None
            quiet = 0.0
            for _ in range(60 * 60 * 3):  # reconnects after ~30 minutes
                payload = state_payload()
                key = json.dumps(payload, sort_keys=True)
                if key != last:
                    last = key
                    quiet = 0.0
                    yield f"data: {key}\n\n"
                await asyncio.sleep(0.5)
                quiet += 0.5
                if quiet >= 15:
                    quiet = 0.0
                    yield ": keepalive\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/task/pause")
    def pause():
        if ctx.tasks.active() is None:
            return JSONResponse({"error": "no_task"}, status_code=409)
        return {"paused": ctx.tasks.pause()}

    @app.post("/api/task/cancel")
    def cancel():
        if not ctx.tasks.cancel():
            return JSONResponse({"error": "no_task"}, status_code=409)
        return JSONResponse({"state": "cancelling"}, status_code=202)

    @app.get("/api/files/{token}")
    def file(token: str):
        path = ctx.tasks.file_for(token)
        if path is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return FileResponse(path, media_type="application/epub+zip", filename=path.name)

    @app.post("/api/resume")
    def resume():
        try:
            task = books.start_resume(ctx.tasks)
        except books.ResumeError as exc:
            return JSONResponse({"error": "cannot_resume", "detail": str(exc)}, status_code=400)
        return JSONResponse({"task_id": task.id}, status_code=202)

    @app.post("/api/resume/discard")
    def discard():
        if ctx.tasks.is_busy():
            raise Busy()
        from core.download_job import clear_job

        clear_job(session.data_dir)
        session.control.active_job = None
        return {"ok": True}

    # -- Single --------------------------------------------------------------

    @app.post("/api/preview")
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
        entry = session.library_store.get_library_entry(item.info.source_url or item.url)
        payload["in_library"] = entry is not None
        return payload

    @app.get("/api/preview/{preview_id}/cover")
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

    @app.post("/api/single")
    def single(body: SingleIn):
        item = ctx.previews.get(body.preview_id)
        if item is None:
            return JSONResponse({"error": "preview_expired"}, status_code=404)
        if not (1 <= body.chapter_from <= body.chapter_to <= len(item.chapters)):
            return JSONResponse({"error": "bad_range"}, status_code=400)
        task = books.start_single(ctx.tasks, item, body.chapter_from, body.chapter_to)
        return JSONResponse({"task_id": task.id}, status_code=202)

    # -- Multi ---------------------------------------------------------------

    @app.post("/api/multi/lookup")
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

    @app.post("/api/multi/build")
    def multi_build(body: MultiIn):
        items = [ctx.previews.get(pid) for pid in body.preview_ids[: books.MAX_MULTI]]
        items = [p for p in items if p is not None]
        if not items:
            return JSONResponse({"error": "preview_expired"}, status_code=404)
        task = books.start_multi(ctx.tasks, items)
        return JSONResponse({"task_id": task.id}, status_code=202)

    # -- Settings ------------------------------------------------------------

    @app.get("/api/settings")
    def get_settings():
        return settings_payload(session.settings)

    @app.put("/api/settings")
    async def put_settings(request: Request):
        try:
            changes = await request.json()
        except Exception:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        if not isinstance(changes, dict):
            return JSONResponse({"error": "bad_json"}, status_code=400)
        busy = ctx.tasks.active()
        if busy is not None and set(changes) - {
            "reader_font_pt", "reader_theme", "reader_mode", "reader_face",
            "reader_leading", "reader_align",
        }:
            # A running job keeps the options it started with; change them after.
            raise Busy(busy.id, busy.label)
        try:
            apply_settings(session.settings, changes)
        except SettingsError as exc:
            return JSONResponse({"error": "bad_setting", "detail": str(exc)}, status_code=400)
        if "output_dir" in changes:
            session.output_dir = session.settings.get("output_dir") or ""
        return settings_payload(session.settings)

    @app.post("/api/install")
    def install_model(body: InstallIn):
        try:
            task = books.start_install(ctx.tasks, body.what)
        except ValueError:
            return JSONResponse({"error": "bad_install"}, status_code=400)
        return JSONResponse({"task_id": task.id}, status_code=202)

    @app.post("/api/service")
    def install_service():
        from web.host import install_user_service

        code, message = install_user_service()
        if code:
            return JSONResponse({"error": "service", "detail": message}, status_code=400)
        return {"ok": True, "message": message}

    app.include_router(library_router(ctx))
    app.include_router(reader_router(ctx))

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
