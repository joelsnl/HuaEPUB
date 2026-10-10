# Author: joelsnl and Anthropic Claude
"""
Resolve a novel for the in-app reader: local EPUB first, else cached TOC/HTML.

Chapter HTML is sanitized for QTextBrowser (no scripts, no navigation).
The one-chapter site fetch and the live translate are shared by the desktop
Read tab and the browser reader.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

from lxml import html as lxml_html

from core.download_runner import downloads_folder
from core.epub_builder import CHAPTER_MAP_NAME
from core.security import is_allowed_epub_path, safe_epub_basename
from core.settings import get_default_books_dir

KIND_EPUB = "epub"
KIND_CACHE = "cache"

_DROP_TAGS = frozenset({
    "script", "style", "iframe", "object", "embed", "form", "link", "meta",
    "base", "applet", "noscript",
})
_SKIP_EPUB_NAME = re.compile(
    r"(nav|toc|ncx|cover|titlepage)\.(xhtml|html|xml)$",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.IGNORECASE | re.DOTALL)


@dataclass
class ReaderChapter:
    title: str
    key: str
    index: int
    html: str = ""
    url: str = ""


@dataclass
class ReaderBook:
    source_url: str
    title: str
    kind: str
    chapters: List[ReaderChapter] = field(default_factory=list)
    epub_path: str = ""


@dataclass
class ReaderOpenResult:
    book: Optional[ReaderBook] = None
    error: str = ""


def book_roots(output_dir: str = "") -> List[Path]:
    roots = [get_default_books_dir()]
    extra = downloads_folder(output_dir)
    if extra.resolve() != roots[0].resolve():
        roots.append(extra)
    return roots


def find_local_epub(
    *,
    output_path: str = "",
    epub_filename: str = "",
    output_dir: str = "",
    extra_path: str = "",
) -> Optional[Path]:
    """Return an existing .epub under the books folders, or None."""
    roots = book_roots(output_dir)
    name = safe_epub_basename(epub_filename or "")
    if not name:
        name = safe_epub_basename(output_path or extra_path or "")
    candidates: List[Path] = []
    for raw in (extra_path, output_path):
        text = (raw or "").strip()
        if text:
            candidates.append(Path(text))
    if name:
        for root in roots:
            candidates.append(root / name)
    seen = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_file() and is_allowed_epub_path(resolved, roots):
            return resolved
    return None


def sanitize_reader_html(html: str) -> str:
    """Drop executable markup and in-chapter links so the viewer cannot navigate."""
    raw = html or ""
    if not raw.strip():
        return ""
    try:
        root = lxml_html.fromstring(raw)
    except Exception:
        return raw
    for tag in _DROP_TAGS:
        for el in list(root.iter(tag)):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    for el in list(root.iter()):
        for attr in list(el.attrib):
            lowered = attr.lower()
            if lowered.startswith("on") or lowered in {"srcdoc", "formaction"}:
                del el.attrib[attr]
        if el.tag == "a":
            el.tag = "span"
            el.attrib.pop("href", None)
            el.attrib.pop("target", None)
        elif el.tag in {"img", "source", "video", "audio", "iframe"}:
            el.attrib.pop("src", None)
            el.attrib.pop("srcset", None)
    try:
        return lxml_html.tostring(root, encoding="unicode", method="html")
    except Exception:
        return raw


_CSS_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")
_CSS_FAMILY_RE = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")


def wrap_reader_html(body: str, *, font_pt: int = 18, color: str = "#e8e8e8",
                     background: str = "#2b2b2b", font_family: str = "",
                     line_height: float = 1.7, align: str = "justify") -> str:
    size = max(12, min(36, int(font_pt or 18)))
    inner = sanitize_reader_html(body)
    color = color if _CSS_COLOR_RE.match(color or "") else "#e8e8e8"
    background = background if _CSS_COLOR_RE.match(background or "") else "#2b2b2b"
    family = f"font-family:'{font_family}'; " if _CSS_FAMILY_RE.match(font_family or "") else ""
    try:
        lead = float(line_height)
    except (TypeError, ValueError):
        lead = 1.7
    lead = min(2.4, max(1.2, lead))
    text_align = "justify" if align == "justify" else "left"
    return (
        "<html><head><meta charset='utf-8'><style>"
        f"body {{ color:{color}; background:{background}; {family}font-size:{size}pt; "
        f"line-height:{lead}; padding:18px 28px; }}"
        "h1,h2,h3 { font-weight:600; line-height:1.3; }"
        f"p {{ margin: 0.75em 0; text-align:{text_align}; }}"
        "</style></head><body>"
        f"{inner}"
        "</body></html>"
    )


def _title_from_html(html: str) -> str:
    match = _HEADING_RE.search(html or "")
    if not match:
        return ""
    text = re.sub(r"<[^>]+>", "", match.group(1))
    return re.sub(r"\s+", " ", text).strip()


def load_epub_chapters(path: Path, *, bodies: bool = True) -> List[ReaderChapter]:
    from ebooklib import ITEM_DOCUMENT, epub

    book = epub.read_epub(str(path))
    by_id = {}
    by_name = {}
    for item in book.get_items():
        ident = getattr(item, "id", None) or getattr(item, "file_name", None)
        if ident:
            by_id[ident] = item
        name = getattr(item, "file_name", None)
        if name:
            by_name[name] = item
    source_urls = _epub_chapter_map(by_name.get(CHAPTER_MAP_NAME))

    ordered = []
    seen = set()
    for entry in book.spine or []:
        ref = entry[0] if isinstance(entry, tuple) else entry
        if ref in (None, "nav"):
            continue
        item = ref if hasattr(ref, "get_content") else (by_id.get(ref) or by_name.get(ref))
        if item is None:
            continue
        ident = getattr(item, "id", None) or getattr(item, "file_name", None)
        if ident in seen:
            continue
        seen.add(ident)
        ordered.append(item)

    if not ordered:
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            ident = getattr(item, "id", None) or getattr(item, "file_name", None)
            if ident in seen:
                continue
            seen.add(ident)
            ordered.append(item)

    chapters: List[ReaderChapter] = []
    for item in ordered:
        name = str(getattr(item, "file_name", "") or "")
        if _SKIP_EPUB_NAME.search(name.replace("\\", "/").split("/")[-1]):
            continue
        try:
            if item.get_type() != ITEM_DOCUMENT:
                continue
        except Exception:
            pass
        title = (getattr(item, "title", None) or "").strip()
        if not bodies:
            html = ""
            if not title:
                title = f"Chapter {len(chapters) + 1}"
        else:
            raw = item.get_content() if hasattr(item, "get_content") else b""
            if isinstance(raw, bytes):
                html = raw.decode("utf-8", errors="replace")
            else:
                html = str(raw or "")
            if not title:
                title = _title_from_html(html) or f"Chapter {len(chapters) + 1}"
        key = name or str(getattr(item, "id", "") or len(chapters))
        chapters.append(
            ReaderChapter(title=title, key=key, index=len(chapters), html=html,
                          url=source_urls.get(name, ""))
        )
    return chapters


def _epub_chapter_map(item) -> dict:
    """Chapter file name -> source URL, from an EPUB HuaEPUB wrote (empty for others)."""
    if item is None:
        return {}
    try:
        raw = json.loads(item.get_content().decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v}


def chapters_from_toc(
    toc: Sequence[dict],
    *,
    cache=None,
    extras: Optional[Iterable] = None,
) -> List[ReaderChapter]:
    rows: List[dict] = []
    seen = set()
    for item in list(toc or []):
        if not isinstance(item, dict):
            url = str(getattr(item, "url", "") or "").strip()
            title = str(getattr(item, "title", "") or "")
        else:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        rows.append({"url": url, "title": title})
    if extras:
        for item in extras:
            url = str(getattr(item, "url", "") or "").strip()
            title = str(getattr(item, "title", "") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            rows.append({"url": url, "title": title})
    chapters: List[ReaderChapter] = []
    for row in rows:
        url = row["url"]
        html = ""
        if cache is not None:
            try:
                html = cache.get_chapter(url) or ""
            except Exception:
                html = ""
        chapters.append(
            ReaderChapter(
                title=row["title"] or f"Chapter {len(chapters) + 1}",
                key=url,
                index=len(chapters),
                html=html,
                url=url,
            )
        )
    return chapters


def resolve_reader_book(
    *,
    source_url: str,
    title: str = "",
    output_path: str = "",
    epub_filename: str = "",
    output_dir: str = "",
    cache=None,
    extra_chapters: Optional[Sequence] = None,
    extra_epub_path: str = "",
) -> ReaderOpenResult:
    """
    Prefer a local EPUB under the books folder. Otherwise build a cache/TOC book.
    """
    url = (source_url or "").strip()
    display = (title or "").strip() or url or "Untitled"
    epub = find_local_epub(
        output_path=output_path,
        epub_filename=epub_filename,
        output_dir=output_dir,
        extra_path=extra_epub_path,
    )
    if epub is not None:
        try:
            chapters = load_epub_chapters(epub)
        except Exception as exc:
            return ReaderOpenResult(error=f"Could not open EPUB: {exc}")
        if not chapters:
            return ReaderOpenResult(error="That EPUB has no readable chapters.")
        return ReaderOpenResult(
            book=ReaderBook(
                source_url=url,
                title=display,
                kind=KIND_EPUB,
                chapters=chapters,
                epub_path=str(epub),
            )
        )

    toc = None
    if cache is not None and url:
        try:
            toc = cache.get_chapter_list(url)
        except Exception:
            toc = None
    chapters = chapters_from_toc(toc or [], cache=cache, extras=extra_chapters)
    _apply_stored_english_titles(chapters, cache, url)
    if not chapters:
        return ReaderOpenResult(
            error="Nothing to read yet. Download an EPUB, or fetch the chapter list first."
        )
    return ReaderOpenResult(
        book=ReaderBook(
            source_url=url,
            title=display,
            kind=KIND_CACHE,
            chapters=chapters,
        )
    )


def next_cache_prefetch_index(book: Optional[ReaderBook], current_index: int) -> Optional[int]:
    """N+1 chapter index for KIND_CACHE when that chapter has no HTML yet."""
    if book is None or book.kind != KIND_CACHE or not book.chapters:
        return None
    nxt = int(current_index) + 1
    if nxt < 0 or nxt >= len(book.chapters):
        return None
    ch = book.chapters[nxt]
    if (ch.html or "").strip() or not (ch.url or "").strip():
        return None
    return nxt


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except Exception:
        return ""


def reader_fetch_waits(job, downloading: bool, chapter_url: str) -> bool:
    """True when a running download is already scraping this chapter's site.

    A different site can be read at the same time. An unknown job waits, so two
    scrapes of one host do not run together.
    """
    if not downloading:
        return False
    host = _hostname(chapter_url)
    if not host or not isinstance(job, dict):
        return True
    hosts = {_hostname(str(job.get("source_url") or ""))}
    info = job.get("info") if isinstance(job.get("info"), dict) else {}
    hosts.add(_hostname(str(info.get("source_url") or "")))
    for key in ("novels", "entries"):
        for item in job.get(key) or []:
            if isinstance(item, dict):
                hosts.add(_hostname(str(item.get("source_url") or item.get("url") or "")))
    hosts.discard("")
    if not hosts:
        return True
    return host in hosts


def _apply_stored_english_titles(chapters, cache, source_url: str) -> None:
    getter = getattr(cache, "get_english_chapter_titles", None) if cache is not None else None
    if not callable(getter) or not source_url:
        return
    try:
        stored = getter(source_url) or []
    except Exception:
        return
    from core.cache import english_chapter_title

    by_url = {}
    for row in stored:
        if not isinstance(row, dict):
            continue
        chapter_url = (row.get("url") or "").strip()
        title = english_chapter_title(row.get("title") or "")
        if chapter_url and title:
            by_url[chapter_url] = title
    for chapter in chapters:
        if chapter.url in by_url:
            chapter.title = by_url[chapter.url]


def html_needs_live_translate(html: str) -> bool:
    """True when cache HTML still has enough Chinese to bother translating."""
    raw = html or ""
    if not raw.strip():
        return False
    from core.cleaner import count_chinese_chars, is_chinese

    return is_chinese(raw) and count_chinese_chars(raw) > 8


class UnsupportedSite(RuntimeError):
    """No parser handles the chapter (or book) URL."""


def fetch_reader_chapter(cache, book_url: str, url: str, title: str = "") -> str:
    """Fetch one missing chapter body from the site and keep it in the cache.

    Raises UnsupportedSite, RuntimeError("Chapter came back empty."), or the
    parser's own error. The caller waits out the site's ``request_delay``.
    """
    from core.parser import Chapter, get_parser_for_url

    parser = get_parser_for_url(url) or get_parser_for_url(book_url)
    if not parser:
        raise UnsupportedSite("Unsupported site")
    html = parser.get_chapter_content(Chapter(title=title or "", url=url))
    if not html or not str(html).strip():
        raise RuntimeError("Chapter came back empty.")
    try:
        cache.put_chapter(book_url, url, title or "", html)
    except Exception:
        pass
    return html


def live_translate_chapter(
    html: str,
    *,
    cache,
    options: dict,
    novel_title: str = "",
    detect_text: str = "",
    chapter_title: str = "",
    chapter_url: str = "",
    source_url: str = "",
) -> tuple:
    """Translate one cache chapter and, when it is still Chinese, its title."""
    from types import SimpleNamespace

    from core.cache import english_chapter_title, save_english_chapter_title
    from core.cleaner import ContentCleaner, is_chinese
    from core.download_runner import make_translator, translator_backend_kwargs

    kw = translator_backend_kwargs({}, options)
    translator = make_translator(cache=cache,
                                 max_workers=int(options.get("workers", 200) or 200), **kw)
    cfg = getattr(translator, "configure_glossary", None)
    if callable(cfg):
        cfg(SimpleNamespace(title=novel_title or "", description=""),
            mode=kw.get("glossary_mode", "auto"), detect_text=detect_text or novel_title or "")
    cleaner = ContentCleaner() if options.get("clean", True) else None
    out = translator.translate_and_apply_html(html or "", cleaner=cleaner) or html
    title = chapter_title or ""
    if title and not english_chapter_title(title) and is_chinese(title):
        try:
            got = translator.translate_texts([title]) or []
        except Exception:
            got = []
        shown = english_chapter_title(got[0] if got else "")
        if shown:
            title = shown
            save_english_chapter_title(cache, source_url, chapter_url, shown)
    return out, title


def resume_index(book: ReaderBook, position: Optional[dict]) -> int:
    if not book.chapters:
        return 0
    if not position:
        return 0
    url = str(position.get("chapter_url") or "")
    if url:
        for ch in book.chapters:
            if ch.url == url or ch.key == url:
                return ch.index
    try:
        idx = int(position.get("chapter_index") or 0)
    except (TypeError, ValueError):
        idx = 0
    return max(0, min(idx, len(book.chapters) - 1))
