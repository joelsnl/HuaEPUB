# Author: joelsnl and Anthropic Claude
"""
SQLite-backed persistent caches.

Two caches in one database file (cache.db in ~/.noveldownloader/):

- chapters: successfully downloaded chapter HTML, keyed by chapter URL.
  Lets a cancelled/failed run resume without re-downloading, and makes
  re-downloading a novel (e.g. to pick up new chapters) nearly instant.
- translations: translated text segments, keyed by (backend, source text).
  Re-translating a novel or sharing recurring phrases between novels
  costs zero API requests.

All methods are thread-safe (single connection guarded by a lock) and
never raise - a broken cache degrades to "no cache", not a crash.
"""

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


class NovelCache:
    """Persistent cache for chapters and translations."""

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

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
