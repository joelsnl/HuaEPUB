"""The translate stage of a build: what it tells the UI, and what it does on cancel and polish.

Scripted fakes stand in for the translator and the EPUB builder, and a fake clock makes the
ETA text deterministic. The expected messages were captured from the pipeline before it was
split into stages, so a refactor that changes what the user sees fails here.
"""

import pytest

from core import download_runner
from core.download_runner import DownloadCancelled, translate_then_build
from core.parser import Chapter, NovelInfo


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class FakeTranslator:
    backend = "google"

    def __init__(self, clock, *, retries=(), cancel_after=False, polish_cancel=False):
        self.clock = clock
        self.retries = retries
        self.cancel_after = cancel_after
        self.polish_cancel = polish_cancel
        self.stats = {"requests": 0, "cache_hits": 0}
        self.pack_done = self.pack_total = 0
        self._progress_source_index = -1
        self._unique_requests = 0
        self._in_flight = 0
        self._gtx = None
        self._cancel_requested = False
        self.harvested = []

    def configure_glossary(self, info, chapters):
        if getattr(self, "glossary_error", False):
            raise RuntimeError("glossary failed")

    def wait_prefetch(self):
        pass

    def harvest_names_from_texts(self, texts, novel_title=""):
        self.harvested.append((len(texts), novel_title))

    def translate_texts_with_retry(self, texts, progress, is_chinese_fn=None,
                                   count_chinese_fn=None, pass_callback=None):
        total = len(texts)
        for done in range(1, total + 1):
            self.clock.tick(0.5)
            self.stats["requests"] += 1
            self._in_flight = 4
            progress(done, total)
            if done == 2 and pass_callback:
                for number, (remaining, cooldown) in enumerate(self.retries, 1):
                    pass_callback(number, remaining, total, cooldown)
        self._cancel_requested = self.cancel_after
        return [f"EN {text}" for text in texts]

    def polish_texts(self, texts, progress):
        for done in range(len(texts) + 1):
            self.clock.tick(0.25)
            progress(done, len(texts))
        self._cancel_requested = self.polish_cancel
        return [f"{text} (polished)" for text in texts]


class FakeBuilder:
    def __init__(self, translator, polish=False):
        self.translator = translator
        self.cleaner = None
        self.polish = polish
        self.verify_translation = False
        self.chapters_with_chinese = None
        self.polish_cancelled = None
        self.built = []
        self.applied = []

    def _extract_text_segments(self, html):
        return [part for part in html.replace("<p>", "").split("</p>") if part]

    def apply_translations(self, info, chapters, all_texts, translated):
        self.applied.append(list(translated))

    def build(self, info, chapters, path, progress, skip_html_clean=False, language=None):
        self.built.append((path, skip_html_clean, language))
        return path


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(download_runner.time, "monotonic", fake)
    monkeypatch.setattr(download_runner.time, "sleep", lambda *_a, **_k: None)
    return fake


def _book(chapters=3):
    info = NovelInfo(title="测试小说", author="作者", source_url="https://x/", description="简介内容")
    items = [Chapter(title=f"第{i}章 标题", url=f"https://x/{i}",
                     content=f"<p>第{i}章的第一段话。</p><p>第二段话{i}。</p>")
             for i in range(chapters)]
    return info, items


def _run(builder, chapters=3, callback=True):
    info, items = _book(chapters)
    messages = []

    def report(current, total, message):
        messages.append((current, total, message))

    result = translate_then_build(builder, info, items, "out.epub", report if callback else None)
    return result, messages


def test_status_goes_from_preparing_to_translating_to_done(clock):
    builder = FakeBuilder(FakeTranslator(clock))
    result, messages = _run(builder)
    assert result == "out.epub"
    assert [m[2] for m in messages[:6]] == [
        "Preparing for translation...",
        "Cleaning: 第0章 标题...",
        "Cleaning: 第2章 标题...",
        "Google · Translating: 0/12 · 8 in flight",
        "Learning character names…",
        "Google · Translating: 0/12 · 8 in flight",
    ]
    assert messages[-1] == (6.0, 6, "Google · Translating: 12/12 · 4 in flight · ch 3/3 第2章 标题")
    assert builder.translator.harvested == [(12, "测试小说")]


def test_the_epub_is_written_from_translated_text_without_a_second_clean(clock):
    builder = FakeBuilder(FakeTranslator(clock))
    _run(builder)
    assert builder.built == [("out.epub", True, "en")]
    assert builder.applied[0][0] == "EN 测试小说"


def test_retry_passes_say_so_in_the_status(clock):
    builder = FakeBuilder(FakeTranslator(clock, retries=[(3, 5.0), (1, 0)]))
    _, messages = _run(builder)
    texts = [m[2] for m in messages]
    assert "Google · Retry pass 1: cooling down 5s (3 left)..." in texts
    assert "Google · Retry pass 2: retrying 1 segments..." in texts
    assert any(t.startswith("Google · Retry pass 2: 3/12") for t in texts)


def test_cancel_during_translation_raises_and_writes_no_epub(clock):
    builder = FakeBuilder(FakeTranslator(clock, cancel_after=True))
    with pytest.raises(DownloadCancelled):
        _run(builder)
    assert builder.built == []


def test_polish_reports_progress_and_a_cancel_still_writes_the_epub(clock):
    builder = FakeBuilder(FakeTranslator(clock, polish_cancel=True), polish=True)
    _, messages = _run(builder)
    polishing = [m[2] for m in messages if m[2].startswith("Polishing English")]
    assert polishing[0] == "Polishing English: 0/12"
    assert polishing[-1] == "Polishing English: 12/12"
    assert builder.polish_cancelled is True
    assert builder.built == [("out.epub", True, "en")]
    assert builder.applied[0][0] == "EN 测试小说 (polished)"


def test_a_finished_polish_is_not_reported_as_cancelled(clock):
    builder = FakeBuilder(FakeTranslator(clock), polish=True)
    _run(builder)
    assert builder.polish_cancelled is False


def test_no_progress_callback_still_builds(clock):
    builder = FakeBuilder(FakeTranslator(clock), polish=True)
    result, messages = _run(builder, callback=False)
    assert result == "out.epub" and messages == []


def test_a_glossary_failure_does_not_stop_the_build(clock):
    translator = FakeTranslator(clock)
    translator.glossary_error = True
    builder = FakeBuilder(translator)
    result, _ = _run(builder)
    assert result == "out.epub"


def test_a_book_with_nothing_to_translate_is_just_built(clock):
    builder = FakeBuilder(FakeTranslator(clock))
    info = NovelInfo(title="English Title", author="Someone", source_url="https://x/")
    chapters = [Chapter(title="Chapter 1", url="https://x/1", content="<p>Already English text.</p>")]
    assert translate_then_build(builder, info, chapters, "out.epub") == "out.epub"
    assert builder.applied == [] and builder.built == [("out.epub", True, "en")]
