# Author: joelsnl and Anthropic Claude
"""Chapter-list snapshots: LOOK resolves a URL to a book + TOC, BUILD uses it."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import parsers  # noqa: F401  (registers SiteConfigParser first, GenericParser last)
from core.parser import Chapter, NovelInfo, fetch_info_and_chapters, get_parser_for_url
from core.security import UnsafeURLError, validate_fetch_url

PREVIEW_TTL_SECONDS = 30 * 60
MAX_PREVIEWS = 8


class PreviewError(Exception):
    """A LOOK that cannot become a preview. ``code`` is the API error key."""

    def __init__(self, code: str, status: int, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.status = status
        self.detail = detail


@dataclass
class Preview:
    id: str
    url: str
    parser: Any
    info: NovelInfo
    chapters: List[Chapter]
    created_at: float = field(default_factory=time.monotonic)

    def to_payload(self) -> Dict[str, Any]:
        """Titles and positions only. Chapter URLs and HTML never leave the server."""
        return {
            "preview_id": self.id,
            "title": self.info.title,
            "author": self.info.author,
            "chapter_count": len(self.chapters),
            "chapters": [
                {"index": pos, "title": ch.title} for pos, ch in enumerate(self.chapters, 1)
            ],
        }


class PreviewStore:
    def __init__(self, ttl: float = PREVIEW_TTL_SECONDS, limit: int = MAX_PREVIEWS):
        self._ttl = ttl
        self._limit = limit
        self._items: Dict[str, Preview] = {}
        self._lock = threading.Lock()

    def _purge(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._items.items() if now - v.created_at > self._ttl]:
            del self._items[key]
        while len(self._items) > self._limit:
            oldest = min(self._items.values(), key=lambda v: v.created_at)
            del self._items[oldest.id]

    def put(self, preview: Preview) -> None:
        with self._lock:
            self._items[preview.id] = preview
            self._purge()

    def get(self, preview_id: str) -> Optional[Preview]:
        with self._lock:
            self._purge()
            return self._items.get(preview_id)


def _short(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text[:160]


def build_preview(url: str) -> Preview:
    """Validate the URL, pick a parser, load info and the chapter list (no bodies)."""
    url = (url or "").strip()
    try:
        validate_fetch_url(url)
    except UnsafeURLError as exc:
        raise PreviewError("blocked_url", 400, _short(exc))
    parser = get_parser_for_url(url)
    if parser is None:
        raise PreviewError("no_parser", 400)
    try:
        info, chapters = fetch_info_and_chapters(parser, url)
    except Exception as exc:
        raise PreviewError("fetch_failed", 502, _short(exc))
    if not chapters:
        raise PreviewError("no_chapters", 400)
    if not info.source_url:
        info.source_url = url
    return Preview(
        id=secrets.token_urlsafe(12),
        url=url,
        parser=parser,
        info=info,
        chapters=list(chapters),
    )
