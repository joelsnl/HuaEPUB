# Author: joelsnl and Anthropic Claude
"""Settings, model installs and the systemd service, all edited from the browser."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from web import books
from web.context import ServerContext
from web.options import SettingsError, apply_settings, settings_payload
from web.tasks import Busy

# Display settings are safe to change while a job runs; everything else waits for it.
READER_SETTINGS = {
    "reader_font_pt", "reader_theme", "reader_mode", "reader_face", "reader_leading",
    "reader_align",
}


class InstallIn(BaseModel):
    what: str


def build_router(ctx: ServerContext) -> APIRouter:
    r = APIRouter()
    session = ctx.session

    @r.get("/api/settings")
    def get_settings():
        return settings_payload(session.settings)

    @r.put("/api/settings")
    async def put_settings(request: Request):
        try:
            changes = await request.json()
        except Exception:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        if not isinstance(changes, dict):
            return JSONResponse({"error": "bad_json"}, status_code=400)
        busy = ctx.tasks.active()
        if busy is not None and set(changes) - READER_SETTINGS:
            # A running job keeps the options it started with; change them after.
            raise Busy(busy.id, busy.label)
        try:
            apply_settings(session.settings, changes)
        except SettingsError as exc:
            return JSONResponse({"error": "bad_setting", "detail": str(exc)}, status_code=400)
        if "output_dir" in changes:
            session.output_dir = session.settings.get("output_dir") or ""
        return settings_payload(session.settings)

    @r.post("/api/install")
    def install_model(body: InstallIn):
        try:
            task = books.start_install(ctx.tasks, body.what)
        except ValueError:
            return JSONResponse({"error": "bad_install"}, status_code=400)
        return JSONResponse({"task_id": task.id}, status_code=202)

    @r.post("/api/service")
    def install_service():
        from web.host import install_user_service

        code, message = install_user_service()
        if code:
            return JSONResponse({"error": "service", "detail": message}, status_code=400)
        return {"ok": True, "message": message}

    return r
