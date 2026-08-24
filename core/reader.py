# Author: joelsnl and Anthropic Claude
"""
Resolve a novel for the in-app reader: local EPUB first, else cached TOC/HTML.

Never Drive-syncs. Callers pull a Drive EPUB into the books folder first if
needed. Chapter HTML is sanitized for QTextBrowser (no scripts, no navigation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from lxml import html as lxml_html

from core.download_runner import downloads_folder
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
    drive_file_id: str = ""


@dataclass
class ReaderOpenResult:
    book: Optional[ReaderBook] = None
    need_drive: bool = False
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


def wrap_reader_html(body: str, *, font_pt: int = 18) -> str:
    size = max(12, min(36, int(font_pt or 18)))
    inner = sanitize_reader_html(body)
    return (
        "<html><head><meta charset='utf-8'><style>"
        f"body {{ color:#e8e8e8; background:#2b2b2b; font-size:{size}pt; "
        "line-height:1.65; padding:8px 16px; }}"
        "h1,h2,h3 { font-weight:600; }"
        "p { margin: 0.7em 0; }"
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


def load_epub_chapters(path: Path) -> List[ReaderChapter]:
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
        raw = item.get_content() if hasattr(item, "get_content") else b""
        if isinstance(raw, bytes):
            html = raw.decode("utf-8", errors="replace")
        else:
            html = str(raw or "")
        title = (getattr(item, "title", None) or "").strip()
        if not title:
            title = _title_from_html(html) or f"Chapter {len(chapters) + 1}"
        key = name or str(getattr(item, "id", "") or len(chapters))
        chapters.append(
            ReaderChapter(title=title, key=key, index=len(chapters), html=html)
        )
    return chapters


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
    drive_file_id: str = "",
    output_dir: str = "",
    cache=None,
    extra_chapters: Optional[Sequence] = None,
    extra_epub_path: str = "",
) -> ReaderOpenResult:
    """
    Prefer a local EPUB under the books folder. If none, signal Drive when
    drive_file_id is set. Otherwise build a cache/TOC book.
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
                drive_file_id=drive_file_id or "",
            )
        )
    if (drive_file_id or "").strip():
        return ReaderOpenResult(need_drive=True)

    toc = None
    if cache is not None and url:
        try:
            toc = cache.get_chapter_list(url)
        except Exception:
            toc = None
    chapters = chapters_from_toc(toc or [], cache=cache, extras=extra_chapters)
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
            drive_file_id=drive_file_id or "",
        )
    )


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
