"""Tests for core.cache.NovelCache (uses a temp database)."""

import pytest

from core.cache import NovelCache


@pytest.fixture
def cache(tmp_path):
    c = NovelCache(tmp_path / 'cache.db')
    yield c
    c.close()


class TestChapterCache:
    def test_roundtrip(self, cache):
        cache.put_chapter('book1', 'https://x/1', '第1章', '<p>content</p>')
        assert cache.get_chapter('https://x/1') == '<p>content</p>'

    def test_miss_returns_none(self, cache):
        assert cache.get_chapter('https://x/nope') is None

    def test_empty_content_not_stored(self, cache):
        cache.put_chapter('book1', 'https://x/1', 't', '')
        assert cache.get_chapter('https://x/1') is None

    def test_count_and_clear_book(self, cache):
        cache.put_chapter('book1', 'https://x/1', 't1', 'c1')
        cache.put_chapter('book1', 'https://x/2', 't2', 'c2')
        cache.put_chapter('book2', 'https://y/1', 't', 'c')
        assert cache.count_chapters('book1') == 2
        cache.clear_book('book1')
        assert cache.count_chapters('book1') == 0
        assert cache.get_chapter('https://y/1') == 'c'

    def test_count_cached_urls(self, cache):
        cache.put_chapter('book1', 'https://x/1', 't1', 'c1')
        cache.put_chapter('book1', 'https://x/2', 't2', 'c2')
        assert cache.count_cached_urls(['https://x/1', 'https://x/2', 'https://x/missing']) == 2
        assert cache.count_cached_urls([]) == 0

    def test_overwrite(self, cache):
        cache.put_chapter('b', 'https://x/1', 't', 'old')
        cache.put_chapter('b', 'https://x/1', 't', 'new')
        assert cache.get_chapter('https://x/1') == 'new'

    def test_sample_chapter_contents_first_and_last(self, cache):
        for i in range(6):
            cache.put_chapter("book1", f"https://x/{i}", f"t{i}", f"chapter-{i}-html")
        sample = cache.sample_chapter_contents("book1", limit=4)
        assert "chapter-0-html" in sample
        assert "chapter-5-html" in sample
        assert len(sample) <= 4


class TestTranslationCache:
    def test_roundtrip(self, cache):
        cache.put_translation('你好', 'Hello', 'google')
        assert cache.get_translation('你好', 'google') == 'Hello'

    def test_backend_isolation(self, cache):
        cache.put_translation('你好', 'Hello (google)', 'google')
        assert cache.get_translation('你好', 'libretranslate') is None

    def test_delete(self, cache):
        cache.put_translation('你好', 'Hello', 'google')
        cache.delete_translation('你好', 'google')
        assert cache.get_translation('你好', 'google') is None

    def test_whitespace_normalized_key(self, cache):
        cache.put_translation('  你好  ', 'Hello', 'google')
        assert cache.get_translation('你好', 'google') == 'Hello'

    def test_internal_whitespace_normalized(self, cache):
        cache.put_translation('你好\n世界', 'Hello world', 'google')
        assert cache.get_translation('你好 世界', 'google') == 'Hello world'

    def test_bulk_get_and_delete(self, cache):
        cache.put_translation('甲', 'A', 'google')
        cache.put_translation('乙', 'B', 'google')
        cache.put_translation('丙', 'C', 'libretranslate')
        hits = cache.get_translations_bulk(['甲', '乙', '丁'], ['google'])
        assert hits['甲'] == 'A'
        assert hits['乙'] == 'B'
        assert '丁' not in hits
        cache.delete_translations([('甲', 'google'), ('乙', 'google')])
        assert cache.get_translation('甲', 'google') is None
        assert cache.get_translation('丙', 'libretranslate') == 'C'

    def test_chapter_fingerprint_roundtrip(self, cache):
        from core.cache import chapter_fingerprint

        html = '<p>这是一段中文</p>'
        fp = chapter_fingerprint(html)
        cache.put_chapter_translation(fp, 'google', ['Hello'], commit=True)
        assert cache.get_chapter_translation(fp, 'google') == ['Hello']
        assert cache.get_chapter_translation('nope', 'google') is None
        cache.delete_chapter_translation(fp, 'google')
        assert cache.get_chapter_translation(fp, 'google') is None


class TestCoverCache:
    def test_roundtrip(self, cache):
        cache.put_cover(b"\xff\xd8fake", cover_url="https://x/cover.jpg", content_type="image/jpeg")
        assert cache.get_cover(cover_url="https://x/cover.jpg") == b"\xff\xd8fake"

    def test_source_url_fallback_key(self, cache):
        cache.put_cover(b"img", source_url="https://book/1")
        assert cache.get_cover(source_url="https://book/1") == b"img"
        assert cache.get_cover(cover_url="https://other") is None

    def test_empty_not_stored(self, cache):
        cache.put_cover(b"", cover_url="https://x/c.jpg")
        assert cache.get_cover(cover_url="https://x/c.jpg") is None


class TestChapterListCache:
    def test_roundtrip_dicts(self, cache):
        cache.put_chapter_list("https://book/1", [
            {"url": "https://book/1/c1", "title": "Ch1"},
            {"url": "https://book/1/c2", "title": "Ch2"},
        ])
        got = cache.get_chapter_list("https://book/1")
        assert got == [
            {"url": "https://book/1/c1", "title": "Ch1"},
            {"url": "https://book/1/c2", "title": "Ch2"},
        ]

    def test_purge_book_drops_chapters_cover_and_toc(self, cache):
        cache.put_chapter("https://book/1", "https://book/1/c1", "t", "c")
        cache.put_cover(b"img", source_url="https://book/1")
        cache.put_cover(b"jpg", cover_url="https://book/1/cover.jpg")
        cache.put_chapter_list("https://book/1", [{"url": "https://book/1/c1", "title": "t"}])
        cache.put_chapter("https://other", "https://other/c1", "t", "keep")
        cache.purge_book("https://book/1", cover_url="https://book/1/cover.jpg")
        assert cache.count_chapters("https://book/1") == 0
        assert cache.get_chapter("https://other/c1") == "keep"
        assert cache.get_cover(source_url="https://book/1") is None
        assert cache.get_cover(cover_url="https://book/1/cover.jpg") is None
        assert cache.get_chapter_list("https://book/1") is None

    def test_meta_includes_fetched_at(self, cache):
        cache.put_chapter_list(
            "https://book/1",
            [{"url": "https://book/1/c1", "title": "Ch1"}],
            fetched_at=1_700_000_000,
        )
        meta = cache.get_chapter_list_meta("https://book/1")
        assert meta is not None
        rows, fetched_at = meta
        assert rows[0]["url"] == "https://book/1/c1"
        assert fetched_at == 1_700_000_000

    def test_max_age_skips_stale_toc(self, cache):
        cache.put_chapter_list(
            "https://book/1",
            [{"url": "https://book/1/c1", "title": "Ch1"}],
            fetched_at=1_000.0,
        )
        assert cache.get_chapter_list("https://book/1") is not None
        assert cache.get_chapter_list("https://book/1", max_age=10) is None

    def test_english_titles_survive_toc_refresh_and_purge(self, cache):
        from core.cache import english_chapter_title, remember_english_chapter_titles
        from core.parser import Chapter

        assert english_chapter_title("Chapter 1 Talent Stripped") == "Chapter 1 Talent Stripped"
        assert english_chapter_title("第1章 天赋") == ""
        assert english_chapter_title("Chapter 12") == ""
        url = "https://book/1"
        remember_english_chapter_titles(cache, url, [
            Chapter(title="Chapter 1 Talent Stripped", url=f"{url}/1"),
            Chapter(title="第2章", url=f"{url}/2"),
        ])
        got = cache.get_english_chapter_titles(url)
        assert got[0] == {"url": f"{url}/1", "title": "Chapter 1 Talent Stripped"}
        assert got[1]["title"] == ""
        cache.put_chapter_list(url, [{"url": f"{url}/1", "title": "第1章"}])
        assert cache.get_chapter_list(url)[0]["title"] == "第1章"
        assert cache.get_english_chapter_titles(url)[0]["title"] == "Chapter 1 Talent Stripped"
        cache.purge_book(url)
        assert cache.get_english_chapter_titles(url) is None


class TestCacheEviction:
    def test_evicts_oldest_chapters_when_over_cap(self, tmp_path):
        path = tmp_path / "cache.db"
        cache = NovelCache(path, max_bytes=0)
        blob = "x" * 50_000
        for i in range(8):
            cache.put_chapter("b", f"https://x/{i}", f"t{i}", blob)
        assert cache.get_chapter("https://x/0") == blob
        # Keep about three 50 KB chapters plus SQLite overhead.
        cache._max_bytes_override = 250_000
        removed = cache.maybe_evict()
        assert removed >= 1
        assert cache.get_chapter("https://x/0") is None
        assert cache.get_chapter("https://x/7") == blob
        cache.close()

    def test_batch_put_translation_visible_after_flush(self, cache):
        cache.put_translation("你好", "Hello", "google", commit=False)
        assert cache.get_translation("你好", "google") == "Hello"
        cache.flush()
        cache2 = NovelCache(cache._db_path)
        try:
            assert cache2.get_translation("你好", "google") == "Hello"
        finally:
            cache2.close()

    def test_clear_chapter_data_keeps_translations(self, cache):
        cache.put_chapter("b", "https://x/1", "t", "c")
        cache.put_translation("你好", "Hello", "google")
        cache.clear_chapter_data()
        assert cache.get_chapter("https://x/1") is None
        assert cache.get_translation("你好", "google") == "Hello"


class TestBrokenCache:
    """A failing cache must never stop a download, but it should not fail silently either."""

    @staticmethod
    def _failing(cache, monkeypatch):
        import sqlite3

        class FullDisk:
            def __getattr__(self, name):
                def fail(*_a, **_k):
                    raise sqlite3.OperationalError('database or disk is full')
                return fail

        monkeypatch.setattr(cache, '_conn', FullDisk())

    def test_a_failing_write_never_raises_and_is_reported_once(self, cache, capsys, monkeypatch):
        from core import utils

        monkeypatch.setattr(utils, '_reported', set())
        self._failing(cache, monkeypatch)
        cache.put_chapter('book', 'https://x/1', 'title', '<p>text</p>')
        cache.put_chapter('book', 'https://x/2', 'title', '<p>text</p>')
        out = capsys.readouterr().out
        assert out.count('Cache: put_chapter failed') == 1
        assert 'database or disk is full' in out and 'continuing without it' in out

    def test_a_failing_read_is_just_a_miss(self, cache, monkeypatch):
        self._failing(cache, monkeypatch)
        assert cache.get_chapter('https://x/1') is None

    def test_each_kind_of_failure_is_reported_separately(self, cache, capsys, monkeypatch):
        from core import utils

        monkeypatch.setattr(utils, '_reported', set())
        self._failing(cache, monkeypatch)
        cache.put_chapter('book', 'https://x/1', 'title', '<p>text</p>')
        cache.put_cover(b'img', cover_url='https://x/c.jpg', source_url='https://x/')
        out = capsys.readouterr().out
        assert 'put_chapter failed' in out and 'put_cover failed' in out
