"""Tests for core.reading_stats."""

import pytest

from core.cache import NovelCache
from core.library import LibraryStore
from core.notes import add_note
from core.reading import set_position, toggle_bookmark
from core.reading_stats import compute_reading_stats
from core.translation.glossary import Glossary, Term, save_glossary_file


class FakeSession:
    def __init__(self, tmp_path):
        self.data_dir = tmp_path
        self.cache = NovelCache(tmp_path / "cache.db")
        self.library_store = LibraryStore(tmp_path / "library.json")


@pytest.fixture
def session(tmp_path):
    s = FakeSession(tmp_path)
    yield s
    s.cache.close()


def _add_book(session, url, title, author, chapters, host_path="book"):
    session.library_store.upsert_library(
        source_url=url, title=title, translated_title=title, author=author,
        chapter_count=chapters,
    )


class TestComputeReadingStats:
    def test_empty_library(self, session):
        stats = compute_reading_stats(session)
        assert stats["books"]["tracked"] == 0
        assert stats["chapters"]["in_library"] == 0
        assert stats["reading_streak_days"] == 0
        assert stats["longest_book"] is None
        assert stats["most_recent_book"] is None

    def test_counts_books_and_chapters(self, session):
        _add_book(session, "https://a.example/book1", "Book One", "Author A", 10)
        _add_book(session, "https://b.example/book2", "Book Two", "Author B", 30)

        stats = compute_reading_stats(session)
        assert stats["books"]["tracked"] == 2
        assert stats["books"]["authors"] == 2
        assert stats["chapters"]["in_library"] == 40
        assert stats["longest_book"] == {"title": "Book Two", "chapters": 30}
        assert stats["sites"]["count"] == 2

    def test_reading_progress_splits_reading_finished_untouched(self, session):
        _add_book(session, "https://a.example/book1", "Reading", "A", 10)
        _add_book(session, "https://a.example/book2", "Finished", "A", 5)
        _add_book(session, "https://a.example/book3", "Untouched", "A", 7)

        set_position("https://a.example/book1", chapter_index=3, data_dir=session.data_dir)
        set_position("https://a.example/book2", chapter_index=4, data_dir=session.data_dir)

        stats = compute_reading_stats(session)
        assert stats["books"]["reading"] == 1
        assert stats["books"]["finished"] == 1
        assert stats["books"]["untouched"] == 1
        # book1: chapters 1-4 read; book2: all 5 read.
        assert stats["chapters"]["read"] == 9

    def test_shelved_books_excluded_from_reading_progress(self, session):
        _add_book(session, "https://a.example/book1", "Shelved", "A", 10)
        session.library_store.set_shelved(["https://a.example/book1"], True)

        stats = compute_reading_stats(session)
        assert stats["books"]["tracked"] == 0
        assert stats["books"]["shelved"] == 1
        assert stats["books"]["untouched"] == 0
        # Still counted toward lifetime totals.
        assert stats["chapters"]["in_library"] == 10

    def test_top_site_by_chapters(self, session):
        _add_book(session, "https://big.example/book1", "Big", "A", 100)
        _add_book(session, "https://small.example/book2", "Small", "A", 5)

        stats = compute_reading_stats(session)
        assert stats["sites"]["top"] == "big.example"
        assert stats["sites"]["top_chapters"] == 100

    def test_notes_counted_across_books(self, session):
        _add_book(session, "https://a.example/book1", "Book", "A", 10)
        add_note("https://a.example/book1", "a cultivation realm", data_dir=session.data_dir)
        add_note("https://a.example/book1", "a character name", data_dir=session.data_dir)

        stats = compute_reading_stats(session)
        assert stats["notes"] == 2

    def test_bookmarks_counted_across_books(self, session):
        _add_book(session, "https://a.example/book1", "Book", "A", 10)
        toggle_bookmark("https://a.example/book1", chapter_index=1, scroll=0.1,
                         data_dir=session.data_dir)
        toggle_bookmark("https://a.example/book1", chapter_index=2, scroll=0.2,
                         data_dir=session.data_dir)

        stats = compute_reading_stats(session)
        assert stats["bookmarks"] == 2

    def test_glossary_terms_deduplicated_across_files(self, session):
        glossary_path = session.data_dir / "glossary.json"
        glossary_path.parent.mkdir(parents=True, exist_ok=True)
        save_glossary_file(
            glossary_path, Glossary(terms=[Term(source="张三", target="Zhang San")]),
        )
        novel_path = session.data_dir / "glossaries" / "book.json"
        novel_path.parent.mkdir(parents=True, exist_ok=True)
        save_glossary_file(
            novel_path,
            Glossary(terms=[
                Term(source="张三", target="Zhang San"),
                Term(source="金丹", target="Golden Core"),
            ]),
        )

        stats = compute_reading_stats(session)
        assert stats["glossary_terms"] == 2

    def test_cached_chapter_and_translation_totals(self, session):
        session.cache.put_chapter("https://a.example/book1", "https://a.example/1", "t",
                                   "<p>" + ("x" * 100) + "</p>")
        session.cache.put_translation("src text", "translated text here", "backend")

        stats = compute_reading_stats(session)
        assert stats["chapters"]["cached"] == 1
        assert stats["text"]["characters_cached"] == 100
        assert stats["text"]["translated_segments"] == 1
        assert stats["text"]["translated_characters"] == len("translated text here")


class TestCacheTextTotals:
    def test_chapter_text_totals_strips_tags(self, tmp_path):
        cache = NovelCache(tmp_path / "cache.db")
        try:
            cache.put_chapter("book1", "https://x/1", "t", "<p>hello</p><p>world</p>")
            totals = cache.chapter_text_totals()
            assert totals["chapters"] == 1
            assert totals["characters"] == len("helloworld")
        finally:
            cache.close()

    def test_translation_text_totals_estimates_words(self, tmp_path):
        cache = NovelCache(tmp_path / "cache.db")
        try:
            cache.put_translation("src", "four little words here", "backend")
            totals = cache.translation_text_totals()
            assert totals["segments"] == 1
            assert totals["characters"] == len("four little words here")
            assert totals["words"] == 4
        finally:
            cache.close()

    def test_empty_cache_returns_zeros(self, tmp_path):
        cache = NovelCache(tmp_path / "cache.db")
        try:
            assert cache.chapter_text_totals() == {"chapters": 0, "characters": 0}
            assert cache.translation_text_totals() == {"segments": 0, "words": 0, "characters": 0}
        finally:
            cache.close()
