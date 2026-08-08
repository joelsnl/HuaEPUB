# Author: joelsnl and Anthropic Claude
"""
SQLite-backed persistent caches.

Local-only database (cache.db in ~/.huaepub/) — never synced to Drive:

- chapters: successfully downloaded chapter HTML, keyed by chapter URL.
- translations: translated text segments, keyed by (backend, source text).
- covers: cover image bytes, keyed by cover URL (or book source_url fallback).
- chapter_lists: TOC snapshots for faster library update checks.

All methods are thread-safe (single connection guarded by a lock) and
never raise - a broken cache degrades to "no cache", not a crash.
"""

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class NovelCache:
    """Persistent local cache for chapters, translations, covers, and TOCs."""

    def __init__(self, db_path):
        self._lock = threading.Lock()
        self._conn = None
        try:
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS chapters (
                    url        TEXT PRIMARY KEY,
                    book_key   TEXT,
                    title      TEXT,
                    content    TEXT,
                    fetched_at REAL
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS translations (
                    key        TEXT PRIMARY KEY,
                    backend    TEXT,
                    source     TEXT,
                    translated TEXT,
                    created_at REAL
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS covers (
                    key          TEXT PRIMARY KEY,
                    url          TEXT,
                    data         BLOB,
                    content_type TEXT,
                    fetched_at   REAL
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS chapter_lists (
                    source_url TEXT PRIMARY KEY,
                    payload    TEXT,
                    fetched_at REAL
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_key)"
            )
            self._conn.commit()
        except Exception as e:
            print(f"Warning: chapter/translation cache unavailable: {e}")
            self._conn = None

    # ------------------------------------------------------------------
    # Chapters
    # ------------------------------------------------------------------

    def get_chapter(self, url: str) -> Optional[str]:
        """Return cached chapter HTML for a URL, or None."""
        if not self._conn or not url:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT content FROM chapters WHERE url = ?", (url,)
                ).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def put_chapter(self, book_key: str, url: str, title: str, content: str):
        """Store successfully downloaded chapter HTML."""
        if not self._conn or not url or not content:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO chapters (url, book_key, title, content, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (url, book_key or '', title or '', content, time.time())
                )
                self._conn.commit()
        except Exception:
            pass

    def count_chapters(self, book_key: str) -> int:
        """Number of cached chapters for a book."""
        if not self._conn:
            return 0
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM chapters WHERE book_key = ?", (book_key,)
                ).fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def count_cached_urls(self, urls: List[str]) -> int:
        """How many of the given chapter URLs are already in the cache."""
        if not self._conn or not urls:
            return 0
        try:
            total = 0
            # SQLite default variable limit is 999 — batch conservatively
            with self._lock:
                for i in range(0, len(urls), 400):
                    batch = [u for u in urls[i:i + 400] if u]
                    if not batch:
                        continue
                    placeholders = ",".join("?" * len(batch))
                    row = self._conn.execute(
                        f"SELECT COUNT(*) FROM chapters WHERE url IN ({placeholders})",
                        batch,
                    ).fetchone()
                    total += row[0] if row else 0
            return total
        except Exception:
            return 0

    def clear_book(self, book_key: str):
        """Drop all cached chapters for a book (force fresh download)."""
        if not self._conn:
            return
        try:
            with self._lock:
                self._conn.execute("DELETE FROM chapters WHERE book_key = ?", (book_key,))
                self._conn.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Translations
    # ------------------------------------------------------------------

    @staticmethod
    def _translation_key(source: str, backend: str) -> str:
        payload = f"{backend}\x00{source.strip()}".encode('utf-8')
        return hashlib.sha256(payload).hexdigest()

    def get_translation(self, source: str, backend: str) -> Optional[str]:
        """Return cached translation for a source text, or None."""
        if not self._conn or not source:
            return None
        try:
            key = self._translation_key(source, backend)
            with self._lock:
                row = self._conn.execute(
                    "SELECT translated FROM translations WHERE key = ?", (key,)
                ).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def put_translation(self, source: str, translated: str, backend: str):
        """Store a successful translation."""
        if not self._conn or not source or not translated:
            return
        try:
            key = self._translation_key(source, backend)
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO translations (key, backend, source, translated, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (key, backend, source, translated, time.time())
                )
                self._conn.commit()
        except Exception:
            pass

    def delete_translation(self, source: str, backend: str):
        """Drop a cached translation (used before retrying failed segments)."""
        if not self._conn or not source:
            return
        try:
            key = self._translation_key(source, backend)
            with self._lock:
                self._conn.execute("DELETE FROM translations WHERE key = ?", (key,))
                self._conn.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Covers (local only — never Drive-synced)
    # ------------------------------------------------------------------

    @staticmethod
    def cover_key(cover_url: str = "", source_url: str = "") -> str:
        """Stable key: prefer cover URL, else book source URL."""
        raw = (cover_url or source_url or "").strip()
        if not raw:
            return ""
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_cover(self, cover_url: str = "", source_url: str = "") -> Optional[bytes]:
        """Return cached cover image bytes, or None."""
        key = self.cover_key(cover_url, source_url)
        if not self._conn or not key:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT data FROM covers WHERE key = ?", (key,)
                ).fetchone()
            data = row[0] if row else None
            return bytes(data) if data else None
        except Exception:
            return None

    def put_cover(
        self,
        data: bytes,
        cover_url: str = "",
        source_url: str = "",
        content_type: str = "",
    ):
        """Store cover image bytes locally."""
        key = self.cover_key(cover_url, source_url)
        if not self._conn or not key or not data:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO covers (key, url, data, content_type, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        key,
                        (cover_url or source_url or "").strip(),
                        bytes(data),
                        content_type or "",
                        time.time(),
                    ),
                )
                self._conn.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Chapter lists / TOC snapshots (local only)
    # ------------------------------------------------------------------

    def get_chapter_list(self, source_url: str) -> Optional[List[Dict[str, str]]]:
        """Return cached TOC as list of {url, title}, or None."""
        if not self._conn or not source_url:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT payload FROM chapter_lists WHERE source_url = ?",
                    (source_url.strip(),),
                ).fetchone()
            if not row or not row[0]:
                return None
            data = json.loads(row[0])
            if not isinstance(data, list):
                return None
            out: List[Dict[str, str]] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                url = (item.get("url") or "").strip()
                if not url:
                    continue
                out.append({"url": url, "title": (item.get("title") or "")})
            return out or None
        except Exception:
            return None

    def put_chapter_list(self, source_url: str, chapters: List[Any]):
        """
        Store a TOC snapshot. Accepts Chapter-like objects or {url, title} dicts.
        """
        if not self._conn or not source_url or not chapters:
            return
        try:
            payload = []
            for ch in chapters:
                if isinstance(ch, dict):
                    url = (ch.get("url") or "").strip()
                    title = ch.get("title") or ""
                else:
                    url = (getattr(ch, "url", None) or "").strip()
                    title = getattr(ch, "title", "") or ""
                if url:
                    payload.append({"url": url, "title": title})
            if not payload:
                return
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO chapter_lists (source_url, payload, fetched_at) "
                    "VALUES (?, ?, ?)",
                    (source_url.strip(), json.dumps(payload, ensure_ascii=False), time.time()),
                )
                self._conn.commit()
        except Exception:
            pass

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
