"""Tests for core.download_runner helpers."""

from pathlib import Path

import pytest

from core.download_runner import (
    DownloadControl,
    DownloadCancelled,
    EpubBuildResult,
    _chapter_note_for_slot,
    _forward_progress,
    _translation_status_line,
    backend_prefetches_during_fetch,
    build_epub,
    download_chapters_with_cache,
    engines_for_chapter_fetch,
    epub_path,
    downloads_folder,
    epub_translate_kwargs,
    eta_from_network_samples,
    eta_from_pack_samples,
    format_completion_notes,
    completion_dialog_title,
    completion_has_warnings,
    run_single_download,
    translator_progress_label,
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

    kw3 = epub_translate_kwargs({"translation_backend": "ctranslate2"})
    assert kw3["backend"] == "ctranslate2"
    assert kw3["glossary_mode"] == "auto"

    kw4 = epub_translate_kwargs({}, {"glossary": "xianxia"})
    assert kw4["glossary_mode"] == "xianxia"

    kw5 = epub_translate_kwargs({"translation_glossary": "off"}, {"glossary": "names"})
    assert kw5["glossary_mode"] == "user"


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


def test_eta_from_pack_samples():
    assert eta_from_pack_samples(10.0, 5, 5) == "  (ETA 10s)"
    assert eta_from_pack_samples(10.0, 1, 5) == ""
    assert eta_from_pack_samples(10.0, 5, 0) == ""


def test_translator_progress_label_covers_every_engine():
    assert translator_progress_label("google") == "Google"
    assert translator_progress_label("google_html") == "Google HTML"
    assert translator_progress_label("google_gtx") == "Google Old"
    assert translator_progress_label("microsoft") == "Microsoft"
    assert translator_progress_label("libretranslate") == "LibreTranslate"
    assert translator_progress_label("ollama") == "Ollama"
    assert translator_progress_label("ctranslate2") == "Offline NMT"
    assert translator_progress_label("unknown") == "Translate"


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

    def count_cached_urls(self, urls):
        return sum(1 for u in urls if self.mapping.get(u))

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


def test_cache_lookup_does_not_preload_html_before_first_fetch():
    """Google used to sit on a full SQLite HTML scan; fetch after one lookup."""
    chapters = [
        Chapter(title=f"Ch {i}", url=f"https://example.com/{i}", content="")
        for i in range(3)
    ]
    gets_before_fetch = []

    class _CountingCache(_MemCache):
        def get_chapter(self, url):
            if not fetches:
                gets_before_fetch.append(url)
            return super().get_chapter(url)

    class _P(_Parser):
        def get_chapter_content(self, chapter):
            fetches.append(chapter.url)
            return f"<p>fresh {chapter.url}</p>"

    fetches = []
    statuses = []
    download_chapters_with_cache(
        control=DownloadControl(),
        cache=_CountingCache(),
        parser=_P(),
        chapters=chapters,
        book_key="https://example.com/book",
        use_cache=True,
        set_status=statuses.append,
        set_progress=lambda _f: None,
    )
    assert fetches[0] == chapters[0].url
    assert gets_before_fetch == [chapters[0].url]
    assert statuses
    assert statuses[0].startswith("Fetching chapters [1/3]")
    assert any("Checking chapter cache" in s for s in statuses) is False


def test_fetch_status_includes_n_of_n_and_eta(monkeypatch):
    clock = {"t": 10.0}

    def mono():
        return clock["t"]

    class _TimedParser:
        request_delay = 0

        def get_chapter_content(self, chapter):
            clock["t"] += 2.0
            return f"<p>fresh {chapter.url}</p>"

    monkeypatch.setattr("core.download_runner.time.monotonic", mono)
    chapters = [
        Chapter(title=f"Ch {i}", url=f"https://example.com/{i}", content="")
        for i in range(3)
    ]
    statuses = []
    download_chapters_with_cache(
        control=DownloadControl(),
        cache=_MemCache(),
        parser=_TimedParser(),
        chapters=chapters,
        book_key="https://example.com/book",
        use_cache=True,
        set_status=statuses.append,
        set_progress=lambda _f, _s="": None,
    )
    fetch_lines = [s for s in statuses if "Fetching chapters [" in s]
    assert fetch_lines
    assert any("[1/3]" in s for s in fetch_lines)
    assert any("[2/3]" in s for s in fetch_lines)
    assert any("ETA" in s for s in fetch_lines)


def test_translation_status_line_zero_of_n_before_http():
    line = _translation_status_line(
        "Google",
        0,
        51399,
        unique_requests=47278,
        in_flight=8,
    )
    assert line.startswith("Google · Translating: 0/51399")
    assert "47278 unique requests" in line
    assert "8 in flight" in line
    retry = _translation_status_line(
        "Google", 4, 20, retry_pass=2, in_flight=8
    )
    assert retry.startswith("Google · Retry pass 2: 4/20")
    libre = _translation_status_line(
        "LibreTranslate", 0, 100, pack_done=0, pack_total=12, in_flight=4
    )
    assert "LibreTranslate · Translating: 0/100" in libre
    assert "0/12 packs" in libre


def test_cancel_during_cache_scan_raises():
    chapters = [
        Chapter(title=f"Ch {i}", url=f"https://example.com/{i}", content="")
        for i in range(4)
    ]
    ctrl = DownloadControl()

    class _SlowCache(_MemCache):
        def get_chapter(self, url):
            ctrl.cancel_requested = True
            return super().get_chapter(url)

    with pytest.raises(DownloadCancelled):
        download_chapters_with_cache(
            control=ctrl,
            cache=_SlowCache(),
            parser=_Parser(),
            chapters=chapters,
            book_key="https://example.com/book",
            use_cache=True,
            set_status=lambda _s: None,
            set_progress=lambda _f: None,
        )


def test_backend_prefetches_during_fetch_only_libre_and_nmt():
    assert backend_prefetches_during_fetch("google") is False
    assert backend_prefetches_during_fetch("google_html") is False
    assert backend_prefetches_during_fetch("google_gtx") is False
    assert backend_prefetches_during_fetch("microsoft") is False
    assert backend_prefetches_during_fetch("ollama") is False
    assert backend_prefetches_during_fetch("libretranslate") is True
    assert backend_prefetches_during_fetch("ctranslate2") is True
    assert backend_prefetches_during_fetch("offline_nmt") is True


def test_google_engines_for_fetch_skip_translator(monkeypatch):
    monkeypatch.setattr(
        "core.download_runner.prepare_translation",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("Google must not build NovelTranslator before fetch")
        ),
    )
    translator, cleaner = engines_for_chapter_fetch(
        cache=_MemCache(),
        workers=8,
        backend="google",
        libretranslate_url="",
        ollama_url="",
        ollama_model="",
        clean=True,
        translate=True,
        glossary_mode="auto",
    )
    assert translator is None
    assert cleaner is not None


def test_libre_engines_for_fetch_builds_translator(monkeypatch):
    sentinel = object()

    def fake_prepare(**kwargs):
        assert kwargs["backend"] == "libretranslate"
        return sentinel, object()

    monkeypatch.setattr("core.download_runner.prepare_translation", fake_prepare)
    translator, _cleaner = engines_for_chapter_fetch(
        cache=_MemCache(),
        workers=8,
        backend="libretranslate",
        libretranslate_url="https://libretranslate.com",
        ollama_url="",
        ollama_model="",
        clean=False,
        translate=True,
        glossary_mode="auto",
    )
    assert translator is sentinel


def test_google_run_starts_fetch_before_translator(tmp_path, monkeypatch):
    order = []

    def fake_prepare(**_k):
        order.append("prepare")
        return None, None

    def fake_build(**kwargs):
        order.append("build")
        return EpubBuildResult(output_path=kwargs["output_path"])

    monkeypatch.setattr("core.download_runner.prepare_translation", fake_prepare)
    monkeypatch.setattr("core.download_runner.build_epub", fake_build)
    monkeypatch.setattr(
        "core.download_runner.record_successful_download", lambda *_a, **_k: None
    )

    class _P(_Parser):
        def get_chapter_content(self, chapter):
            order.append("fetch")
            return "<p>ok</p>"

    info = NovelInfo(title="T", source_url="https://example.com/book")
    chapters = [Chapter(title="Ch 1", url="https://example.com/1", content="")]
    run_single_download(
        control=DownloadControl(),
        cache=_MemCache(),
        library_store=object(),
        parser=_P(),
        info=info,
        chapters=chapters,
        output_path=str(tmp_path / "book.epub"),
        translated_title="T",
        use_cache=True,
        clean=True,
        translate=True,
        workers=8,
        backend="google",
        libretranslate_url="",
        set_status=lambda _s: None,
        set_progress=lambda _f: None,
        glossary_mode="auto",
    )
    assert order[0] == "fetch"
    assert "prepare" not in order
    assert "build" in order


def test_libre_run_prepares_translator_before_fetch(tmp_path, monkeypatch):
    order = []

    def fake_prepare(**_k):
        order.append("prepare")
        return object(), None

    def fake_build(**kwargs):
        order.append("build")
        return EpubBuildResult(output_path=kwargs["output_path"])

    monkeypatch.setattr("core.download_runner.prepare_translation", fake_prepare)
    monkeypatch.setattr("core.download_runner.build_epub", fake_build)
    monkeypatch.setattr(
        "core.download_runner.record_successful_download", lambda *_a, **_k: None
    )

    class _P(_Parser):
        def get_chapter_content(self, chapter):
            order.append("fetch")
            return "<p>ok</p>"

    info = NovelInfo(title="T", source_url="https://example.com/book")
    chapters = [Chapter(title="Ch 1", url="https://example.com/1", content="")]
    run_single_download(
        control=DownloadControl(),
        cache=_MemCache(),
        library_store=object(),
        parser=_P(),
        info=info,
        chapters=chapters,
        output_path=str(tmp_path / "book.epub"),
        translated_title="T",
        use_cache=True,
        clean=False,
        translate=True,
        workers=8,
        backend="libretranslate",
        libretranslate_url="https://libretranslate.com",
        set_status=lambda _s: None,
        set_progress=lambda _f: None,
        glossary_mode="auto",
    )
    assert order[0] == "prepare"
    assert order.index("prepare") < order.index("fetch")


class _FakeTranslator:
    def __init__(self, control, cancel_at=None, keep_chinese=False, backend="google"):
        self._cancel_requested = False
        self.control = control
        self.cancel_at = cancel_at
        self.keep_chinese = keep_chinese
        self.backend = backend
        self._in_flight = 0
        self._progress_source_index = -1
        self.pack_done = 0
        self.pack_total = 0
        self._unique_requests = 0
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
        self._in_flight = 8
        self._unique_requests = len(texts)
        self._progress_source_index = max(0, len(texts) - 1)
        if progress:
            progress(0, len(texts))
        for i, text in enumerate(texts):
            self.stats["requests"] += 1
            if progress:
                progress(i + 1, len(texts))
            if self.keep_chinese:
                out.append(text)
            else:
                out.append("Hello world. This is translated English.")
        self._in_flight = 0
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


def test_prefetch_chinese_pairs_are_still_translated(tmp_path, monkeypatch):
    """Failed prefetch must not skip the final retry pass."""
    dest = tmp_path / "book.epub"
    ctrl = DownloadControl()
    fake = _FakeTranslator(ctrl)
    monkeypatch.setattr("core.download_runner.make_translator", lambda **k: fake)
    ch = _zh_chapter()
    body = "这是一段用于测试的中文正文" * 8
    ch.cleaned_html = f"<p>{body}</p>"
    ch.translation_applied = True
    ch.translation_pairs = [(body, body)]
    result = build_epub(
        control=ctrl,
        cache=_MemCache(),
        info=_zh_info(),
        chapters=[ch],
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
    assert fake.stats["requests"] >= 4
    assert not result.translation_warnings


def test_translation_status_line_has_engine_inflight_chapter_eta():
    line = _translation_status_line(
        "Google",
        4,
        20,
        cache_hits=1,
        in_flight=8,
        chapter_note=" · ch 1/3 Ch 0",
        eta="  (ETA 9s)",
    )
    assert line == (
        "Google · Translating: 4/20 · 1 cached · 8 in flight · ch 1/3 Ch 0  (ETA 9s)"
    )
    retry = _translation_status_line(
        "Microsoft", 2, 10, retry_pass=2, in_flight=4
    )
    assert retry.startswith("Microsoft · Retry pass 2: 2/10")
    assert "4 in flight" in retry
    packed = _translation_status_line(
        "LibreTranslate",
        10,
        100,
        pack_done=3,
        pack_total=20,
        in_flight=2,
        eta="  (ETA 1m)",
    )
    assert packed == (
        "LibreTranslate · Translating: 10/100 · 3/20 packs · 2 in flight  (ETA 1m)"
    )
    nmt = _translation_status_line("Offline NMT", 5, 40, in_flight=32)
    assert nmt.startswith("Offline NMT · Translating: 5/40")
    assert "32 in flight" in nmt
    start = _translation_status_line(
        "Google", 0, 51399, unique_requests=47278, in_flight=8
    )
    assert start.startswith("Google · Translating: 0/51399")
    assert "47278 unique requests" in start
    assert "8 in flight" in start
    later = _translation_status_line(
        "Google",
        4,
        51399,
        unique_requests=47278,
        in_flight=8,
        network_requests=4,
    )
    assert "unique requests" not in later
    grouped = _translation_status_line(
        "Google",
        100,
        51399,
        unique_requests=47278,
        in_flight=8,
        network_requests=0,
    )
    assert "47278 unique requests" in grouped


def test_chapter_note_for_slot_names_current_chapter():
    chapters = [
        Chapter(title="第一章 开始", url="https://example.com/1"),
        Chapter(title="A very long chapter title that should truncate", url="https://example.com/2"),
    ]
    texts = [
        ("title", 0, "书名"),
        ("content", 0, "正文"),
        ("content", 1, "更多"),
    ]
    assert _chapter_note_for_slot(texts, chapters, 1, 0) == " · novel title"
    assert _chapter_note_for_slot(texts, chapters, 1, 1) == " · ch 1/2 第一章 开始"
    note = _chapter_note_for_slot(texts, chapters, 2, 2)
    assert note.startswith(" · ch 2/2 ")
    assert note.endswith("…")


def test_forward_progress_always_calls_set_status():
    """Two-arg set_progress used to skip set_status (TypeError-only path)."""
    statuses = []
    seen = []

    def set_progress(fraction, status=""):
        seen.append((fraction, status))

    _forward_progress(set_progress, statuses.append, 0.4, "Google · Translating: 4/20")
    assert statuses == ["Google · Translating: 4/20"]
    assert seen == [(0.4, "Google · Translating: 4/20")]

    statuses.clear()
    one_arg = []
    _forward_progress(one_arg.append, statuses.append, 0.5, "Fetching chapters [1/3]: Ch")
    assert one_arg == [0.5]
    assert statuses == ["Fetching chapters [1/3]: Ch"]


def test_google_translate_pass_does_not_wait_on_qwen(tmp_path, monkeypatch):
    """Qwen classify is Help-menu / startup, not an inline gate before Google."""
    dest = tmp_path / "book.epub"
    ctrl = DownloadControl()
    fake = _FakeTranslator(ctrl)
    fake.harvest_calls = 0
    fake.qwen_calls = 0

    def harvest(_texts, novel_title=""):
        fake.harvest_calls += 1
        return 0

    def classify(*_a, **_k):
        fake.qwen_calls += 1
        raise AssertionError("Qwen must not gate the Google translate pass")

    fake.harvest_names_from_texts = harvest
    fake.classify_glossary_with_qwen = classify
    monkeypatch.setattr("core.download_runner.make_translator", lambda **k: fake)
    statuses = []
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
        set_status=statuses.append,
        set_progress=lambda _f, _s="": None,
    )
    assert dest.is_file()
    assert isinstance(result, EpubBuildResult)
    assert fake.harvest_calls == 1
    assert fake.qwen_calls == 0
    assert any("Google · Translating:" in s for s in statuses)
    assert not any("Qwen" in s for s in statuses)


def test_build_epub_streams_live_translate_status_via_set_status(tmp_path, monkeypatch):
    """Live counters must reach set_status even if set_progress swallows status."""
    dest = tmp_path / "book.epub"
    ctrl = DownloadControl()
    fake = _FakeTranslator(ctrl)
    monkeypatch.setattr("core.download_runner.make_translator", lambda **k: fake)
    statuses = []
    fractions = []

    def set_progress(fraction, status=""):
        fractions.append(fraction)
        # Swallow status — the old try/except TypeError path never called set_status.

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
        set_status=statuses.append,
        set_progress=set_progress,
        ollama_polish=True,
    )
    assert dest.is_file()
    assert isinstance(result, EpubBuildResult)
    joined = "\n".join(statuses)
    assert any("Google · Translating:" in s for s in statuses)
    assert any("in flight" in s for s in statuses)
    assert any("ch 1/1" in s for s in statuses)
    assert any(s.startswith("Polishing English:") for s in statuses)
    assert any("Writing EPUB" in s or "Adding chapter:" in s for s in statuses)
    assert "Starting download…" not in joined
    assert fractions
    assert any(f > 0 for f in fractions)


def test_build_epub_emits_translate_status_before_http(tmp_path, monkeypatch):
    """Segment count must reach set_status before the first engine GET returns."""
    import threading

    dest = tmp_path / "book.epub"
    ctrl = DownloadControl()
    entered_http = threading.Event()
    allow_http = threading.Event()
    statuses = []
    fractions = []

    class BlockingTranslator(_FakeTranslator):
        def translate_texts_with_retry(self, texts, progress=None, **kwargs):
            self._in_flight = 8
            self._unique_requests = len(texts)
            if progress:
                progress(0, len(texts))
            entered_http.set()
            assert allow_http.wait(5)
            return ["Hello world. This is translated English."] * len(texts)

    fake = BlockingTranslator(ctrl)
    monkeypatch.setattr("core.download_runner.make_translator", lambda **k: fake)

    def run():
        build_epub(
            control=ctrl,
            cache=_MemCache(),
            info=_zh_info(),
            chapters=[_zh_chapter()],
            output_path=str(dest),
            clean=True,
            translate=True,
            workers=200,
            backend="google",
            libretranslate_url="",
            set_status=statuses.append,
            set_progress=lambda f, _s="": fractions.append(f),
        )

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered_http.wait(8), "translate pass never started"
        joined = "\n".join(statuses)
        assert any("Google · Translating:" in s for s in statuses)
        assert "Starting download…" not in joined
        assert fractions
        assert any(f > 0 for f in fractions)
        assert any("in flight" in s for s in statuses)
    finally:
        allow_http.set()
        thread.join(8)
        assert not thread.is_alive()
