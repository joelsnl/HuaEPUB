"""HuaEPUB Simple: jobs, pipeline rules, and HTTP routes. Offline, no PySide6."""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from core.cache import NovelCache
from core.download_runner import DownloadCancelled, DownloadControl, EpubBuildResult
from core.parser import Chapter, NovelInfo
from web import pipeline as pipeline_mod
from web.jobs import JobBusy, JobManager
from web.pipeline import PipelineResult, run_pipeline
from web.preview import Preview, PreviewStore
from web.server import create_app


class _NoNetworkParser:
    request_delay = 0

    def get_chapter_content(self, chapter):
        raise AssertionError(f"must not hit the network: {chapter.url}")


def _preview(count=5, title="A Test Novel") -> Preview:
    chapters = [Chapter(title=f"Chapter {i}", url=f"https://example.com/c/{i}") for i in range(1, count + 1)]
    info = NovelInfo(title=title, author="Tester", source_url="https://example.com/book/1")
    return Preview(id="p1", url=info.source_url, parser=_NoNetworkParser(), info=info, chapters=chapters)


def _fake_runner(*, control, output_path, chapters, report, warnings=False, wait=None, **_kw):
    report("fetching", 0.5, "Fetching")
    if wait:
        wait(control)
    report("translating", 0.5, "Translating")
    Path(output_path).write_bytes(b"PK\x03\x04 fake epub")
    return PipelineResult(
        path=output_path,
        notes="1 chapter still has significant Chinese." if warnings else "",
        has_warnings=warnings,
        flagged=[1] if warnings else [],
    )


@pytest.fixture
def cache(tmp_path):
    c = NovelCache(tmp_path / "cache.db", max_bytes=0)
    yield c
    c.close()


def _manager(tmp_path, cache, runner=_fake_runner):
    return JobManager(tmp_path / "staging", cache, runner=runner)


# ---------- jobs ----------

def test_job_completes_and_stages_one_epub(tmp_path, cache):
    mgr = _manager(tmp_path, cache)
    job = mgr.start(_preview(), 1, 5)
    mgr.wait(job.id)
    snap = job.snapshot()
    assert snap["state"] == "done"
    assert snap["phase"] == "Saved"
    assert snap["filename"] == "A Test Novel.epub"
    assert mgr.epub_for(job.id) is job
    assert job.path.parent.parent == (tmp_path / "staging")


def test_partial_range_shows_in_file_name(tmp_path, cache):
    mgr = _manager(tmp_path, cache)
    job = mgr.start(_preview(), 2, 4)
    mgr.wait(job.id)
    assert job.snapshot()["filename"] == "A Test Novel (2-4).epub"
    assert job.total == 3


def test_warnings_title_is_never_a_plain_success(tmp_path, cache):
    mgr = _manager(tmp_path, cache, runner=lambda **kw: _fake_runner(warnings=True, **kw))
    job = mgr.start(_preview(), 1, 5)
    mgr.wait(job.id)
    snap = job.snapshot()
    assert snap["phase"] == "Saved with warnings"
    assert snap["warnings"] is True and snap["flagged"] == [1]


def test_second_job_while_running_is_refused(tmp_path, cache):
    def slow(*, control, **kw):
        while not control.cancel_requested:
            time.sleep(0.01)
        raise DownloadCancelled()

    mgr = _manager(tmp_path, cache, runner=slow)
    job = mgr.start(_preview(), 1, 5)
    with pytest.raises(JobBusy) as busy:
        mgr.start(_preview(), 1, 5)
    assert busy.value.job_id == job.id
    mgr.cancel(job.id)
    mgr.wait(job.id)


def test_cancel_leaves_no_epub(tmp_path, cache):
    def slow(*, control, output_path, **kw):
        Path(output_path).with_name("book.epub.tmp").write_bytes(b"half")
        while not control.cancel_requested:
            time.sleep(0.01)
        raise DownloadCancelled()

    mgr = _manager(tmp_path, cache, runner=slow)
    job = mgr.start(_preview(), 1, 5)
    mgr.cancel(job.id)
    mgr.wait(job.id)
    assert job.snapshot()["state"] == "cancelled"
    assert mgr.epub_for(job.id) is None
    assert list((tmp_path / "staging").iterdir()) == []


def test_runner_failure_becomes_a_short_error(tmp_path, cache):
    def broken(**kw):
        raise RuntimeError("Google   said\nno")

    mgr = _manager(tmp_path, cache, runner=broken)
    job = mgr.start(_preview(), 1, 5)
    mgr.wait(job.id)
    snap = job.snapshot()
    assert snap["state"] == "error" and snap["error"] == "Google said no"


def test_new_job_and_startup_remove_old_staged_files(tmp_path, cache):
    mgr = _manager(tmp_path, cache)
    first = mgr.start(_preview(), 1, 5)
    mgr.wait(first.id)
    old_dir = first.directory
    second = mgr.start(_preview(), 1, 5)
    mgr.wait(second.id)
    assert not old_dir.exists() and mgr.get(first.id) is None

    leftover = tmp_path / "staging" / "crash"
    leftover.mkdir()
    (leftover / "book.epub").write_bytes(b"x")
    JobManager(tmp_path / "staging", cache)
    assert not leftover.exists()


def test_clear_deletes_the_staged_file(tmp_path, cache):
    mgr = _manager(tmp_path, cache)
    job = mgr.start(_preview(), 1, 5)
    mgr.wait(job.id)
    assert mgr.clear(job.id) is True
    assert not job.directory.exists()


def test_job_works_on_copies_of_the_preview_chapters(tmp_path, cache):
    seen = []

    def runner(*, chapters, output_path, **kw):
        chapters[0].content = "<p>filled</p>"
        seen.append(chapters)
        Path(output_path).write_bytes(b"PK")
        return PipelineResult(path=output_path)

    mgr = _manager(tmp_path, cache, runner=runner)
    prev = _preview()
    mgr.wait(mgr.start(prev, 1, 5).id)
    assert prev.chapters[0].content == ""


# ---------- pipeline rules ----------

def test_pipeline_uses_the_runner_without_polish_library_or_resume(monkeypatch, tmp_path, cache):
    calls = {}

    def fake_engines(**kw):
        calls["engines"] = kw
        return None, None

    def fake_download(**kw):
        calls["download"] = kw
        return []

    def fake_build(**kw):
        calls["build"] = kw
        Path(kw["output_path"]).write_bytes(b"PK")
        return EpubBuildResult(output_path=kw["output_path"])

    monkeypatch.setattr(pipeline_mod, "engines_for_chapter_fetch", fake_engines)
    monkeypatch.setattr(pipeline_mod, "download_chapters_with_cache", fake_download)
    monkeypatch.setattr(pipeline_mod, "build_epub", fake_build)
    monkeypatch.setenv("HOME", str(tmp_path))
    prev = _preview()
    control = DownloadControl()
    out = tmp_path / "book.epub"
    result = run_pipeline(
        control=control, cache=cache, parser=prev.parser, info=prev.info,
        chapters=prev.chapters, output_path=str(out), report=lambda *a: None, workers=999,
    )
    assert calls["build"]["ollama_polish"] is False
    assert calls["build"]["glossary_mode"] == "auto" and calls["build"]["clean"] is True
    assert calls["build"]["translate"] is True and calls["build"]["backend"] == "google"
    assert calls["build"]["workers"] == 200  # capped
    assert control.data_dir is None and control.active_job is None
    assert not (tmp_path / "library.json").exists()
    assert not (tmp_path / ".huaepub" / "library.json").exists()
    assert result.has_warnings is False


def test_pipeline_flags_chapters_that_need_a_look(monkeypatch, tmp_path, cache):
    monkeypatch.setattr(pipeline_mod, "engines_for_chapter_fetch", lambda **kw: (None, None))
    monkeypatch.setattr(pipeline_mod, "download_chapters_with_cache", lambda **kw: ["Chapter 2"])

    def fake_build(**kw):
        Path(kw["output_path"]).write_bytes(b"PK")
        return EpubBuildResult(
            output_path=kw["output_path"],
            translation_warnings=[("Chapter 4", 90)],
            heuristic_chapters=["Chapter 5"],
        )

    monkeypatch.setattr(pipeline_mod, "build_epub", fake_build)
    prev = _preview()
    result = run_pipeline(
        control=DownloadControl(), cache=cache, parser=prev.parser, info=prev.info,
        chapters=prev.chapters, output_path=str(tmp_path / "b.epub"), report=lambda *a: None,
    )
    assert result.has_warnings is True
    assert result.flagged == [1, 3, 4]
    assert "significant Chinese" in result.notes


def test_real_offline_pipeline_writes_an_epub_from_cache(tmp_path, cache):
    prev = _preview(count=2)
    for ch in prev.chapters:
        cache.put_chapter(prev.info.source_url, ch.url, ch.title, f"<p>Body of {ch.title} with words.</p>")
    steps = []
    out = tmp_path / "out" / "book.epub"
    out.parent.mkdir()
    result = run_pipeline(
        control=DownloadControl(), cache=cache, parser=prev.parser, info=prev.info,
        chapters=prev.chapters, output_path=str(out),
        report=lambda state, frac, msg: steps.append(state), translate=False, workers=1,
    )
    assert out.is_file() and result.has_warnings is False
    assert "fetching" in steps and steps[-1] == "writing"
    with zipfile.ZipFile(out) as zf:
        assert "mimetype" in zf.namelist()


# ---------- HTTP ----------

@pytest.fixture
def client(tmp_path, cache):
    mgr = _manager(tmp_path, cache)
    prev = _preview()
    store = PreviewStore()
    app = create_app(
        manager=mgr, previews=store, version="9.9.9", preview_builder=lambda url: (store.put(prev), prev)[1]
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.mgr = mgr
        yield c


def _wait_done(client, job_id):
    client.mgr.wait(job_id)
    return client.get(f"/api/jobs/{job_id}").json()


def test_health_and_page(client):
    assert client.get("/api/health").json() == {"name": "HuaEPUB Simple", "version": "9.9.9"}
    page = client.get("/")
    assert page.status_code == 200 and "HuaEPUB" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200


def test_preview_route_calls_the_translator_with_the_cache(tmp_path, cache):
    calls = []
    prev = _preview()
    store = PreviewStore()
    app = create_app(
        manager=_manager(tmp_path, cache), previews=store, cache=cache,
        preview_builder=lambda url: (store.put(prev), prev)[1],
        preview_translator=lambda item, **kw: calls.append((item.id, kw.get("cache"))),
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.post("/api/preview", json={"url": "https://example.com/book/1"})
    assert calls == [("p1", cache)]


def test_cover_route(tmp_path, cache):
    prev = _preview()
    prev.info.cover_url = "https://example.com/cover.jpg"
    prev.has_cover = True
    store = PreviewStore()
    store.put(prev)

    def fake_fetch(session, url):
        assert url == "https://example.com/cover.jpg"
        return b"\x89PNG\r\n\x1a\n" + b"x" * 20

    app = create_app(manager=_manager(tmp_path, cache), previews=store, version="1")
    import web.server as server_mod

    with monkeypatch_module(server_mod, "fetch_cover_bytes", fake_fetch):
        with TestClient(app, base_url="http://127.0.0.1:8765") as c:
            ok = c.get("/api/preview/p1/cover")
            assert ok.status_code == 200 and ok.headers["content-type"] == "image/png"
            assert c.get("/api/preview/missing/cover").status_code == 404

    no_cover = create_app(manager=_manager(tmp_path, cache), previews=PreviewStore())
    with TestClient(no_cover, base_url="http://127.0.0.1:8765") as c:
        c.post("/api/preview", json={"url": "https://example.com/book/1"})
        assert c.get("/api/preview/missing/cover").status_code == 404


class monkeypatch_module:
    """Tiny context manager: patch one attribute on a module, restore on exit."""

    def __init__(self, module, name, value):
        self.module, self.name, self.value = module, name, value

    def __enter__(self):
        self._old = getattr(self.module, self.name)
        setattr(self.module, self.name, self.value)

    def __exit__(self, *exc):
        setattr(self.module, self.name, self._old)


def test_full_flow_preview_build_download_delete(client):
    pv = client.post("/api/preview", json={"url": "https://example.com/book/1"})
    assert pv.status_code == 200
    body = pv.json()
    assert body["chapter_count"] == 5 and "url" not in str(body["chapters"])

    started = client.post(
        "/api/jobs", json={"preview_id": body["preview_id"], "chapter_from": 1, "chapter_to": 5}
    )
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    snap = _wait_done(client, job_id)
    assert snap["state"] == "done" and snap["filename"] == "A Test Novel.epub"

    epub = client.get(f"/api/jobs/{job_id}/epub")
    assert epub.status_code == 200
    assert epub.headers["content-type"] == "application/epub+zip"
    assert "attachment" in epub.headers["content-disposition"]
    assert epub.content.startswith(b"PK")

    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert client.get(f"/api/jobs/{job_id}/epub").status_code == 404
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_events_stream_ends_with_the_terminal_state(client):
    client.post("/api/preview", json={"url": "https://example.com/book/1"})
    job_id = client.post(
        "/api/jobs", json={"preview_id": "p1", "chapter_from": 1, "chapter_to": 5}
    ).json()["job_id"]
    with client.stream("GET", f"/api/jobs/{job_id}/events") as resp:
        text = "".join(resp.iter_text())
    assert resp.status_code == 200
    assert '"state": "done"' in text.strip().splitlines()[-1] or '"state": "done"' in text


def test_job_validation_errors(client):
    client.post("/api/preview", json={"url": "https://example.com/book/1"})
    assert client.post("/api/jobs", json={"preview_id": "nope", "chapter_from": 1, "chapter_to": 2}).status_code == 404
    for lo, hi in ((0, 3), (3, 2), (1, 6)):
        r = client.post("/api/jobs", json={"preview_id": "p1", "chapter_from": lo, "chapter_to": hi})
        assert r.status_code == 400 and r.json()["error"] == "bad_range"
    assert client.get("/api/jobs/missing").status_code == 404
    assert client.post("/api/jobs/missing/cancel").status_code == 404


def test_overlap_gives_409_and_cancel_works(tmp_path, cache):
    def slow(*, control, **kw):
        while not control.cancel_requested:
            time.sleep(0.01)
        raise DownloadCancelled()

    mgr = _manager(tmp_path, cache, runner=slow)
    prev = _preview()
    store = PreviewStore()
    store.put(prev)
    app = create_app(manager=mgr, previews=store, version="1")
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        first = c.post("/api/jobs", json={"preview_id": "p1", "chapter_from": 1, "chapter_to": 5})
        job_id = first.json()["job_id"]
        again = c.post("/api/jobs", json={"preview_id": "p1", "chapter_from": 1, "chapter_to": 5})
        assert again.status_code == 409 and again.json()["job_id"] == job_id
        assert c.post("/api/preview", json={"url": "https://example.com/x"}).status_code == 409
        assert c.delete(f"/api/jobs/{job_id}").status_code == 409
        assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 202
        mgr.wait(job_id)
        assert c.get(f"/api/jobs/{job_id}").json()["state"] == "cancelled"
        assert c.get(f"/api/jobs/{job_id}/epub").status_code == 404
        assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_preview_errors_are_reported(tmp_path, cache):
    from web.preview import PreviewError

    def failing(url):
        raise PreviewError("fetch_failed", 502, "timed out")

    app = create_app(manager=_manager(tmp_path, cache), preview_builder=failing)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        r = c.post("/api/preview", json={"url": "https://example.com/x"})
    assert r.status_code == 502 and r.json() == {"error": "fetch_failed", "detail": "timed out"}


def test_foreign_host_and_cross_site_posts_are_refused(client):
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 421
    assert client.get("/api/health", headers={"host": "192.168.1.5:8765"}).status_code == 421
    bad = client.post(
        "/api/preview", json={"url": "https://example.com/x"}, headers={"origin": "https://evil.example"}
    )
    assert bad.status_code == 403
    ok = client.post(
        "/api/preview", json={"url": "https://example.com/x"}, headers={"origin": "http://127.0.0.1:8765"}
    )
    assert ok.status_code == 200


def test_lan_mode_needs_the_access_code(tmp_path, cache):
    app = create_app(manager=_manager(tmp_path, cache), version="1", lan_code="abcd2345")
    with TestClient(app, base_url="http://192.168.1.20:8765", follow_redirects=False) as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/").status_code == 401
        assert c.get("/?code=wrong").status_code == 401
        entered = c.get("/?code=abcd2345")
        assert entered.status_code == 303 and "huaepub_simple_code" in entered.headers["set-cookie"]
        assert "httponly" in entered.headers["set-cookie"].lower()
        assert c.get("/api/health").status_code == 200
    with TestClient(app, base_url="http://8.8.8.8:8765") as c:
        assert c.get("/api/health").status_code == 421


def test_localhost_mode_rejects_lan_hosts(tmp_path, cache):
    app = create_app(manager=_manager(tmp_path, cache), version="1")
    with TestClient(app, base_url="http://192.168.1.20:8765") as c:
        assert c.get("/api/health").status_code == 421
