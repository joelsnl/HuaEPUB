# Author: joelsnl and Anthropic Claude
"""
Library “are there new chapters?” checks.

Check fetches TOC only (never chapter bodies, covers, or translation).
A TOC snapshot younger than TOC_FRESH_SECONDS is reused so a second
Check seconds later does not hammer the site. Different hosts may run
in parallel; the same host reuses one HTTP session and honors
request_delay.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from core.library import LibraryEntry, new_chapters_since
from core.parser import Chapter, get_parser_for_url

# Skip a network TOC fetch when we just checked this novel (seconds, not hours).
TOC_FRESH_SECONDS = 90.0
# Cap parallel host groups so we do not open dozens of sessions at once.
MAX_HOST_WORKERS = 4


def host_key(url: str) -> str:
    """Registrable host for session reuse / delay (www. stripped)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        host = (urlparse(raw if "://" in raw else f"https://{raw}").hostname or "")
    except Exception:
        return ""
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def chapters_from_toc_rows(rows: Optional[Sequence[Any]]) -> List[Chapter]:
    """Turn cache TOC dicts or Chapter-like objects into Chapter list."""
    out: List[Chapter] = []
    for i, item in enumerate(rows or []):
        if isinstance(item, dict):
            url = (item.get("url") or "").strip()
            title = item.get("title") or ""
        else:
            url = (getattr(item, "url", None) or "").strip()
            title = getattr(item, "title", "") or ""
        if not url:
            continue
        out.append(Chapter(title=title, url=url, index=i))
    return out


def status_for_chapters(entry: LibraryEntry, chapters: Sequence[Any]) -> dict:
    """Badge dict for one library row (no network)."""
    new_only, _ = new_chapters_since(
        chapters, entry.last_chapter_url, entry.chapter_count
    )
    if new_only:
        return {
            "state": "update",
            "new_count": len(new_only),
            "total": len(chapters),
            "error": "",
            "cover_refreshed": False,
            "from_cache": False,
        }
    return {
        "state": "current",
        "new_count": 0,
        "total": len(chapters),
        "error": "",
        "cover_refreshed": False,
        "from_cache": False,
    }


def error_status(message: str) -> dict:
    return {
        "state": "error",
        "new_count": 0,
        "total": 0,
        "error": message or "Failed",
        "cover_refreshed": False,
        "from_cache": False,
    }


def cached_toc_if_fresh(
    cache,
    source_url: str,
    *,
    now: Optional[float] = None,
    max_age: float = TOC_FRESH_SECONDS,
) -> Optional[List[Chapter]]:
    """Return Chapter list from a still-fresh TOC snapshot, or None."""
    if cache is None or not source_url:
        return None
    getter = getattr(cache, "get_chapter_list_meta", None)
    if not callable(getter):
        rows = cache.get_chapter_list(source_url)
        if not rows:
            return None
        return chapters_from_toc_rows(rows)
    meta = getter(source_url)
    if not meta:
        return None
    rows, fetched_at = meta
    try:
        age = (time.time() if now is None else float(now)) - float(fetched_at or 0)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > float(max_age):
        return None
    chapters = chapters_from_toc_rows(rows)
    return chapters or None


def fetch_toc_chapters(parser, url: str) -> List[Chapter]:
    """TOC only — never get_novel_info / fetch_info_and_chapters / bodies."""
    chapters = parser.get_chapter_list(url)
    if not chapters:
        raise ValueError("No chapters found")
    return list(chapters)


class HostSessionPool:
    """One parser (HTTP session) per host; serialize + delay on that host."""

    def __init__(
        self,
        get_parser: Callable[[str], Any] = get_parser_for_url,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._get_parser = get_parser
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._parsers: Dict[str, Any] = {}
        self._host_locks: Dict[str, threading.Lock] = {}
        self._next_ok: Dict[str, float] = {}

    def host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    def parser_for(self, url: str):
        host = host_key(url)
        with self._lock:
            parser = self._parsers.get(host)
            if parser is None:
                parser = self._get_parser(url)
                self._parsers[host] = parser
            return parser

    def wait_turn(self, url: str, parser) -> None:
        """Sleep only the remaining per-host delay (not a fixed extra pause)."""
        host = host_key(url)
        delay = float(getattr(parser, "request_delay", 0) or 0)
        if delay <= 0:
            return
        with self._lock:
            ready_at = self._next_ok.get(host, 0.0)
        wait = ready_at - self._clock()
        if wait > 0:
            self._sleep(wait)

    def mark_request(self, url: str, parser) -> None:
        host = host_key(url)
        delay = float(getattr(parser, "request_delay", 0) or 0)
        with self._lock:
            self._next_ok[host] = self._clock() + max(0.0, delay)


def check_library_entry(
    entry: LibraryEntry,
    cache,
    pool: HostSessionPool,
    *,
    force: bool = False,
    now: Optional[float] = None,
    max_age: float = TOC_FRESH_SECONDS,
) -> dict:
    """Check one novel: fresh TOC cache or a TOC-only network fetch."""
    url = (entry.source_url or "").strip()
    if not url:
        return error_status("Missing URL")
    if not force:
        cached = cached_toc_if_fresh(cache, url, now=now, max_age=max_age)
        if cached is not None:
            st = status_for_chapters(entry, cached)
            st["from_cache"] = True
            return st
    host = host_key(url)
    with pool.host_lock(host):
        parser = pool.parser_for(url)
        if parser is None:
            return error_status("Unsupported site")
        pool.wait_turn(url, parser)
        try:
            chapters = fetch_toc_chapters(parser, url)
        except Exception as e:
            pool.mark_request(url, parser)
            return error_status(str(e))
        pool.mark_request(url, parser)
    if cache is not None:
        try:
            cache.put_chapter_list(url, chapters)
        except Exception:
            pass
    return status_for_chapters(entry, chapters)


def run_library_check(
    entries: Sequence[LibraryEntry],
    cache,
    *,
    force: bool = False,
    now: Optional[float] = None,
    max_age: float = TOC_FRESH_SECONDS,
    get_parser: Callable[[str], Any] = get_parser_for_url,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    on_entry: Optional[Callable[[str, dict], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_workers: int = MAX_HOST_WORKERS,
) -> Tuple[int, int]:
    """
    Check every library entry. Returns (novels_with_updates, total).

    Novels on different hosts run concurrently (capped). Same-host TOCs
    share one session and wait ``request_delay`` between network fetches.
    """
    items = list(entries or [])
    total = len(items)
    if total == 0:
        return 0, 0

    pool = HostSessionPool(get_parser, sleep=sleep, clock=clock)
    progress_lock = threading.Lock()
    started = 0
    with_updates = 0

    def emit_progress(entry: LibraryEntry) -> None:
        nonlocal started
        title = entry.translated_title or entry.title or entry.source_url or ""
        with progress_lock:
            started += 1
            n = started
        if on_progress:
            on_progress(n, total, title)

    def check_one(entry: LibraryEntry) -> Tuple[str, dict]:
        emit_progress(entry)
        try:
            st = check_library_entry(
                entry, cache, pool, force=force, now=now, max_age=max_age
            )
        except Exception as e:
            st = error_status(str(e))
        if on_entry:
            on_entry(entry.source_url, st)
        return entry.source_url, st

    by_host: Dict[str, List[LibraryEntry]] = defaultdict(list)
    for entry in items:
        by_host[host_key(entry.source_url) or "_"].append(entry)

    groups = list(by_host.values())
    workers = max(1, min(int(max_workers or 1), len(groups)))

    def run_group(group: List[LibraryEntry]) -> List[dict]:
        return [check_one(entry)[1] for entry in group]

    if workers == 1:
        results = []
        for group in groups:
            results.extend(run_group(group))
    else:
        results = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(run_group, group) for group in groups]
            for fut in as_completed(futs):
                results.extend(fut.result())

    with_updates = sum(1 for st in results if st.get("state") == "update")
    return with_updates, total
