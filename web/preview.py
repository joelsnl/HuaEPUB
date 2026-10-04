# Author: joelsnl and Anthropic Claude
"""Chapter-list snapshots: LOOK resolves a URL to a book + TOC, BUILD uses it."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import parsers  # noqa: F401  (registers SiteConfigParser first, GenericParser last)
from core.cleaner import is_chinese
from core.parser import Chapter, NovelInfo, fetch_info_and_chapters, get_parser_for_url
from core.security import UnsafeURLError, validate_fetch_url

PREVIEW_TTL_SECONDS = 30 * 60
MAX_PREVIEWS = 64
PREVIEW_TRANSLATE_WORKERS = 50


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
    # English display copies only — the pipeline always translates from
    # ``info`` / ``chapters`` above, never from these. Empty = not translated.
    title_en: str = ""
    author_en: str = ""
    chapter_titles_en: List[str] = field(default_factory=list)
    has_cover: bool = False

    def to_payload(self) -> Dict[str, Any]:
        """Titles and positions only. Chapter URLs and HTML never leave the server."""
        titles_en = self.chapter_titles_en
        return {
            "preview_id": self.id,
            "title": self.info.title,
            "title_en": self.title_en,
            "author": self.info.author,
            "author_en": self.author_en,
            "chapter_count": len(self.chapters),
            "has_cover": self.has_cover,
            "chapters": [
                {
                    "index": pos,
                    "title": ch.title,
                    "title_en": titles_en[pos - 1] if pos - 1 < len(titles_en) else "",
                }
                for pos, ch in enumerate(self.chapters, 1)
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
        has_cover=bool(info.cover_url),
    )


def translate_preview(preview: Preview, *, cache: Any, workers: int = PREVIEW_TRANSLATE_WORKERS) -> None:
    """Fill in English title/author/chapter-title display copies, best-effort.

    Never raises and never touches ``preview.info`` or ``preview.chapters`` —
    the real pipeline translates those itself, from the original text, so a
    preview-only failure here can never change what gets built.
    """
    try:
        from core.translator import GoogleTranslator

        title_zh = is_chinese(preview.info.title)
        author_zh = is_chinese(preview.info.author)
        texts: List[str] = []
        if title_zh:
            texts.append(preview.info.title)
        if author_zh:
            texts.append(preview.info.author)
        chapter_indices = [i for i, ch in enumerate(preview.chapters) if is_chinese(ch.title)]
        texts.extend(preview.chapters[i].title for i in chapter_indices)
        if not texts:
            return

        translator = GoogleTranslator(max_workers=workers, persistent_cache=cache)
        results = translator.translate_texts(texts)

        pos = 0
        if title_zh:
            preview.title_en = results[pos] if pos < len(results) else ""
            pos += 1
        if author_zh:
            preview.author_en = results[pos] if pos < len(results) else ""
            pos += 1
        titles_en = [""] * len(preview.chapters)
        for i in chapter_indices:
            if pos < len(results):
                titles_en[i] = results[pos]
            pos += 1
        preview.chapter_titles_en = titles_en
    except Exception:
        # Display-only: a translate failure leaves the preview showing the
        # original text rather than failing the LOOK.
        pass
