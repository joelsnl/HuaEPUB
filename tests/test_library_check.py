"""Library Check: TOC-only, cached snapshot vs network, per-host delay."""

from __future__ import annotations

from dataclasses import dataclass

from core.cache import NovelCache
from core.library import LibraryEntry
from core.library_check import (
    TOC_FRESH_SECONDS,
    HostSessionPool,
    cached_toc_if_fresh,
    chapters_from_toc_rows,
    check_library_entry,
    fetch_toc_chapters,
    host_key,
    run_library_check,
    status_for_chapters,
)
from core.parser import Chapter


@dataclass
class _Rec:
    calls: list
    request_delay: float = 0.0

    def get_chapter_list(self, url):
        self.calls.append(("toc", url))
        n = 3 if url.endswith("a") else 2
        return [Chapter(title=f"C{i}", url=f"{url}/c{i}") for i in range(1, n + 1)]

    def get_novel_info(self, url):
        self.calls.append(("info", url))
        raise AssertionError("Check must not fetch novel info")

    def fetch_all_parallel(self, url):
        self.calls.append(("parallel", url))
        raise AssertionError("Check must not use fetch_all_parallel")


def _entry(url, *, count=2, last=""):
    return LibraryEntry(
        source_url=url,
        title=url.rsplit("/", 1)[-1],
        chapter_count=count,
        last_chapter_url=last or f"{url}/c{count}",
    )


def test_host_key_strips_www():
    assert host_key("https://www.Example.com/book/1") == "example.com"
    assert host_key("https://example.com/book/2") == "example.com"


def test_status_detects_new_chapters():
    entry = _entry("https://a.test/book", count=2, last="https://a.test/book/c2")
    chapters = [
        Chapter("1", "https://a.test/book/c1"),
        Chapter("2", "https://a.test/book/c2"),
        Chapter("3", "https://a.test/book/c3"),
    ]
    st = status_for_chapters(entry, chapters)
    assert st["state"] == "update"
    assert st["new_count"] == 1
    assert st["total"] == 3


def test_cached_toc_if_fresh(tmp_path):
    cache = NovelCache(tmp_path / "cache.db")
    try:
        cache.put_chapter_list(
            "https://book/1",
            [{"url": "https://book/1/c1", "title": "1"}],
            fetched_at=1_000.0,
        )
        assert cached_toc_if_fresh(
            cache, "https://book/1", now=1_000.0 + 10, max_age=90
        )
        assert cached_toc_if_fresh(
            cache, "https://book/1", now=1_000.0 + 200, max_age=90
        ) is None
    finally:
        cache.close()


def test_check_uses_fresh_toc_without_network(tmp_path):
    cache = NovelCache(tmp_path / "cache.db")
    rec = _Rec([])
    pool = HostSessionPool(lambda _u: rec)
    url = "https://a.test/a"
    cache.put_chapter_list(
        url,
        [
            {"url": f"{url}/c1", "title": "1"},
            {"url": f"{url}/c2", "title": "2"},
        ],
        fetched_at=5_000.0,
    )
    entry = _entry(url, count=2, last=f"{url}/c2")
    try:
        st = check_library_entry(
            entry, cache, pool, force=False, now=5_010.0, max_age=90
        )
        assert st["state"] == "current"
        assert st["from_cache"] is True
        assert rec.calls == []
    finally:
        cache.close()


def test_check_refetches_stale_toc_only(tmp_path):
    cache = NovelCache(tmp_path / "cache.db")
    rec = _Rec([])
    pool = HostSessionPool(lambda _u: rec)
    url = "https://a.test/a"
    cache.put_chapter_list(
        url,
        [{"url": f"{url}/c1", "title": "1"}],
        fetched_at=1.0,
    )
    entry = _entry(url, count=2, last=f"{url}/c2")
    try:
        st = check_library_entry(
            entry, cache, pool, force=False, now=10_000.0, max_age=90
        )
        assert rec.calls == [("toc", url)]
        assert not any(kind == "info" for kind, _ in rec.calls)
        assert st["state"] == "update"
        assert st["new_count"] == 1
        assert st["from_cache"] is False
        # Fresh snapshot stored for the next Check.
        assert cache.get_chapter_list(url, max_age=TOC_FRESH_SECONDS)
    finally:
        cache.close()


def test_force_ignores_fresh_cache(tmp_path):
    cache = NovelCache(tmp_path / "cache.db")
    rec = _Rec([])
    pool = HostSessionPool(lambda _u: rec)
    url = "https://a.test/a"
    cache.put_chapter_list(
        url,
        [{"url": f"{url}/c1", "title": "1"}, {"url": f"{url}/c2", "title": "2"}],
        fetched_at=9_000.0,
    )
    entry = _entry(url, count=2, last=f"{url}/c2")
    try:
        check_library_entry(entry, cache, pool, force=True, now=9_010.0, max_age=90)
        assert rec.calls == [("toc", url)]
    finally:
        cache.close()


def test_fetch_toc_does_not_call_info():
    rec = _Rec([])
    chapters = fetch_toc_chapters(rec, "https://a.test/a")
    assert [c.url for c in chapters] == [
        "https://a.test/a/c1",
        "https://a.test/a/c2",
        "https://a.test/a/c3",
    ]
    assert rec.calls == [("toc", "https://a.test/a")]


def test_same_host_reuses_parser_and_honors_delay(tmp_path):
    cache = NovelCache(tmp_path / "cache.db")
    sleeps = []
    rec = _Rec([], request_delay=2.0)
    created = []

    def get_parser(url):
        created.append(url)
        return rec

    entries = [
        _entry("https://a.test/a", count=2, last="https://a.test/a/c2"),
        _entry("https://a.test/other", count=2, last="https://a.test/other/c2"),
    ]
    try:
        run_library_check(
            entries,
            cache,
            force=True,
            get_parser=get_parser,
            sleep=sleeps.append,
            clock=lambda: 0.0,
            max_workers=1,
        )
        assert len(created) == 1
        assert rec.calls == [("toc", "https://a.test/a"), ("toc", "https://a.test/other")]
        assert sleeps == [2.0]
    finally:
        cache.close()


def test_run_library_check_reports_updates(tmp_path):
    cache = NovelCache(tmp_path / "cache.db")
    rec = _Rec([])
    progress = []
    done = []
    entries = [
        _entry("https://a.test/a", count=2, last="https://a.test/a/c2"),
        _entry("https://b.test/b", count=2, last="https://b.test/b/c2"),
    ]
    try:
        with_updates, total = run_library_check(
            entries,
            cache,
            force=True,
            get_parser=lambda _u: rec,
            on_progress=lambda i, t, n: progress.append((i, t, n)),
            on_entry=lambda u, st: done.append((u, st["state"])),
            max_workers=2,
        )
        assert total == 2
        assert with_updates == 1
        assert len(progress) == 2
        assert progress[0][0] == 1
        states = {u: s for u, s in done}
        assert states["https://a.test/a"] == "update"
        assert states["https://b.test/b"] == "current"
    finally:
        cache.close()


def test_chapters_from_toc_rows():
    ch = chapters_from_toc_rows(
        [{"url": "https://x/1", "title": "One"}, {"url": "", "title": "skip"}]
    )
    assert len(ch) == 1
    assert ch[0].url == "https://x/1"
    assert ch[0].title == "One"
