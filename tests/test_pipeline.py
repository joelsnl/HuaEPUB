"""Tiny offline pipeline: cached chapters → real EPUB bytes (no network)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.cache import NovelCache
from core.download_runner import DownloadCancelled, DownloadControl
from core.library import LibraryStore
from core.parser import Chapter, NovelInfo
from core.tasks import download_one_novel


class _NoNetworkParser:
    request_delay = 0

    def get_chapter_content(self, chapter):
        raise AssertionError(f"pipeline test must not hit the network: {chapter.url}")


def test_cached_chapters_build_real_epub(tmp_path):
    dest = tmp_path / "A Test Novel.epub"
    cache = NovelCache(tmp_path / "cache.db", max_bytes=0)
    store = LibraryStore(tmp_path / "library.json")
    info = NovelInfo(
        title="A Test Novel",
        author="Tester",
        description="Offline pipeline",
        source_url="https://example.com/book/pipeline",
    )
    chapters = [
        Chapter(
            title="Chapter 1",
            url="https://example.com/book/pipeline/1",
            content="",
            index=1,
        ),
        Chapter(
            title="Chapter 2",
            url="https://example.com/book/pipeline/2",
            content="",
            index=2,
        ),
    ]
    cache.put_chapter(
        info.source_url, chapters[0].url, chapters[0].title,
        "<p>First chapter body with enough words.</p>",
    )
    cache.put_chapter(
        info.source_url, chapters[1].url, chapters[1].title,
        "<p>Second chapter body with enough words.</p>",
    )

    session = SimpleNamespace(control=DownloadControl(), cache=cache, library_store=store,
                              settings={})
    options = {"use_cache": True, "clean": True, "translate": False, "workers": 1,
               "backend": "google"}
    failed, result = download_one_novel(
        session, _NoNetworkParser(), info, chapters, str(dest), "A Test Novel", options,
        book_key=info.source_url, set_status=lambda _s: None,
        set_progress=lambda _f, _s="": None, set_build_progress=lambda _f, _s="": None,
    )

    assert failed == []
    assert dest.is_file()
    assert dest.stat().st_size > 200
    assert not dest.with_name(dest.name + ".tmp").exists()
    assert result.output_path == str(dest)
    assert result.translation_warnings == []
    assert result.polish_cancelled is False

    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
        assert "mimetype" in names
        assert zf.read("mimetype") == b"application/epub+zip"
        assert any(n.endswith(".xhtml") or n.endswith(".html") for n in names)
        assert any(n.endswith(".smil") for n in names)

    lib = store.get_library()
    assert any(e.source_url == info.source_url for e in lib)


def test_cancelled_streaming_download_leaves_no_tmp_file(tmp_path):
    """A plain download streams straight into the EPUB; cancelling mid-fetch
    must not leave a half-written .epub.tmp behind."""
    dest = tmp_path / "Cancelled Novel.epub"
    cache = NovelCache(tmp_path / "cache.db", max_bytes=0)
    store = LibraryStore(tmp_path / "library.json")
    info = NovelInfo(title="Cancelled Novel", author="Tester",
                      source_url="https://example.com/book/cancel")
    chapters = [
        Chapter(title=f"Ch {i}", url=f"https://example.com/book/cancel/{i}", content="", index=i)
        for i in range(5)
    ]
    control = DownloadControl()

    class _CancelAfterFirst:
        request_delay = 0

        def get_chapter_content(self, chapter):
            if chapter.index == 1:
                control.cancel_requested = True
            return f"<p>content {chapter.index}</p>"

    session = SimpleNamespace(control=control, cache=cache, library_store=store, settings={})
    options = {"use_cache": True, "clean": True, "translate": False, "workers": 1,
               "backend": "google"}
    with pytest.raises(DownloadCancelled):
        download_one_novel(
            session, _CancelAfterFirst(), info, chapters, str(dest), "Cancelled Novel", options,
            book_key=info.source_url, set_status=lambda _s: None,
            set_progress=lambda _f, _s="": None, set_build_progress=lambda _f, _s="": None,
        )

    assert not dest.exists()
    assert not Path(str(dest) + ".tmp").exists()
