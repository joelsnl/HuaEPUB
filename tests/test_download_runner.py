"""Tests for core.download_runner helpers."""

from pathlib import Path

import pytest

from core.download_runner import (
    DownloadControl,
    DownloadCancelled,
    EpubBuildResult,
    build_epub,
    download_chapters_with_cache,
    epub_path,
    downloads_folder,
    epub_translate_kwargs,
    eta_from_network_samples,
    format_completion_notes,
    completion_dialog_title,
    completion_has_warnings,
)
from core.parser import Chapter, NovelInfo


def test_epub_path_preferred_strips_copy_suffix(tmp_path):
    p = epub_path(tmp_path, "Title", preferred_name="Book (1).epub")
    assert Path(p).name == "Book.epub"


def test_epub_path_strips_parent_directories(tmp_path):
    p = epub_path(tmp_path, "Title", preferred_name=r"..\..\Windows\evil.epub")
    assert Path(p).resolve().parent == tmp_path.resolve()
    assert Path(p).name == "evil.epub"


def test_downloads_folder_custom(tmp_path):
    d = tmp_path / "out"
    assert downloads_folder(str(d)) == d
    assert d.is_dir()


def test_epub_translate_kwargs_includes_polish():
    kw = epub_translate_kwargs(
        {"translation_backend": "google", "ollama_polish": True}
    )
    assert kw["backend"] == "google"
    assert kw["ollama_polish"] is True
    assert kw["ollama_model"] == "qwen2.5:3b"

    kw2 = epub_translate_kwargs({}, {"backend": "ollama", "ollama_polish": True})
    assert kw2["backend"] == "ollama"
    assert kw2["ollama_polish"] is True


def test_pause_and_cancel():
    ctrl = DownloadControl()
    ctrl.is_paused = True
    ctrl.cancel_requested = True
    try:
        ctrl.wait_while_paused()
        assert False, "expected cancel"
    except DownloadCancelled:
        pass


def test_eta_from_network_samples():
    assert eta_from_network_samples(10.0, 5, 5) == "  (ETA 10s)"
    assert eta_from_network_samples(0.0, 5, 5) == ""
    assert eta_from_network_samples(10.0, 0, 5) == ""
    assert eta_from_network_samples(10.0, 5, 0) == ""


def test_format_completion_notes_polish_and_warnings():
    notes = format_completion_notes(
        failed_chapters=["Ch 1"],
        translation_warnings=[("Chapter 2", 80)],
        polish_cancelled=True,
        heuristic_chapters=["Chapter 9"],
    )
    assert "Polish was stopped" in notes
    assert "1 chapter(s) had placeholders" in notes
    assert "significant Chinese" in notes
    assert "generic content guess" in notes
    assert "Chapter 9" in notes
    assert format_completion_notes() == ""


def test_completion_dialog_title_never_says_success_with_warnings():
    clean = "Completed: 2/2 novels\n\n  • Book.epub\n"
    assert completion_has_warnings(clean) is False
    assert completion_dialog_title(clean, "Multi-download complete") == (
        "Multi-download complete"
    )
    warned = clean + "\nPolish was stopped — EPUB saved with machine translation."
    assert completion_dialog_title(warned, "Library updated") == "Saved with warnings"
    failed = "Update All: 1/2 succeeded\n  • Other: download failed\n"
    assert completion_dialog_title(failed, "Update All") == "Saved with warnings"
    partial = "Completed: 1/3 novels\n\n  • Book: HTTP 403\n"
    assert completion_dialog_title(partial, "Multi-download complete") == (
        "Saved with warnings"
    )


class _MemCache:
    def __init__(self, mapping=None):
        self.mapping = dict(mapping or {})

    def get_chapter(self, url):
        return self.mapping.get(url)

    def put_chapter(self, book_key, url, title, content):
        self.mapping[url] = content

    def get_cover(self, *a, **k):
        return None

    def put_cover(self, *a, **k):
        pass

    def get_translation(self, *a, **k):
        return None

    def put_translation(self, *a, **k):
        pass

    def delete_translation(self, *a, **k):
        pass


class _Parser:
    request_delay = 0

    def get_chapter_content(self, chapter):
        return f"<p>fresh {chapter.url}</p>"


def test_library_update_eta_ignores_cache_hits():
    """Cached chapters must not produce a fake ETA before any network fetch."""
    chapters = [
        Chapter(title=f"Ch {i}", url=f"https://example.com/{i}", content="")
        for i in range(5)
    ]
    cache = _MemCache({ch.url: f"<p>cached {i}</p>" for i, ch in enumerate(chapters[:-1])})
    statuses = []
    download_chapters_with_cache(
        control=DownloadControl(),
        cache=cache,
        parser=_Parser(),
        chapters=chapters,
        book_key="https://example.com/book",
        use_cache=True,
        set_status=statuses.append,
        set_progress=lambda _f: None,
    )
    cache_lines = [s for s in statuses if s.startswith("Cached")]
    assert cache_lines
    assert all("ETA" not in s for s in cache_lines)
    assert any("Fetching chapters [1/1]" in s for s in statuses)
    assert chapters[-1].content == "<p>fresh https://example.com/4</p>"


class _FakeTranslator:
    def __init__(self, control, cancel_at=None, keep_chinese=False):
        self._cancel_requested = False
        self.control = control
        self.cancel_at = cancel_at
        self.keep_chinese = keep_chinese
        self.stats = {
            "requests": 0, "cache_hits": 0, "retry_passes": 0,
            "paragraphs_translated": 0, "characters_translated": 0,
            "retries": 0, "errors": 0,
        }

    def cancel(self):
        self._cancel_requested = True

    def translate_texts_with_retry(self, texts, progress=None, **kwargs):
        if self.cancel_at == "translate":
            self.control.cancel_requested = True
            self.cancel()
        out = []
        for i, text in enumerate(texts):
            self.stats["requests"] += 1
            if progress:
                progress(i + 1, len(texts))
            if self.keep_chinese:
                out.append(text)
            else:
                out.append("Hello world. This is translated English.")
        return out

    def polish_texts(self, texts, progress=None):
        if self.cancel_at == "polish":
            self.control.cancel_requested = True
            self.cancel()
        if progress:
            progress(1, max(len(texts), 1))
        return list(texts)


def _zh_chapter():
    body = "这是一段用于测试的中文正文" * 8
    return Chapter(title="第一章 开始", url="https://example.com/1", content=f"<p>{body}</p>")


def _zh_info():
    return NovelInfo(title="测试小说", author="作者", source_url="https://example.com/book")


def test_cancel_during_translate_does_not_write_epub(tmp_path, monkeypatch):
    dest = tmp_path / "book.epub"
    ctrl = DownloadControl()
    fake = _FakeTranslator(ctrl, cancel_at="translate")
    monkeypatch.setattr("core.download_runner.make_translator", lambda **k: fake)
    with pytest.raises(DownloadCancelled):
        build_epub(
            control=ctrl,
            cache=_MemCache(),
            info=_zh_info(),
            chapters=[_zh_chapter()],
            output_path=str(dest),
            clean=True,
            translate=True,
            workers=1,
            backend="google",
            libretranslate_url="",
            set_status=lambda _s: None,
            set_progress=lambda _f: None,
        )
    assert not dest.exists()
    assert not (tmp_path / "book.epub.tmp").exists()


def test_cancel_during_polish_still_writes_epub(tmp_path, monkeypatch):
    dest = tmp_path / "book.epub"
    ctrl = DownloadControl()
    fake = _FakeTranslator(ctrl, cancel_at="polish")
    monkeypatch.setattr("core.download_runner.make_translator", lambda **k: fake)
    result = build_epub(
        control=ctrl,
        cache=_MemCache(),
        info=_zh_info(),
        chapters=[_zh_chapter()],
        output_path=str(dest),
        clean=True,
        translate=True,
        workers=1,
        backend="google",
        libretranslate_url="",
        set_status=lambda _s: None,
        set_progress=lambda _f: None,
        ollama_polish=True,
    )
    assert isinstance(result, EpubBuildResult)
    assert result.polish_cancelled is True
    assert dest.is_file()
    assert dest.stat().st_size > 0
    assert not (tmp_path / "book.epub.tmp").exists()


def test_build_epub_returns_translation_warnings(tmp_path, monkeypatch):
    dest = tmp_path / "book.epub"
    ctrl = DownloadControl()
    fake = _FakeTranslator(ctrl, keep_chinese=True)
    monkeypatch.setattr("core.download_runner.make_translator", lambda **k: fake)
    result = build_epub(
        control=ctrl,
        cache=_MemCache(),
        info=_zh_info(),
        chapters=[_zh_chapter()],
        output_path=str(dest),
        clean=True,
        translate=True,
        workers=1,
        backend="google",
        libretranslate_url="",
        set_status=lambda _s: None,
        set_progress=lambda _f: None,
    )
    assert dest.is_file()
    assert result.translation_warnings
    assert result.translation_warnings[0][1] > 50
