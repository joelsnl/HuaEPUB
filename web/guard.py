# Author: joelsnl and Anthropic Claude
"""Who may talk to the server, and the headers every answer carries.

Two modes, chosen by the desktop app:

- ``lan``: plain HTTP, only loopback and private-network clients, an access
  code (QR link) for sign-in.
- ``remote``: HTTPS only, any client, a password for sign-in.

Both use signed session cookies, sign-in rate limits, an Origin check plus a
custom header on every state-changing request, and a strict CSP.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from web.auth import COOKIE_NAME, LAN_COOKIE_DAYS, REMOTE_COOKIE_DAYS
from web.context import ServerContext

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


def host_name(host_header: str) -> str:
    host = (host_header or "").strip()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_private_or_local(ip_text: str) -> bool:
    try:
        addr = ipaddress.ip_address((ip_text or "").split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return bool(addr.is_loopback or addr.is_private) and not addr.is_unspecified


def lan_host_allowed(host_header: str) -> bool:
    """Block DNS rebinding: in LAN mode the Host must be an IP or localhost."""
    name = host_name(host_header).lower()
    if name in LOCAL_HOSTS:
        return True
    return is_private_or_local(name)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def signed_in(ctx: ServerContext, request: Request) -> bool:
    return ctx.secrets.cookie_valid(request.cookies.get(COOKIE_NAME) or "", ctx.mode)


def set_session_cookie(ctx: ServerContext, resp: Response) -> None:
    days = REMOTE_COOKIE_DAYS if ctx.remote else LAN_COOKIE_DAYS
    resp.set_cookie(
        COOKIE_NAME, ctx.secrets.make_cookie(ctx.mode), httponly=True, samesite="strict",
        secure=ctx.https, max_age=days * 86400, path="/",
    )


def harden(ctx: ServerContext, response: Response, request: Request) -> Response:
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


def install_guard(app: FastAPI, ctx: ServerContext) -> None:
    """Put the request checks in front of every route."""

    @app.middleware("http")
    async def guard(request: Request, call_next):
        ip = client_ip(request)
        if not ctx.remote:
            if not is_private_or_local(ip):
                return PlainTextResponse("This server only answers devices on its own network.",
                                         status_code=403)
            if not lan_host_allowed(request.headers.get("host", "")):
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
        if (path not in PUBLIC_PATHS and not path.startswith("/open/")
                and not signed_in(ctx, request)):
            code = request.query_params.get("code") or ""
            if path == "/" and code and not ctx.remote:
                verdict = ctx.limiter.check(ip)
                if verdict.allowed and ctx.secrets.check_code(code):
                    ctx.limiter.succeeded(ip)
                    resp = RedirectResponse("/", status_code=303)
                    set_session_cookie(ctx, resp)
                    return harden(ctx, resp, request)
                if verdict.allowed:
                    ctx.limiter.failed(ip)
                    print(f"Server: wrong access code from {ip}")
            if path.startswith("/api/"):
                return harden(ctx, JSONResponse({"error": "sign_in_required"}, status_code=401),
                              request)
            return harden(ctx, RedirectResponse("/login", status_code=303), request)
        response = await call_next(request)
        return harden(ctx, response, request)
