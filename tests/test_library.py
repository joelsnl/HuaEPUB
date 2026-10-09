"""Tests for core.library."""

from dataclasses import dataclass

from core.library import (
    HistoryEntry,
    LibraryData,
    LibraryEntry,
    LibraryStore,
    RemovedEntry,
    merge_library,
    new_chapters_since,
    library_payload_hash,
    library_data_to_dict,
    purge_novel_artifacts,
)


@dataclass
class FakeChapter:
    title: str
    url: str


class TestNewChaptersSince:
    def test_by_url(self):
        chapters = [
            FakeChapter("1", "http://x/1"),
            FakeChapter("2", "http://x/2"),
            FakeChapter("3", "http://x/3"),
        ]
        new, start = new_chapters_since(chapters, "http://x/2", 2)
        assert [c.url for c in new] == ["http://x/3"]
        assert start == 2

    def test_up_to_date(self):
        chapters = [FakeChapter("1", "http://x/1"), FakeChapter("2", "http://x/2")]
        new, start = new_chapters_since(chapters, "http://x/2", 2)
        assert new == []
        assert start == 2

    def test_fallback_to_count(self):
        chapters = [
            FakeChapter("1", "http://x/1"),
            FakeChapter("2", "http://x/2"),
            FakeChapter("3", "http://x/3"),
        ]
        new, start = new_chapters_since(chapters, "http://missing", 2)
        assert [c.url for c in new] == ["http://x/3"]
        assert start == 2

    def test_no_prior(self):
        chapters = [FakeChapter("1", "http://x/1")]
        new, start = new_chapters_since(chapters, "", 0)
        assert new == chapters
        assert start == 0


class TestLibraryStore:
    def test_history_and_library_roundtrip(self, tmp_path):
        store = LibraryStore(tmp_path / "library.json")
        store.add_history(
            source_url="http://x/book",
            title="书名",
            translated_title="Book",
            author="Author",
            chapter_count=10,
            output_path="/tmp/Book.epub",
        )
        store.upsert_library(
            source_url="http://x/book",
            title="书名",
            translated_title="Book",
            author="Author",
            chapter_count=10,
            last_chapter_url="http://x/10",
            last_chapter_title="Ch 10",
            output_path="/tmp/Book.epub",
        )

        store2 = LibraryStore(tmp_path / "library.json")
        hist = store2.get_history()
        assert len(hist) == 1
        assert hist[0].translated_title == "Book"

        lib = store2.get_library()
        assert len(lib) == 1
        assert lib[0].last_chapter_url == "http://x/10"

        assert store2.remove_library("http://x/book")
        assert store2.get_library() == []
        tombs = store2.get_removed()
        assert len(tombs) == 1
        assert tombs[0].source_url == "http://x/book"
        assert tombs[0].epub_filename == "Book.epub"

    def test_preserves_epub_filename_on_upsert(self, tmp_path):
        store = LibraryStore(tmp_path / "library.json")
        store.upsert_library(
            source_url="http://x/book",
            title="Book",
            chapter_count=5,
            last_chapter_url="http://x/5",
            epub_filename="Book.epub",
        )
        store.upsert_library(
            source_url="http://x/book",
            title="Book",
            chapter_count=6,
            last_chapter_url="http://x/6",
        )
        entry = store.get_library_entry("http://x/book")
        assert entry.epub_filename == "Book.epub"
        assert entry.chapter_count == 6

    def test_clear_library_keeps_history_by_default(self, tmp_path):
        path = tmp_path / "library.json"
        store = LibraryStore(path)
        store.add_history(source_url="http://x/a", title="A")
        store.upsert_library(source_url="http://x/a", title="A", chapter_count=1)
        store.clear(clear_library=True, clear_history=False)
        assert store.get_library() == []
        assert len(store.get_history()) == 1
        assert any(r.source_url == "http://x/a" for r in store.get_removed())

    def test_clear_both(self, tmp_path):
        path = tmp_path / "library.json"
        store = LibraryStore(path)
        store.add_history(source_url="http://x/a", title="A")
        store.upsert_library(source_url="http://x/a", title="A", chapter_count=1)
        store.clear(clear_library=True, clear_history=True)
        assert store.get_library() == []
        assert store.get_history() == []


class TestMergeLibrary:
    def test_newer_cursor_wins(self):
        local = LibraryData(library=[
            LibraryEntry(
                source_url="http://a",
                title="A",
                chapter_count=10,
                last_downloaded_at=100,
                last_chapter_url="http://a/10",
            )
        ])
        remote = LibraryData(library=[
            LibraryEntry(
                source_url="http://a",
                title="A",
                chapter_count=12,
                last_downloaded_at=200,
                last_chapter_url="http://a/12",
            )
        ])
        merged = merge_library(local, remote)
        assert len(merged.library) == 1
        assert merged.library[0].chapter_count == 12
        assert merged.library[0].last_chapter_url == "http://a/12"

    def test_union_of_novels(self):
        local = LibraryData(library=[
            LibraryEntry(source_url="http://a", title="A", last_downloaded_at=1),
        ])
        remote = LibraryData(library=[
            LibraryEntry(source_url="http://b", title="B", last_downloaded_at=2),
        ])
        merged = merge_library(local, remote)
        urls = {e.source_url for e in merged.library}
        assert urls == {"http://a", "http://b"}

    def test_keeps_local_path_when_remote_wins_cursor(self):
        local = LibraryData(library=[
            LibraryEntry(
                source_url="http://a",
                chapter_count=5,
                last_downloaded_at=50,
                output_path="/local/A.epub",
            )
        ])
        remote = LibraryData(library=[
            LibraryEntry(
                source_url="http://a",
                chapter_count=9,
                last_downloaded_at=90,
                epub_filename="A.epub",
            )
        ])
        merged = merge_library(local, remote)
        e = merged.library[0]
        assert e.chapter_count == 9
        assert e.output_path == "/local/A.epub"
        assert e.epub_filename == "A.epub"

    def test_history_cap_and_newest(self):
        local = LibraryData(history=[
            HistoryEntry(source_url="http://a", downloaded_at=10, title="old"),
        ])
        remote = LibraryData(history=[
            HistoryEntry(source_url="http://a", downloaded_at=20, title="new"),
            HistoryEntry(source_url="http://b", downloaded_at=15, title="B"),
        ])
        merged = merge_library(local, remote)
        by_url = {h.source_url: h for h in merged.history}
        assert by_url["http://a"].title == "new"
        assert "http://b" in by_url

    def test_hash_stable(self):
        data = LibraryData(library=[
            LibraryEntry(source_url="http://a", title="A"),
        ])
        payload = library_data_to_dict(data)
        assert library_payload_hash(payload) == library_payload_hash(payload)

    def test_tombstone_blocks_remote_resurrect(self):
        local = LibraryData(
            library=[],
            removed=[RemovedEntry(source_url="http://a", removed_at=500)],
        )
        remote = LibraryData(library=[
            LibraryEntry(
                source_url="http://a",
                title="A",
                last_downloaded_at=100,
            )
        ])
        merged = merge_library(local, remote)
        assert merged.library == []
        assert merged.removed[0].source_url == "http://a"

    def test_newer_download_clears_tombstone(self):
        local = LibraryData(
            removed=[RemovedEntry(source_url="http://a", removed_at=100)],
        )
        remote = LibraryData(library=[
            LibraryEntry(
                source_url="http://a",
                title="A",
                last_downloaded_at=200,
            )
        ])
        merged = merge_library(local, remote)
        assert len(merged.library) == 1
        assert merged.removed == []

    def test_upsert_after_remove_clears_tombstone(self, tmp_path):
        store = LibraryStore(tmp_path / "library.json")
        store.upsert_library(source_url="http://x/book", title="Book", chapter_count=1)
        store.remove_library("http://x/book")
        assert store.get_library() == []
        store.upsert_library(source_url="http://x/book", title="Book", chapter_count=2)
        assert len(store.get_library()) == 1
        assert store.get_removed() == []

    def test_purge_deletes_local_epub(self, tmp_path):
        epub = tmp_path / "books" / "A.epub"
        epub.parent.mkdir()
        epub.write_bytes(b"epub")
        other = tmp_path / "books" / "B.epub"
        other.write_bytes(b"keep")
        entry = LibraryEntry(
            source_url="http://a",
            output_path=str(epub),
            epub_filename="A.epub",
        )
        purge_novel_artifacts(
            entry, extra_dirs=[tmp_path / "books"], data_dir=tmp_path
        )
        assert not epub.exists()
        assert other.exists()

    def test_purge_does_not_delete_epub_outside_books(self, tmp_path):
        victim = tmp_path / "victim.epub"
        victim.write_bytes(b"keep")
        books = tmp_path / "books"
        books.mkdir()
        entry = LibraryEntry(
            source_url="http://a",
            output_path=str(victim),
            epub_filename="../../victim.epub",
        )
        purge_novel_artifacts(entry, extra_dirs=[books], data_dir=tmp_path)
        assert victim.exists()


class TestShelving:
    def test_shelved_book_is_kept_and_found_again(self, tmp_path):
        store = LibraryStore(tmp_path / "library.json")
        store.upsert_library("https://a.test/book/1", title="废柴逆袭", translated_title="Bad Book")
        assert store.set_shelved(["https://a.test/book/1"], True) == 1
        again = LibraryStore(tmp_path / "library.json")       # survives a restart
        found = again.find_shelved("https://a.test/book/1")
        assert found is not None and found.shelved_at > 0

    def test_same_title_on_another_site_is_recognised(self, tmp_path):
        store = LibraryStore(tmp_path / "library.json")
        store.upsert_library("https://a.test/book/1", title="废柴 逆袭", translated_title="Bad Book")
        store.set_shelved(["https://a.test/book/1"], True)
        found = store.find_shelved("https://b.test/novel/99", "废柴逆袭")
        assert found is not None and found.source_url == "https://a.test/book/1"

    def test_books_not_shelved_are_not_reported(self, tmp_path):
        store = LibraryStore(tmp_path / "library.json")
        store.upsert_library("https://a.test/book/1", title="好书")
        assert store.find_shelved("https://a.test/book/1", "好书") is None

    def test_update_keeps_the_book_shelved(self, tmp_path):
        store = LibraryStore(tmp_path / "library.json")
        store.upsert_library("https://a.test/book/1", title="书名", chapter_count=3)
        store.set_shelved(["https://a.test/book/1"], True)
        store.upsert_library("https://a.test/book/1", title="书名", chapter_count=5)
        assert store.get_library_entry("https://a.test/book/1").shelved_at > 0

    def test_put_back_clears_the_shelf(self, tmp_path):
        store = LibraryStore(tmp_path / "library.json")
        store.upsert_library("https://a.test/book/1", title="书名")
        store.set_shelved(["https://a.test/book/1"], True)
        assert store.set_shelved(["https://a.test/book/1"], False) == 1
        assert store.find_shelved("https://a.test/book/1") is None
