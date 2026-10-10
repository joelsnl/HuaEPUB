# Author: joelsnl and Anthropic Claude
"""What the server is doing now: shared state, the event stream, pause / cancel, files, resume."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from web import books
from web.context import ServerContext, book_roots
from web.storage import memory_payload, storage_payload
from web.tasks import Busy


def build_router(ctx: ServerContext) -> APIRouter:
    r = APIRouter()
    session = ctx.session

    def state_payload():
        return {
            "task": ctx.tasks.snapshot(),
            "busy": ctx.tasks.is_busy(),
            "resume": None if ctx.tasks.is_busy() else books.resume_payload(session.data_dir),
        }

    @r.get("/api/state")
    def state():
        payload = state_payload()
        payload.update({"version": ctx.version, "mode": ctx.mode})
        return payload

    @r.get("/api/storage")
    def storage_state():
        places = [("books folder", root) for root in book_roots(session)]
        places.append(("app data", session.data_dir))
        payload = storage_payload(places)
        if payload is None:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        ram = memory_payload()
        if ram is not None:
            payload["ram"] = ram
        return payload

    @r.get("/api/events")
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

    @r.post("/api/task/pause")
    def pause():
        if ctx.tasks.active() is None:
            return JSONResponse({"error": "no_task"}, status_code=409)
        return {"paused": ctx.tasks.pause()}

    @r.post("/api/task/cancel")
    def cancel():
        if not ctx.tasks.cancel():
            return JSONResponse({"error": "no_task"}, status_code=409)
        return JSONResponse({"state": "cancelling"}, status_code=202)

    @r.get("/api/files/{token}")
    def file(token: str):
        path = ctx.tasks.file_for(token)
        if path is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return FileResponse(path, media_type="application/epub+zip", filename=path.name)

    @r.post("/api/resume")
    def resume():
        try:
            task = books.start_resume(ctx.tasks)
        except books.ResumeError as exc:
            return JSONResponse({"error": "cannot_resume", "detail": str(exc)}, status_code=400)
        return JSONResponse({"task_id": task.id}, status_code=202)

    @r.post("/api/resume/discard")
    def discard():
        if ctx.tasks.is_busy():
            raise Busy()
        from core.download_job import clear_job

        clear_job(session.data_dir)
        session.control.active_job = None
        return {"ok": True}

    return r
