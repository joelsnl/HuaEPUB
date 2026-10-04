"""HuaEPUB Simple: previews and the package boundary. Offline, no PySide6."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from core.parser import Chapter, NovelInfo
from core.security import UnsafeURLError
from web import preview as preview_mod
from web.preview import PreviewError, PreviewStore, build_preview

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
        {"index": 1, "title": "第一章"},
        {"index": 2, "title": "第二章"},
    ]
    text = str(payload)
    assert "example.com/c/" not in text and "secret" not in text
    assert item.info.source_url == "https://example.com/b"


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


def test_web_never_loads_qt_and_core_never_loads_web():
    code = (
        "import sys, web.server, web.jobs, web.pipeline, web.preview\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in ('PySide6', 'gui')]\n"
        "assert not bad, bad\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr

    for folder in ("core", "gui", "parsers"):
        for path in (ROOT / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "import web" not in text and "from web" not in text, path


def test_desktop_build_and_packaging_do_not_know_about_web():
    assert "web" not in (ROOT / "build.py").read_text(encoding="utf-8").lower().replace("website", "")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'include = ["core*", "gui*", "parsers*"]' in pyproject
    assert "fastapi" not in (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
