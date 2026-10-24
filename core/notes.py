# Author: joelsnl and Anthropic Claude
"""
Reader notes: passages a reader saves from a book (a cultivation realm list, a character, a
place) so they can be found again without a screenshot.

Stored as notes.json in ~/.huaepub/, keyed by the book's source URL. Local only, like
reading.json. Notes stay when a book leaves the Library, so adding it again brings them back.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.settings import get_data_dir

NOTES_FILE = "notes.json"
MAX_TEXT = 2000
MAX_LABEL = 80
MAX_NOTES_PER_BOOK = 500

_lock = threading.Lock()


def _path(data_dir: Optional[Path] = None) -> Path:
    return Path(data_dir or get_data_dir()) / NOTES_FILE


def _load_unlocked(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _row(item: Any) -> Optional[dict]:
    if not isinstance(item, dict) or not item.get("id"):
        return None
    try:
        index = max(0, int(item.get("chapter_index") or 0))
    except (TypeError, ValueError):
        index = 0
    try:
        scroll = min(1.0, max(0.0, float(item.get("scroll") or 0.0)))
    except (TypeError, ValueError):
        scroll = 0.0
    return {
        "id": str(item["id"]),
        "text": _clean(item.get("text"), MAX_TEXT),
        "label": _clean(item.get("label"), MAX_LABEL),
        "chapter_index": index,
        "chapter_url": str(item.get("chapter_url") or ""),
        "scroll": scroll,
        "created_at": float(item.get("created_at") or 0),
    }


def _book_rows(data: Dict[str, Any], url: str) -> List[dict]:
    rows = [_row(i) for i in (data.get(url) or [])]
    return [r for r in rows if r]


def _save_unlocked(path: Path, data: Dict[str, Any], url: str, rows: List[dict]) -> None:
    from core.atomic_io import atomic_write_json

    if rows:
        data[url] = rows
    else:
        data.pop(url, None)
    atomic_write_json(path, data, fsync=True, ensure_ascii=False)


def list_notes(source_url: str, *, data_dir: Optional[Path] = None) -> List[dict]:
    """The book's notes, newest first."""
    url = (source_url or "").strip()
    if not url:
        return []
    with _lock:
        rows = _book_rows(_load_unlocked(_path(data_dir)), url)
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)


def add_note(
    source_url: str,
    text: str,
    *,
    label: str = "",
    chapter_index: int = 0,
    chapter_url: str = "",
    scroll: float = 0.0,
    data_dir: Optional[Path] = None,
) -> Optional[dict]:
    """Save a note. None when the text is empty or the book already has the most notes allowed."""
    url = (source_url or "").strip()
    body = _clean(text, MAX_TEXT)
    if not url or not body:
        return None
    new = _row({
        "id": secrets.token_urlsafe(6), "text": body, "label": label,
        "chapter_index": chapter_index, "chapter_url": chapter_url, "scroll": scroll,
        "created_at": time.time(),
    })
    with _lock:
        path = _path(data_dir)
        data = _load_unlocked(path)
        rows = _book_rows(data, url)
        if len(rows) >= MAX_NOTES_PER_BOOK:
            return None
        rows.append(new)
        _save_unlocked(path, data, url, rows)
    return new


def set_label(source_url: str, note_id: str, label: str, *, data_dir: Optional[Path] = None) -> bool:
    url = (source_url or "").strip()
    with _lock:
        path = _path(data_dir)
        data = _load_unlocked(path)
        rows = _book_rows(data, url)
        for row in rows:
            if row["id"] == note_id:
                row["label"] = _clean(label, MAX_LABEL)
                _save_unlocked(path, data, url, rows)
                return True
    return False


def count_all_notes(*, data_dir: Optional[Path] = None) -> int:
    """Total notes across every book, from one read of notes.json (for the stats page)."""
    with _lock:
        data = _load_unlocked(_path(data_dir))
    return sum(len(_book_rows(data, url)) for url in data.keys())


def delete_note(source_url: str, note_id: str, *, data_dir: Optional[Path] = None) -> bool:
    url = (source_url or "").strip()
    with _lock:
        path = _path(data_dir)
        data = _load_unlocked(path)
        rows = _book_rows(data, url)
        kept = [r for r in rows if r["id"] != note_id]
        if len(kept) == len(rows):
            return False
        _save_unlocked(path, data, url, kept)
    return True
