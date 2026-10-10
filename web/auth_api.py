# Author: joelsnl and Anthropic Claude
"""Sign-in routes: session info, login, logout, the one-time open link and the login page."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.branding import APP_TITLE
from web.auth import COOKIE_NAME
from web.context import ServerContext
from web.guard import client_ip, set_session_cookie, signed_in

STATIC_DIR = Path(__file__).resolve().parent / "static"


class LoginIn(BaseModel):
    secret: str


def build_router(ctx: ServerContext) -> APIRouter:
    r = APIRouter()

    @r.get("/api/session")
    def session_info(request: Request):
        return {"name": APP_TITLE, "mode": ctx.mode, "signed_in": signed_in(ctx, request),
                "version": ctx.version if signed_in(ctx, request) else ""}

    @r.post("/api/login")
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
        set_session_cookie(ctx, resp)
        return resp

    @r.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @r.get("/open/{token}")
    def open_link(token: str):
        if not ctx.open_links.redeem(token):
            return RedirectResponse("/login", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        set_session_cookie(ctx, resp)
        return resp

    @r.get("/login")
    def login_page():
        return FileResponse(STATIC_DIR / "login.html")

    return r
