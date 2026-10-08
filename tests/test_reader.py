"""Offline tests for the in-app reader (EPUB vs cache, reading.json)."""

from pathlib import Path

from core.cache import NovelCache
from core.epub_builder import EPUBBuilder
from core.library import LibraryEntry, purge_novel_artifacts
from core.parser import Chapter, NovelInfo
from core.reader import (
    KIND_CACHE,
    KIND_EPUB,
    ReaderBook,
    ReaderChapter,
    find_local_epub,
    html_needs_live_translate,
    next_cache_prefetch_index,
    reader_fetch_waits,
    resolve_reader_book,
    resume_index,
    sanitize_reader_html,
)
from core.reading import clear_position, get_position, set_position


def _book(tmp_path: Path) -> Path:
    dest = tmp_path / "books"
    dest.mkdir(parents=True, exist_ok=True)
    epub = dest / "Test Book.epub"
    info = NovelInfo(title="Test Book", author="Author", source_url="https://example.com/book")
    chapters = [
        Chapter(title="Chapter 1", url="https://example.com/1", content="<p>Hello there.</p>"),
        Chapter(title="Chapter 2", url="https://example.com/2", content="<p>More words.</p>"),
    ]
    EPUBBuilder().build(info, chapters, str(epub))
    return epub


class TestSanitize:
    def test_strips_script_and_links(self):
        html = (
            '<p>Hi</p><script>alert(1)</script>'
            '<iframe src="https://evil.example/x"></iframe>'
            '<a href="https://evil.example/x">click</a>'
            '<img src="https://evil.example/x.png">'
        )
        out = sanitize_reader_html(html)
        assert "script" not in out.lower()
        assert "iframe" not in out.lower()
        assert "href" not in out.lower()
        assert "evil.example" not in out
        assert "Hi" in out
        assert "click" in out


class TestResolveEpub:
    def test_loads_epub_under_books(self, tmp_path):
        epub = _book(tmp_path)
        result = resolve_reader_book(
            source_url="https://example.com/book",
            title="Test Book",
            output_path=str(epub),
            output_dir=str(tmp_path / "books"),
        )
        assert result.error == ""
        assert result.book is not None
        assert result.book.kind == KIND_EPUB
        titles = [c.title for c in result.book.chapters]
        assert "Chapter 1" in titles
        assert "Chapter 2" in titles
        body = " ".join(c.html for c in result.book.chapters)
        assert "Hello there" in body

    def test_rejects_epub_outside_books(self, tmp_path):
        epub = tmp_path / "outside.epub"
        info = NovelInfo(title="Nope", author="A", source_url="https://example.com/x")
        chapters = [
            Chapter(title="Ch", url="https://example.com/x/1", content="<p>secret</p>"),
        ]
        EPUBBuilder().build(info, chapters, str(epub))
        books = tmp_path / "books"
        books.mkdir()
        result = resolve_reader_book(
            source_url="https://example.com/x",
            title="Nope",
            output_path=str(epub),
            output_dir=str(books),
        )
        assert result.book is None or result.book.kind != KIND_EPUB
        assert find_local_epub(
            output_path=str(epub), output_dir=str(books)
        ) is None

    def test_nothing_to_read_without_epub_or_toc(self, tmp_path):
        books = tmp_path / "books"
        books.mkdir()
        result = resolve_reader_book(
            source_url="https://example.com/book",
            title="Book",
            output_dir=str(books),
        )
        assert result.book is None
        assert result.error


class TestResolveCache:
    def test_uses_toc_and_cached_html(self, tmp_path):
        cache = NovelCache(tmp_path / "cache.db", max_bytes=0)
        url = "https://example.com/book"
        cache.put_chapter_list(
            url,
            [
                {"url": "https://example.com/1", "title": "Ch 1"},
                {"url": "https://example.com/2", "title": "Ch 2"},
            ],
        )
        cache.put_chapter(url, "https://example.com/1", "Ch 1", "<p>cached one</p>")
        try:
            result = resolve_reader_book(
                source_url=url,
                title="Book",
                output_dir=str(tmp_path / "books"),
                cache=cache,
            )
            assert result.book is not None
            assert result.book.kind == KIND_CACHE
            assert "cached one" in result.book.chapters[0].html
            assert result.book.chapters[1].html == ""
            assert result.book.chapters[1].url == "https://example.com/2"
        finally:
            cache.close()

    def test_cache_open_uses_stored_english_titles(self, tmp_path):
        cache = NovelCache(tmp_path / "cache.db", max_bytes=0)
        url = "https://example.com/book"
        cache.put_chapter_list(
            url,
            [
                {"url": "https://example.com/1", "title": "第一章"},
                {"url": "https://example.com/2", "title": "第二章"},
            ],
        )
        cache.put_english_chapter_titles(url, [{"url": "https://example.com/1", "title": "The Gate"}])
        try:
            result = resolve_reader_book(
                source_url=url, title="Book", output_dir=str(tmp_path / "books"), cache=cache,
            )
            assert result.book.chapters[0].title == "The Gate"
            assert result.book.chapters[1].title == "第二章"
        finally:
            cache.close()

    def test_missing_everything_errors(self, tmp_path):
        result = resolve_reader_book(
            source_url="https://example.com/none",
            title="None",
            output_dir=str(tmp_path / "books"),
        )
        assert result.book is None
        assert "Nothing to read" in result.error

    def test_resume_index_by_url(self, tmp_path):
        cache = NovelCache(tmp_path / "cache.db", max_bytes=0)
        url = "https://example.com/book"
        cache.put_chapter_list(
            url,
            [
                {"url": "https://example.com/1", "title": "A"},
                {"url": "https://example.com/2", "title": "B"},
            ],
        )
        try:
            book = resolve_reader_book(
                source_url=url, title="Book", cache=cache, output_dir=str(tmp_path)
            ).book
            idx = resume_index(book, {"chapter_url": "https://example.com/2", "chapter_index": 0})
            assert idx == 1
            assert resume_index(book, {"chapter_index": 99}) == 1
            assert resume_index(book, None) == 0
        finally:
            cache.close()


def test_bookmark_survives_saving_the_place(tmp_path):
    from core.reading import get_bookmarks, set_position, toggle_bookmark

    url = "https://example.com/book"
    assert toggle_bookmark(url, chapter_url="https://example.com/2", chapter_index=1, scroll=0.4,
                           data_dir=tmp_path) is True
    assert toggle_bookmark(url, chapter_url="https://example.com/2", chapter_index=1, scroll=0.4,
                           data_dir=tmp_path) is False
    assert toggle_bookmark(url, chapter_url="https://example.com/2", chapter_index=1, scroll=0.4,
                           data_dir=tmp_path) is True
    set_position(url, chapter_url="https://example.com/3", chapter_index=2, scroll=0.1, data_dir=tmp_path)
    marks = get_bookmarks(url, data_dir=tmp_path)
    assert len(marks) == 1 and marks[0]["chapter_index"] == 1


def test_reader_fetch_waits_for_the_same_site():
    assert reader_fetch_waits(None, False, "https://a.example/1") is False
    job = {"source_url": "https://a.example/book"}
    assert reader_fetch_waits(job, True, "https://a.example/1") is True
    assert reader_fetch_waits(job, True, "https://b.example/1") is False
    assert reader_fetch_waits({}, True, "https://a.example/1") is True


class TestReadingJson:
    def test_roundtrip_and_clear(self, tmp_path):
        url = "https://example.com/book"
        set_position(
            url,
            chapter_url="https://example.com/3",
            chapter_index=2,
            scroll=0.4,
            data_dir=tmp_path,
        )
        pos = get_position(url, data_dir=tmp_path)
        assert pos is not None
        assert pos["chapter_url"] == "https://example.com/3"
        assert pos["chapter_index"] == 2
        assert abs(pos["scroll"] - 0.4) < 1e-6
        tmp = tmp_path / "reading.json.tmp"
        assert not tmp.exists()
        clear_position(url, data_dir=tmp_path)
        assert get_position(url, data_dir=tmp_path) is None

    def test_purge_clears_position(self, tmp_path):
        url = "https://example.com/book"
        set_position(url, chapter_index=4, data_dir=tmp_path)
        books = tmp_path / "books"
        books.mkdir()
        entry = LibraryEntry(source_url=url, title="Book")
        purge_novel_artifacts(entry, extra_dirs=[books], data_dir=tmp_path)
        assert get_position(url, data_dir=tmp_path) is None


def test_next_cache_prefetch_index():
    book = ReaderBook(
        source_url="https://example.com/book",
        title="T",
        kind=KIND_CACHE,
        chapters=[
            ReaderChapter(title="1", key="a", index=0, html="<p>hi</p>", url="https://x/1"),
            ReaderChapter(title="2", key="b", index=1, html="", url="https://x/2"),
        ],
    )
    assert next_cache_prefetch_index(book, 0) == 1
    assert next_cache_prefetch_index(book, 1) is None
    book.chapters[1].html = "<p>next</p>"
    assert next_cache_prefetch_index(book, 0) is None


def test_html_needs_live_translate():
    assert html_needs_live_translate("") is False
    assert html_needs_live_translate("<p>Hello there.</p>") is False
    assert html_needs_live_translate("<p>这是一段用于测试的中文正文内容</p>") is True
