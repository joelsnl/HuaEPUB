# Author: joelsnl and Anthropic Claude
"""Reading stats route: one read-only endpoint for the browser's Stats page."""

from __future__ import annotations

from fastapi import APIRouter

from core.reading_stats import compute_reading_stats
from web.context import ServerContext


def build_router(ctx: ServerContext) -> APIRouter:
    r = APIRouter(prefix="/api/stats")
    session = ctx.session

    @r.get("")
    def stats():
        return compute_reading_stats(session)

    return r
