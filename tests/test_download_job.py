"""Tests for local incomplete-download job persistence (not Drive-synced)."""

from core.download_job import (
    JOB_VERSION,
    chapters_from_job,
    chapters_to_job,
    clear_job,
    job_chapter_urls,
    job_display_title,
    load_job,
    novel_info_from_job,
    novel_info_to_job,
    save_job,
)
from core.parser import Chapter, NovelInfo


def test_save_load_clear_roundtrip(tmp_path):
    job = {
        "kind": "single",
        "status": "paused",
        "source_url": "https://example.com/book/1",
        "title": "测试",
        "translated_title": "Test",
        "chapters": [{"url": "https://example.com/1", "title": "Ch1", "index": 0}],
        "options": {"translate": True},
    }
    save_job(job, tmp_path)
    loaded = load_job(tmp_path)
    assert loaded is not None
    assert loaded["version"] == JOB_VERSION
    assert loaded["kind"] == "single"
    assert loaded["title"] == "测试"
    assert loaded["chapters"][0]["url"] == "https://example.com/1"
    clear_job(tmp_path)
    assert load_job(tmp_path) is None


def test_load_missing_returns_none(tmp_path):
    assert load_job(tmp_path) is None


def test_reject_bad_version(tmp_path):
    save_job({"kind": "single", "chapters": []}, tmp_path)
    # Corrupt version
    path = tmp_path / "active_download.json"
    path.write_text('{"version": 999, "kind": "single"}', encoding="utf-8")
    assert load_job(tmp_path) is None


def test_chapter_and_info_helpers():
    chapters = [
        Chapter(title="一", url="https://x/1", index=0),
        Chapter(title="二", url="https://x/2", index=1),
    ]
    blob = chapters_to_job(chapters)
    restored = chapters_from_job(blob)
    assert len(restored) == 2
    assert restored[0].url == "https://x/1"
    assert restored[1].title == "二"

    info = NovelInfo(title="书", author="作者", source_url="https://x/book", cover_url="https://x/c.jpg")
    info2 = novel_info_from_job(novel_info_to_job(info))
    assert info2.title == "书"
    assert info2.source_url == "https://x/book"
    assert info2.cover_url == "https://x/c.jpg"


def test_job_display_and_urls():
    single = {
        "kind": "single",
        "translated_title": "Hello",
        "chapters": [{"url": "https://a/1"}, {"url": "https://a/2"}],
    }
    assert job_display_title(single) == "Hello"
    assert job_chapter_urls(single) == ["https://a/1", "https://a/2"]

    multi = {
        "kind": "multi",
        "novels": [
            {"title": "Done", "done": True, "chapters": [{"url": "https://d/1"}]},
            {"title": "Next", "done": False, "chapters": [{"url": "https://n/1"}]},
        ],
    }
    assert "Next" in job_display_title(multi)
    assert job_chapter_urls(multi) == ["https://n/1"]
