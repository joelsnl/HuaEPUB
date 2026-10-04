"""HuaEPUB Simple: previews and the package boundary. Offline, no PySide6."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from core.parser import Chapter, NovelInfo
from core.security import UnsafeURLError
from web import preview as preview_mod
from web.preview import PreviewError, PreviewStore, build_preview, translate_preview

ROOT = Path(__file__).resolve().parent.parent


class _FakeParser:
    request_delay = 0


def _patch(monkeypatch, *, parser=True, chapters=None, info=None, fetch_error=None):
    monkeypatch.setattr(preview_mod, "validate_fetch_url", lambda url: None)
    monkeypatch.setattr(
        preview_mod, "get_parser_for_url", lambda url: _FakeParser() if parser else None
    )

    def fake_fetch(_parser, url):
        if fetch_error:
            raise fetch_error
        return info or NovelInfo(title="T", author="A"), chapters

    monkeypatch.setattr(preview_mod, "fetch_info_and_chapters", fake_fetch)


def test_blocked_url_is_rejected_before_any_fetch():
    with pytest.raises(PreviewError) as err:
        build_preview("http://127.0.0.1/book")
    assert err.value.code == "blocked_url"
    assert err.value.status == 400


def test_validator_error_maps_to_blocked(monkeypatch):
    def boom(url):
        raise UnsafeURLError("private host")

    monkeypatch.setattr(preview_mod, "validate_fetch_url", boom)
    with pytest.raises(PreviewError) as err:
        build_preview("https://example.com/b")
    assert err.value.code == "blocked_url"


def test_no_parser(monkeypatch):
    _patch(monkeypatch, parser=False, chapters=[])
    with pytest.raises(PreviewError) as err:
        build_preview("https://example.com/b")
    assert err.value.code == "no_parser"


def test_fetch_failure_is_a_502(monkeypatch):
    _patch(monkeypatch, fetch_error=RuntimeError("timed   out"))
    with pytest.raises(PreviewError) as err:
        build_preview("https://example.com/b")
    assert (err.value.code, err.value.status, err.value.detail) == ("fetch_failed", 502, "timed out")


def test_empty_toc_is_an_error_not_a_build(monkeypatch):
    _patch(monkeypatch, chapters=[])
    with pytest.raises(PreviewError) as err:
        build_preview("https://example.com/b")
    assert err.value.code == "no_chapters"


def test_payload_has_titles_and_positions_but_no_urls_or_html(monkeypatch):
    chapters = [
        Chapter(title="第一章", url="https://example.com/c/1", content="<p>secret</p>"),
        Chapter(title="第二章", url="https://example.com/c/2"),
    ]
    _patch(monkeypatch, chapters=chapters, info=NovelInfo(title="书", author="作者"))
    item = build_preview("https://example.com/b")
    payload = item.to_payload()
    assert payload["chapter_count"] == 2
    assert payload["chapters"] == [
        {"index": 1, "title": "第一章", "title_en": ""},
        {"index": 2, "title": "第二章", "title_en": ""},
    ]
    assert payload["title_en"] == "" and payload["author_en"] == "" and payload["has_cover"] is False
    text = str(payload)
    assert "example.com/c/" not in text and "secret" not in text
    assert item.info.source_url == "https://example.com/b"


def test_has_cover_flag_follows_info(monkeypatch):
    info = NovelInfo(title="书", author="作者", cover_url="https://example.com/cover.jpg")
    _patch(monkeypatch, chapters=[Chapter(title="c", url="https://example.com/c")], info=info)
    item = build_preview("https://example.com/b")
    assert item.has_cover is True
    assert item.to_payload()["has_cover"] is True


# ---------- translate_preview ----------

class _FakeGoogleTranslator:
    """Appends ' (EN)' to each text, in order — enough to prove wiring."""

    def __init__(self, *, max_workers=50, persistent_cache=None):
        self.max_workers = max_workers
        self.cache = persistent_cache

    def translate_texts(self, texts):
        return [t + " (EN)" for t in texts]


def _fake_translator_module(monkeypatch):
    import core.translator as translator_mod

    monkeypatch.setattr(translator_mod, "GoogleTranslator", _FakeGoogleTranslator)


def test_translate_preview_fills_title_author_and_chinese_chapter_titles(monkeypatch):
    _fake_translator_module(monkeypatch)
    info = NovelInfo(title="青云剑录", author="示例作者")
    chapters = [
        Chapter(title="第一章", url="https://example.com/c/1"),
        Chapter(title="Prologue", url="https://example.com/c/2"),  # already English
        Chapter(title="第三章", url="https://example.com/c/3"),
    ]
    item = preview_mod.Preview(id="p", url="https://example.com/b", parser=None, info=info, chapters=chapters)
    translate_preview(item, cache=None)
    assert item.title_en == "青云剑录 (EN)"
    assert item.author_en == "示例作者 (EN)"
    assert item.chapter_titles_en == ["第一章 (EN)", "", "第三章 (EN)"]
    # Originals are never touched — the real pipeline must still see the source text.
    assert item.info.title == "青云剑录" and chapters[1].title == "Prologue"

    payload = item.to_payload()
    assert payload["chapters"][1]["title_en"] == ""
    assert payload["chapters"][2]["title_en"] == "第三章 (EN)"


def test_translate_preview_is_a_noop_for_english_books(monkeypatch):
    _fake_translator_module(monkeypatch)
    info = NovelInfo(title="Already English", author="Someone")
    chapters = [Chapter(title="Chapter One", url="https://example.com/c/1")]
    item = preview_mod.Preview(id="p", url="https://example.com/b", parser=None, info=info, chapters=chapters)
    translate_preview(item, cache=None)
    assert item.title_en == "" and item.author_en == "" and item.chapter_titles_en == []


def test_translate_preview_failure_never_raises_and_leaves_display_blank(monkeypatch):
    import core.translator as translator_mod

    class _Boom:
        def __init__(self, **kw):
            raise RuntimeError("no network")

    monkeypatch.setattr(translator_mod, "GoogleTranslator", _Boom)
    info = NovelInfo(title="青云剑录", author="示例作者")
    chapters = [Chapter(title="第一章", url="https://example.com/c/1")]
    item = preview_mod.Preview(id="p", url="https://example.com/b", parser=None, info=info, chapters=chapters)
    translate_preview(item, cache=None)  # must not raise
    assert item.title_en == "" and item.chapter_titles_en == []
    assert item.info.title == "青云剑录"  # original still intact for the real pipeline


def test_store_expires_old_previews(monkeypatch):
    _patch(monkeypatch, chapters=[Chapter(title="c", url="https://example.com/c")])
    store = PreviewStore(ttl=0.05)
    item = build_preview("https://example.com/b")
    store.put(item)
    assert store.get(item.id) is item
    time.sleep(0.08)
    assert store.get(item.id) is None


def test_store_keeps_only_the_newest_few(monkeypatch):
    _patch(monkeypatch, chapters=[Chapter(title="c", url="https://example.com/c")])
    store = PreviewStore(limit=2)
    items = [build_preview("https://example.com/b") for _ in range(3)]
    for item in items:
        store.put(item)
        time.sleep(0.002)
    assert store.get(items[0].id) is None
    assert store.get(items[2].id) is items[2]
