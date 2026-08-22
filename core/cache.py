# Author: joelsnl and Anthropic Claude
"""
SQLite-backed persistent caches.

Local-only database (cache.db in ~/.huaepub/) — never synced to Drive:

- chapters: successfully downloaded chapter HTML, keyed by chapter URL.
- translations: translated / polished text segments, keyed by (backend, source).
- covers: cover image bytes, keyed by cover URL (or book source_url fallback).
- chapter_lists: TOC snapshots for faster library update checks.

Size cap: settings cache_max_mb (default 2048; 0 = unlimited). Nothing is
timer-cleared. When over the cap, oldest stored chapter HTML is deleted
first, then covers, then TOCs, then translations. Help → Cache… is the UI.

Translation writes may be batched (put_translation(..., commit=False) +
flush()) so a long Google pass is not one SQLite commit per segment.

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

    _COMMIT_BATCH = 200

    def __init__(self, db_path, max_bytes: Optional[int] = None):
        self._lock = threading.Lock()
        self._conn = None
        self._db_path = Path(db_path)
        self._max_bytes_override = max_bytes
        self._pending = 0
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
                self._pending = 0
            self.maybe_evict()
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

    def delete_cover(self, cover_url: str = "", source_url: str = ""):
        key = self.cover_key(cover_url, source_url)
        if not self._conn or not key:
            return
        try:
            with self._lock:
                self._conn.execute("DELETE FROM covers WHERE key = ?", (key,))
                self._conn.commit()
        except Exception:
            pass

    def delete_chapter_list(self, source_url: str):
        if not self._conn or not source_url:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM chapter_lists WHERE source_url = ?",
                    (source_url.strip(),),
                )
                self._conn.commit()
        except Exception:
            pass

    def purge_book(self, source_url: str, cover_url: str = ""):
        """Drop chapter HTML, TOC snapshot, and cover bytes for one novel."""
        url = (source_url or "").strip()
        if url:
            self.clear_book(url)
            self.delete_chapter_list(url)
            self.delete_cover(source_url=url)
        if cover_url:
            self.delete_cover(cover_url=cover_url)

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

    def put_translation(self, source: str, translated: str, backend: str, commit: bool = True):
        """Store a successful translation. Pass commit=False to batch, then flush()."""
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
                self._pending += 1
                if commit or self._pending >= self._COMMIT_BATCH:
                    self._conn.commit()
                    self._pending = 0
            if commit:
                self.maybe_evict()
        except Exception:
            pass

    def flush(self):
        """Commit batched translation writes."""
        if not self._conn:
            return
        try:
            with self._lock:
                if self._pending:
                    self._conn.commit()
                    self._pending = 0
            self.maybe_evict()
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
                self._pending = 0
            self.maybe_evict()
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
                self._pending = 0
            self.maybe_evict()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Size cap / LRU eviction
    # ------------------------------------------------------------------

    def file_size_bytes(self) -> int:
        """On-disk size of cache.db plus WAL/SHM sidecars."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self._db_path) + suffix) if suffix else self._db_path
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                pass
        return total

    def _cap_bytes(self) -> int:
        if self._max_bytes_override is not None:
            return max(0, int(self._max_bytes_override))
        try:
            from core.settings import get_setting
            mb = int(get_setting("cache_max_mb") or 0)
        except Exception:
            mb = 2048
        if mb <= 0:
            return 0
        return mb * 1024 * 1024

    def _payload_bytes_unlocked(self, table: str) -> str:
        if table == "chapters":
            return "COALESCE(LENGTH(content), 0)"
        if table == "covers":
            return "COALESCE(LENGTH(data), 0)"
        if table == "chapter_lists":
            return "COALESCE(LENGTH(payload), 0)"
        if table == "translations":
            return "COALESCE(LENGTH(source), 0) + COALESCE(LENGTH(translated), 0)"
        return "0"

    def _payload_total_unlocked(self) -> int:
        total = 0
        for table in ("chapters", "covers", "chapter_lists", "translations"):
            expr = self._payload_bytes_unlocked(table)
            row = self._conn.execute(
                f"SELECT COALESCE(SUM({expr}), 0) FROM {table}"
            ).fetchone()
            total += int(row[0] or 0)
        return total

    def maybe_evict(self) -> int:
        """
        If cache.db is over the size cap, delete oldest rows until it fits.

        Order: chapter HTML, then covers, then TOC snapshots, then translations
        as a last resort. Returns how many rows were removed.

        The delete loop uses payload sizes because WAL files do not shrink
        until VACUUM.
        """
        if not self._conn:
            return 0
        cap = self._cap_bytes()
        if cap <= 0 or self.file_size_bytes() <= cap:
            return 0
        target_payload = max(int(cap * 0.85), cap - 8 * 1024 * 1024)
        removed = 0
        stages = (
            ("chapters", "url", "fetched_at"),
            ("covers", "key", "fetched_at"),
            ("chapter_lists", "source_url", "fetched_at"),
            ("translations", "key", "created_at"),
        )
        try:
            with self._lock:
                if self._pending:
                    self._conn.commit()
                    self._pending = 0
                payload = self._payload_total_unlocked()
                for table, pk, col in stages:
                    size_sql = self._payload_bytes_unlocked(table)
                    while payload > target_payload:
                        row = self._conn.execute(
                            f"SELECT {pk}, {size_sql} FROM {table} "
                            f"ORDER BY COALESCE({col}, 0) ASC LIMIT 1"
                        ).fetchone()
                        if not row:
                            break
                        self._conn.execute(
                            f"DELETE FROM {table} WHERE {pk} = ?", (row[0],)
                        )
                        payload -= int(row[1] or 0)
                        removed += 1
                    if payload <= target_payload:
                        break
                if removed:
                    self._conn.commit()
                    self._conn.execute("VACUUM")
                    self._conn.commit()
            if removed:
                now_mb = self.file_size_bytes() / (1024 * 1024)
                cap_mb = cap / (1024 * 1024)
                print(
                    f"Cache was over {cap_mb:.0f} MB; removed {removed} oldest "
                    f"entries. Now {now_mb:.1f} MB."
                )
        except Exception as e:
            print(f"Warning: cache eviction failed: {e}")
        return removed

    def clear_chapter_data(self):
        """Drop chapter HTML, covers, and TOC snapshots. Keep translations."""
        if not self._conn:
            return
        try:
            with self._lock:
                self._conn.execute("DELETE FROM chapters")
                self._conn.execute("DELETE FROM covers")
                self._conn.execute("DELETE FROM chapter_lists")
                self._conn.commit()
                self._pending = 0
                self._conn.execute("VACUUM")
                self._conn.commit()
        except Exception:
            pass

    def clear_all(self):
        """Drop every cache table, including translations."""
        if not self._conn:
            return
        try:
            with self._lock:
                for table in ("chapters", "covers", "chapter_lists", "translations"):
                    self._conn.execute(f"DELETE FROM {table}")
                self._conn.commit()
                self._pending = 0
                self._conn.execute("VACUUM")
                self._conn.commit()
        except Exception:
            pass

    def close(self):
        if self._conn:
            try:
                with self._lock:
                    if self._pending:
                        self._conn.commit()
                        self._pending = 0
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
