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


class TestBrokenCache:
    def test_unwritable_path_degrades_gracefully(self, tmp_path):
        # A directory as the db path makes sqlite fail to open
        bad = NovelCache(tmp_path)
        assert bad.get_chapter('https://x') is None
        bad.put_chapter('b', 'https://x', 't', 'c')  # must not raise
        assert bad.get_translation('你好', 'google') is None
        assert bad.get_cover(cover_url='https://x/c') is None
        bad.put_cover(b'x', cover_url='https://x/c')
        bad.close()
