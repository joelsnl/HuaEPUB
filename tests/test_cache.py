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


class TestBrokenCache:
    def test_unwritable_path_degrades_gracefully(self, tmp_path):
        # A directory as the db path makes sqlite fail to open
        bad = NovelCache(tmp_path)
        assert bad.get_chapter('https://x') is None
        bad.put_chapter('b', 'https://x', 't', 'c')  # must not raise
        assert bad.get_translation('你好', 'google') is None
        bad.close()
