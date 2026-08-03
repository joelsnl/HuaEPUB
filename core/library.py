# Author: joelsnl and Anthropic Claude
"""
Persistent download history and reading library.

- history: recent successful downloads (for quick re-fill of the URL box)
- library: novels you follow; stores last-downloaded chapter so "Update"
  can pull only new chapters and rebuild a complete EPUB (old chapters
  come from the chapter cache).

Stored as library.json in ~/.noveldownloader/. Never raises to callers.
Optional Google Drive sync merges remote copies via merge_library().
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

LIBRARY_FILE = "library.json"
MAX_HISTORY = 40


@dataclass
class HistoryEntry:
    source_url: str
    title: str = ""
    translated_title: str = ""
    author: str = ""
    chapter_count: int = 0
    output_path: str = ""
    downloaded_at: float = 0.0


@dataclass
class LibraryEntry:
    source_url: str
    title: str = ""
    translated_title: str = ""
    author: str = ""
    cover_url: str = ""
    chapter_count: int = 0
    last_chapter_url: str = ""
    last_chapter_title: str = ""
    last_downloaded_at: float = 0.0
    output_path: str = ""
    drive_file_id: str = ""
    epub_filename: str = ""


@dataclass
class LibraryData:
    history: List[HistoryEntry] = field(default_factory=list)
    library: List[LibraryEntry] = field(default_factory=list)


def _history_from_dict(e: dict) -> Optional[HistoryEntry]:
    url = (e.get('source_url') or '').strip()
    if not url:
        return None
    try:
        return HistoryEntry(
            source_url=url,
            title=e.get('title', '') or '',
            translated_title=e.get('translated_title', '') or '',
            author=e.get('author', '') or '',
            chapter_count=int(e.get('chapter_count') or 0),
            output_path=e.get('output_path', '') or '',
            downloaded_at=float(e.get('downloaded_at') or 0),
        )
    except (TypeError, ValueError):
        return None


def _library_from_dict(e: dict) -> Optional[LibraryEntry]:
    url = (e.get('source_url') or '').strip()
    if not url:
        return None
    try:
        return LibraryEntry(
            source_url=url,
            title=e.get('title', '') or '',
            translated_title=e.get('translated_title', '') or '',
            author=e.get('author', '') or '',
            cover_url=e.get('cover_url', '') or '',
            chapter_count=int(e.get('chapter_count') or 0),
            last_chapter_url=e.get('last_chapter_url', '') or '',
            last_chapter_title=e.get('last_chapter_title', '') or '',
            last_downloaded_at=float(e.get('last_downloaded_at') or 0),
            output_path=e.get('output_path', '') or '',
            drive_file_id=e.get('drive_file_id', '') or '',
            epub_filename=e.get('epub_filename', '') or '',
        )
    except (TypeError, ValueError):
        return None


def library_data_from_dict(raw: dict) -> LibraryData:
    """Parse a library.json-shaped dict into LibraryData."""
    data = LibraryData()
    if not isinstance(raw, dict):
        return data
    for e in raw.get('history', []):
        if isinstance(e, dict):
            entry = _history_from_dict(e)
            if entry:
                data.history.append(entry)
    for e in raw.get('library', []):
        if isinstance(e, dict):
            entry = _library_from_dict(e)
            if entry:
                data.library.append(entry)
    return data


def library_data_to_dict(data: LibraryData) -> dict:
    return {
        'history': [asdict(e) for e in data.history],
        'library': [asdict(e) for e in data.library],
    }


def library_payload_hash(payload: dict) -> str:
    """Stable hash of library JSON for skip-noop sync."""
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _prefer_library_entry(a: LibraryEntry, b: LibraryEntry) -> LibraryEntry:
    """Pick the better of two entries for the same source_url."""
    if a.last_downloaded_at != b.last_downloaded_at:
        winner = a if a.last_downloaded_at > b.last_downloaded_at else b
        loser = b if winner is a else a
    elif a.chapter_count != b.chapter_count:
        winner = a if a.chapter_count > b.chapter_count else b
        loser = b if winner is a else a
    else:
        winner, loser = a, b

    # Fill blanks from the other side (e.g. local path vs remote drive id)
    merged = LibraryEntry(**asdict(winner))
    if not merged.drive_file_id and loser.drive_file_id:
        merged.drive_file_id = loser.drive_file_id
    if not merged.epub_filename and loser.epub_filename:
        merged.epub_filename = loser.epub_filename
    if not merged.output_path and loser.output_path:
        merged.output_path = loser.output_path
    if not merged.cover_url and loser.cover_url:
        merged.cover_url = loser.cover_url
    if not merged.translated_title and loser.translated_title:
        merged.translated_title = loser.translated_title
    if not merged.author and loser.author:
        merged.author = loser.author
    return merged


def merge_library(local: LibraryData, remote: LibraryData) -> LibraryData:
    """
    Merge local and remote library data.

    - Union novels by source_url; newer last_downloaded_at wins
      (ties → higher chapter_count).
    - History by source_url; newer downloaded_at wins; capped at MAX_HISTORY.
    """
    lib_map = {}
    for entry in list(remote.library) + list(local.library):
        # Process remote first then local so equal timestamps keep local
        # unless remote is newer — iterate remote then local with prefer
        existing = lib_map.get(entry.source_url)
        if existing is None:
            lib_map[entry.source_url] = entry
        else:
            lib_map[entry.source_url] = _prefer_library_entry(existing, entry)

    # Sort by last_downloaded_at descending
    library = sorted(
        lib_map.values(),
        key=lambda e: e.last_downloaded_at,
        reverse=True,
    )

    hist_map = {}
    for entry in list(remote.history) + list(local.history):
        existing = hist_map.get(entry.source_url)
        if existing is None or entry.downloaded_at > existing.downloaded_at:
            hist_map[entry.source_url] = entry

    history = sorted(
        hist_map.values(),
        key=lambda e: e.downloaded_at,
        reverse=True,
    )[:MAX_HISTORY]

    return LibraryData(history=history, library=library)


class LibraryStore:
    """Thread-safe JSON store for history + library."""

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            from core.settings import get_data_dir
            path = get_data_dir() / LIBRARY_FILE
        self._path = path
        self._lock = threading.Lock()
        self._data = LibraryData()
        self._load()

    def _load(self):
        try:
            if not self._path.exists():
                return
            with open(self._path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            self._data = library_data_from_dict(raw if isinstance(raw, dict) else {})
        except Exception:
            self._data = LibraryData()

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = library_data_to_dict(self._data)
            with open(self._path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def get_data(self) -> LibraryData:
        with self._lock:
            return LibraryData(
                history=list(self._data.history),
                library=list(self._data.library),
            )

    def replace_data(self, data: LibraryData):
        """Replace entire store contents (used after Drive merge)."""
        with self._lock:
            self._data = LibraryData(
                history=list(data.history),
                library=list(data.library),
            )
            self._save()

    def to_payload(self) -> dict:
        with self._lock:
            return library_data_to_dict(self._data)

    def add_history(
        self,
        source_url: str,
        title: str = '',
        translated_title: str = '',
        author: str = '',
        chapter_count: int = 0,
        output_path: str = '',
    ):
        if not source_url:
            return
        entry = HistoryEntry(
            source_url=source_url,
            title=title or '',
            translated_title=translated_title or title or '',
            author=author or '',
            chapter_count=chapter_count,
            output_path=output_path or '',
            downloaded_at=time.time(),
        )
        with self._lock:
            self._data.history = [
                h for h in self._data.history if h.source_url != source_url
            ]
            self._data.history.insert(0, entry)
            self._data.history = self._data.history[:MAX_HISTORY]
            self._save()

    def get_history(self) -> List[HistoryEntry]:
        with self._lock:
            return list(self._data.history)

    def upsert_library(
        self,
        source_url: str,
        title: str = '',
        translated_title: str = '',
        author: str = '',
        cover_url: str = '',
        chapter_count: int = 0,
        last_chapter_url: str = '',
        last_chapter_title: str = '',
        output_path: str = '',
        drive_file_id: str = '',
        epub_filename: str = '',
    ):
        if not source_url:
            return
        with self._lock:
            prev = None
            for e in self._data.library:
                if e.source_url == source_url:
                    prev = e
                    break
            entry = LibraryEntry(
                source_url=source_url,
                title=title or (prev.title if prev else ''),
                translated_title=translated_title or (prev.translated_title if prev else '') or title or '',
                author=author or (prev.author if prev else ''),
                cover_url=cover_url or (prev.cover_url if prev else ''),
                chapter_count=chapter_count,
                last_chapter_url=last_chapter_url or '',
                last_chapter_title=last_chapter_title or '',
                last_downloaded_at=time.time(),
                output_path=output_path or (prev.output_path if prev else ''),
                drive_file_id=drive_file_id or (prev.drive_file_id if prev else ''),
                epub_filename=epub_filename or (prev.epub_filename if prev else ''),
            )
            self._data.library = [
                e for e in self._data.library if e.source_url != source_url
            ]
            self._data.library.insert(0, entry)
            self._save()

    def update_drive_file(
        self,
        source_url: str,
        drive_file_id: str = '',
        epub_filename: str = '',
        output_path: str = '',
    ):
        """Update Drive/EPUB fields without bumping last_downloaded_at."""
        if not source_url:
            return
        with self._lock:
            for e in self._data.library:
                if e.source_url == source_url:
                    if drive_file_id:
                        e.drive_file_id = drive_file_id
                    if epub_filename:
                        e.epub_filename = epub_filename
                    if output_path:
                        e.output_path = output_path
                    self._save()
                    return

    def get_library(self) -> List[LibraryEntry]:
        with self._lock:
            return list(self._data.library)

    def get_library_entry(self, source_url: str) -> Optional[LibraryEntry]:
        with self._lock:
            for e in self._data.library:
                if e.source_url == source_url:
                    return e
        return None

    def remove_library(self, source_url: str) -> bool:
        with self._lock:
            before = len(self._data.library)
            self._data.library = [
                e for e in self._data.library if e.source_url != source_url
            ]
            if len(self._data.library) != before:
                self._save()
                return True
            return False


def new_chapters_since(chapters, last_chapter_url: str, last_chapter_count: int = 0):
    """
    Return (new_chapters, start_index) for chapters after the last download.

    Prefers matching last_chapter_url; falls back to slicing at last_chapter_count
    when the URL is missing (site reshuffle / first run edge cases).
    """
    if not chapters:
        return [], 0

    if last_chapter_url:
        for i, ch in enumerate(chapters):
            if getattr(ch, 'url', None) == last_chapter_url:
                return chapters[i + 1:], i + 1

    if last_chapter_count > 0:
        start = min(last_chapter_count, len(chapters))
        return chapters[start:], start

    return chapters, 0
