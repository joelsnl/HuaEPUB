# Author: joelsnl and Anthropic Claude
"""HTTP app for HuaEPUB server mode (FastAPI).

This module only wires the app together. The request checks live in ``web/guard.py``
and each group of routes in its own module (``auth_api``, ``task_api``, ``download_api``,
``settings_api``, ``library_api``, ``reader_api``). Work runs on one shared task slot
(``web/tasks.py``).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.branding import APP_TITLE
from web import auth_api, download_api, library_api, reader_api, settings_api, task_api
from web.auth_api import STATIC_DIR
from web.context import ServerContext
from web.guard import install_guard
from web.tasks import Busy


def create_app(ctx: ServerContext) -> FastAPI:
    app = FastAPI(title=APP_TITLE, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Busy)
    async def busy(_request: Request, exc: Busy):
        # Routes that need the task slot raise Busy; this is the one answer for all of them.
        return JSONResponse({"error": "busy", "task_id": exc.task_id, "label": exc.label},
                            status_code=409)

    install_guard(app, ctx)
    for module in (auth_api, task_api, download_api, settings_api, library_api, reader_api):
        app.include_router(module.build_router(ctx))

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
