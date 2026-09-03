# Author: joelsnl and Anthropic Claude
"""
Persistent download history and reading library.

- history: recent successful downloads (for quick re-fill of the URL box)
- library: novels you follow; stores last-downloaded chapter so "Update"
  can pull only new chapters and rebuild a complete EPUB (old chapters
  come from the chapter cache).

Stored as library.json in ~/.huaepub/. Never raises to callers.
Optional Google Drive sync merges remote copies via merge_library()
(library.json + EPUBs only — never cache.db or resume files).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from core.security import (
    is_allowed_epub_path,
    safe_epub_basename,
)

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
    description: str = ""


@dataclass
class RemovedEntry:
    """Tombstone so Drive merge cannot resurrect a novel the user removed."""
    source_url: str
    removed_at: float = 0.0
    epub_filename: str = ""
    drive_file_id: str = ""


@dataclass
class LibraryData:
    history: List[HistoryEntry] = field(default_factory=list)
    library: List[LibraryEntry] = field(default_factory=list)
    removed: List[RemovedEntry] = field(default_factory=list)


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
            description=e.get('description', '') or '',
        )
    except (TypeError, ValueError):
        return None


def _removed_from_dict(e: dict) -> Optional[RemovedEntry]:
    url = (e.get('source_url') or '').strip()
    if not url:
        return None
    try:
        return RemovedEntry(
            source_url=url,
            removed_at=float(e.get('removed_at') or 0),
            epub_filename=e.get('epub_filename', '') or '',
            drive_file_id=e.get('drive_file_id', '') or '',
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
    for e in raw.get('removed', []):
        if isinstance(e, dict):
            entry = _removed_from_dict(e)
            if entry:
                data.removed.append(entry)
    return data


def library_data_to_dict(data: LibraryData) -> dict:
    return {
        'history': [asdict(e) for e in data.history],
        'library': [asdict(e) for e in data.library],
        'removed': [asdict(e) for e in data.removed],
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
    if not merged.description and loser.description:
        merged.description = loser.description
    return merged


def _prefer_removed_entry(a: RemovedEntry, b: RemovedEntry) -> RemovedEntry:
    winner = a if a.removed_at >= b.removed_at else b
    loser = b if winner is a else a
    merged = RemovedEntry(**asdict(winner))
    if not merged.epub_filename and loser.epub_filename:
        merged.epub_filename = loser.epub_filename
    if not merged.drive_file_id and loser.drive_file_id:
        merged.drive_file_id = loser.drive_file_id
    return merged


def _tombstone_map(entries: List[RemovedEntry]) -> dict:
    out = {}
    for entry in entries:
        url = (entry.source_url or "").strip()
        if not url:
            continue
        prev = out.get(url)
        out[url] = entry if prev is None else _prefer_removed_entry(prev, entry)
    return out


def merge_library(local: LibraryData, remote: LibraryData) -> LibraryData:
    """
    Merge local and remote library data.

    - Union novels by source_url; newer last_downloaded_at wins
      (ties → higher chapter_count).
    - History by source_url; newer downloaded_at wins; capped at MAX_HISTORY.
    - Tombstones (`removed`) win over older library/history rows so a local
      Remove is not undone by the next Drive sync.
    """
    removed_map = _tombstone_map(list(remote.removed) + list(local.removed))

    lib_map = {}
    for entry in list(remote.library) + list(local.library):
        existing = lib_map.get(entry.source_url)
        if existing is None:
            lib_map[entry.source_url] = entry
        else:
            lib_map[entry.source_url] = _prefer_library_entry(existing, entry)

    library = []
    for entry in lib_map.values():
        tomb = removed_map.get(entry.source_url)
        if tomb and float(entry.last_downloaded_at or 0) <= float(tomb.removed_at or 0):
            continue
        if tomb and float(entry.last_downloaded_at or 0) > float(tomb.removed_at or 0):
            removed_map.pop(entry.source_url, None)
        library.append(entry)

    library = sorted(
        library,
        key=lambda e: e.last_downloaded_at,
        reverse=True,
    )

    hist_map = {}
    for entry in list(remote.history) + list(local.history):
        existing = hist_map.get(entry.source_url)
        if existing is None or entry.downloaded_at > existing.downloaded_at:
            hist_map[entry.source_url] = entry

    history = []
    for entry in hist_map.values():
        tomb = removed_map.get(entry.source_url)
        if tomb and float(entry.downloaded_at or 0) <= float(tomb.removed_at or 0):
            continue
        history.append(entry)
    history = sorted(
        history,
        key=lambda e: e.downloaded_at,
        reverse=True,
    )[:MAX_HISTORY]

    removed = sorted(
        removed_map.values(),
        key=lambda e: e.removed_at,
        reverse=True,
    )
    return LibraryData(history=history, library=library, removed=removed)


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
            from core.atomic_io import atomic_write_json
            payload = library_data_to_dict(self._data)
            atomic_write_json(self._path, payload, fsync=False)
        except Exception as e:
            print(f"Failed to save library.json to {self._path}: {e}")

    def get_data(self) -> LibraryData:
        with self._lock:
            return LibraryData(
                history=list(self._data.history),
                library=list(self._data.library),
                removed=list(self._data.removed),
            )

    def replace_data(self, data: LibraryData) -> None:
        """Replace entire store contents (used after Drive merge)."""
        with self._lock:
            self._data = LibraryData(
                history=list(data.history),
                library=list(data.library),
                removed=list(getattr(data, "removed", None) or []),
            )
            self._save()
            print(
                f"Library store updated: {len(self._data.library)} novel(s) "
                f"→ {self._path}"
            )

    def reload(self) -> None:
        """Re-read library.json from disk into memory."""
        with self._lock:
            self._load()

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
    ) -> None:
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
        description: str = '',
    ) -> None:
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
                description=description or (prev.description if prev else ''),
            )
            self._data.library = [
                e for e in self._data.library if e.source_url != source_url
            ]
            self._data.library.insert(0, entry)
            self._data.removed = [
                r for r in self._data.removed if r.source_url != source_url
            ]
            self._save()

    def update_drive_file(
        self,
        source_url: str,
        drive_file_id: str = '',
        epub_filename: str = '',
        output_path: str = '',
    ) -> None:
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

    def update_metadata(
        self,
        source_url: str,
        *,
        title: str = '',
        translated_title: str = '',
        author: str = '',
        cover_url: str = '',
        description: str = '',
    ) -> None:
        """Update display metadata without bumping last_downloaded_at."""
        if not source_url:
            return
        with self._lock:
            for e in self._data.library:
                if e.source_url == source_url:
                    if title:
                        e.title = title
                    if translated_title:
                        e.translated_title = translated_title
                    if author:
                        e.author = author
                    if cover_url:
                        e.cover_url = cover_url
                    if description:
                        e.description = description
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

    def remove_library(self, source_url: str) -> Optional[LibraryEntry]:
        """
        Drop the novel from library + history and write a tombstone so Drive
        sync cannot resurrect it. Returns the removed entry (for file/cache
        cleanup), or None if it was not in the library.
        """
        url = (source_url or "").strip()
        if not url:
            return None
        with self._lock:
            found = None
            for e in self._data.library:
                if e.source_url == url:
                    found = e
                    break
            self._data.library = [
                e for e in self._data.library if e.source_url != url
            ]
            self._data.history = [
                h for h in self._data.history if h.source_url != url
            ]
            filename = ""
            drive_id = ""
            if found:
                filename = found.epub_filename or (
                    Path(found.output_path).name if found.output_path else ""
                )
                drive_id = found.drive_file_id or ""
            self._data.removed = [
                r for r in self._data.removed if r.source_url != url
            ]
            self._data.removed.insert(
                0,
                RemovedEntry(
                    source_url=url,
                    removed_at=time.time(),
                    epub_filename=filename,
                    drive_file_id=drive_id,
                ),
            )
            self._save()
            return found

    def get_removed(self) -> List[RemovedEntry]:
        with self._lock:
            return list(self._data.removed)

    def clear(
        self,
        *,
        clear_library: bool = True,
        clear_history: bool = False,
    ) -> None:
        """
        Wipe tracked library and/or recent history.
        Does not touch EPUB files or the chapter/translation cache.
        """
        with self._lock:
            if clear_library:
                now = time.time()
                tombs = {r.source_url: r for r in self._data.removed}
                for e in self._data.library:
                    filename = e.epub_filename or (
                        Path(e.output_path).name if e.output_path else ""
                    )
                    tombs[e.source_url] = RemovedEntry(
                        source_url=e.source_url,
                        removed_at=now,
                        epub_filename=filename,
                        drive_file_id=e.drive_file_id or "",
                    )
                self._data.removed = list(tombs.values())
                self._data.library = []
            if clear_history:
                self._data.history = []
            self._save()


def new_chapters_since(
    chapters, last_chapter_url: str, last_chapter_count: int = 0
) -> Tuple[list, int]:
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


def _unlink_epub(path: Path) -> bool:
    try:
        p = Path(path)
        if p.is_file() and p.suffix.lower() == ".epub":
            p.unlink()
            print(f"Deleted local EPUB: {p}")
            return True
    except Exception as e:
        print(f"Could not delete local EPUB {path}: {e}")
    return False


def purge_novel_artifacts(
    entry: LibraryEntry, cache=None, extra_dirs=None, *, data_dir=None
) -> None:
    """
    Delete this novel's local EPUB and per-book caches (chapters, TOC, cover).
    Does not wipe the shared translation cache (phrases are reused across books).
    Only unlinks .epub files under extra_dirs (books folder / output folder).
    Also drops the local reading position (never Drive-synced).
    """
    try:
        from core.reading import clear_position
        clear_position(entry.source_url, data_dir=data_dir)
    except Exception as e:
        print(f"Could not clear reading position for {entry.source_url}: {e}")
    if cache is not None:
        try:
            cache.purge_book(entry.source_url, cover_url=entry.cover_url or "")
        except Exception as e:
            print(f"Could not purge cache for {entry.source_url}: {e}")

    roots = []
    for folder in extra_dirs or []:
        try:
            roots.append(Path(folder))
        except Exception:
            continue

    seen = set()
    candidates = []
    if entry.output_path:
        candidates.append(Path(entry.output_path))
    name = safe_epub_basename(entry.epub_filename or "")
    if not name:
        name = safe_epub_basename(entry.output_path or "")
    for folder in extra_dirs or []:
        if name:
            candidates.append(Path(folder) / name)
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not roots or not is_allowed_epub_path(path, roots):
            continue
        _unlink_epub(path)
