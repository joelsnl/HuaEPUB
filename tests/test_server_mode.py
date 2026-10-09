"""Server mode: sign-in, TLS, settings, the task slot and the HTTP routes (offline)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from fastapi.testclient import TestClient  # noqa: E402

from core.parser import Chapter, NovelInfo  # noqa: E402
from web.auth import (  # noqa: E402
    COOKIE_NAME, LoginLimiter, OpenLinks, PasswordTooShort, ServerSecrets, normalize_code,
)
from web.preview import Preview  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ("127.0.0.1", 50000)
LAN = ("192.168.1.20", 50000)
H = {"X-HuaEPUB": "1"}


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def session(home):
    from core.session import AppSession

    s = AppSession()
    yield s
    try:
        s.cache.close()
    except Exception:
        pass


class _Parser:
    request_delay = 0

    def __init__(self, html="<p>Hello there.</p>"):
        self.html = html

    def get_chapter_content(self, chapter):
        return self.html


def _preview(count=5, url="https://example.com/book/1", title="A Test Novel") -> Preview:
    chapters = [Chapter(title=f"Chapter {i}", url=f"{url}/c/{i}") for i in range(1, count + 1)]
    info = NovelInfo(title=title, author="Tester", source_url=url)
    return Preview(id="p1", url=url, parser=_Parser(), info=info, chapters=chapters)


def _ctx(session, *, mode="lan", https=False, hsts=False, builder=None):
    from web.context import ServerContext, book_roots
    from web.tasks import TaskManager

    manager = TaskManager(session, file_roots=lambda: book_roots(session))
    secrets = ServerSecrets(session.data_dir / "server" / "secret.json")
    ctx = ServerContext(session=session, tasks=manager, secrets=secrets, mode=mode,
                        version="9.9.9", https=https, hsts=hsts,
                        preview_builder=builder or (lambda url: _preview(url=url)),
                        preview_translator=lambda item, cache: None)
    return ctx


def _client(ctx, client=LOCAL, base="http://127.0.0.1:8765"):
    from web.server import create_app

    return TestClient(create_app(ctx), base_url=base, client=client, follow_redirects=False)


def _signed_in(ctx, client=LOCAL, base="http://127.0.0.1:8765"):
    c = _client(ctx, client=client, base=base)
    c.cookies.set(COOKIE_NAME, ctx.secrets.make_cookie(ctx.mode))
    return c


def _wait_done(ctx, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = ctx.tasks.snapshot()
        if snap and snap["state"] in ("done", "error", "cancelled"):
            return snap
        time.sleep(0.02)
    raise AssertionError(f"task did not finish: {ctx.tasks.snapshot()}")


# ----------------------------------------------------------------------
# Sign-in secrets
# ----------------------------------------------------------------------


def test_secrets_file_is_created_once_and_kept(tmp_path):
    path = tmp_path / "server" / "secret.json"
    a = ServerSecrets(path)
    b = ServerSecrets(path)
    assert a.lan_code == b.lan_code and len(a.lan_code) == 8
    assert a.key == b.key and len(a.key) == 32
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_access_code_ignores_case_spaces_and_dashes(tmp_path):
    s = ServerSecrets(tmp_path / "s.json")
    code = s.lan_code
    assert s.check_code(code.upper())
    assert s.check_code(f"{code[:4]} - {code[4:]}")
    assert not s.check_code("")
    assert not s.check_code(code[:-1])
    assert normalize_code(" Ab-c D ") == "abcd"


def test_cookie_round_trip_and_mode_binding(tmp_path):
    s = ServerSecrets(tmp_path / "s.json")
    lan = s.make_cookie("lan")
    assert s.cookie_valid(lan, "lan")
    assert not s.cookie_valid(lan, "remote")
    tampered = lan[:-2] + ("AA" if not lan.endswith("AA") else "BB")
    assert not s.cookie_valid(tampered, "lan")
    assert not s.cookie_valid("", "lan")


def test_cookie_expires(tmp_path):
    s = ServerSecrets(tmp_path / "s.json")
    old = s.make_cookie("lan", now=time.time() - 31 * 86400)
    assert not s.cookie_valid(old, "lan")
    remote = s.make_cookie("remote", now=time.time() - 8 * 86400)
    assert not s.cookie_valid(remote, "remote")


def test_new_code_signs_out_lan_cookies(tmp_path):
    s = ServerSecrets(tmp_path / "s.json")
    cookie = s.make_cookie("lan")
    s.rotate_code()
    assert s.lan_gen == 2
    assert not s.cookie_valid(cookie, "lan")
    assert s.cookie_valid(s.make_cookie("lan"), "lan")


def test_password_rules_and_rotation(tmp_path):
    s = ServerSecrets(tmp_path / "s.json")
    assert not s.has_password
    assert not s.check_password("anything at all")
    with pytest.raises(PasswordTooShort):
        s.set_password("short")
    s.set_password("correct horse battery")
    assert s.has_password
    assert s.check_password("correct horse battery")
    assert not s.check_password("correct horse batterY")
    cookie = s.make_cookie("remote")
    s.set_password("another long password")
    assert not s.cookie_valid(cookie, "remote")
    text = (tmp_path / "s.json").read_text(encoding="utf-8")
    assert "correct horse" not in text and "another long" not in text


def test_login_limiter_per_address_and_global():
    now = [1000.0]
    lim = LoginLimiter(clock=lambda: now[0], global_limit=8)
    for _ in range(5):
        assert lim.check("1.1.1.1").allowed
        lim.failed("1.1.1.1")
    verdict = lim.check("1.1.1.1")
    assert not verdict.allowed and 0 < verdict.retry_after <= 15 * 60 + 1
    assert lim.check("2.2.2.2").allowed
    now[0] += 15 * 60 + 1
    assert lim.check("1.1.1.1").allowed
    for i in range(8):
        lim.failed(f"10.0.0.{i}")
    assert not lim.check("3.3.3.3").allowed


def test_login_limiter_success_clears_the_address():
    lim = LoginLimiter(clock=lambda: 0.0)
    for _ in range(4):
        lim.failed("1.1.1.1")
    lim.succeeded("1.1.1.1")
    lim.failed("1.1.1.1")
    assert lim.check("1.1.1.1").allowed


def test_open_links_work_once_and_expire():
    now = [0.0]
    links = OpenLinks(ttl=60, clock=lambda: now[0])
    token = links.issue()
    assert links.redeem(token)
    assert not links.redeem(token)
    late = links.issue()
    now[0] = 61
    assert not links.redeem(late)
    assert not links.redeem("")


# ----------------------------------------------------------------------
# TLS
# ----------------------------------------------------------------------


def test_generated_certificate_is_reused_and_covers_names(tmp_path):
    from web import tls

    a = tls.ensure_certificate(tmp_path, ["192.168.1.20", "example.duckdns.org"])
    assert a.generated and a.cert_path.is_file() and a.key_path.is_file()
    assert len(a.fingerprint.split(":")) == 32
    b = tls.ensure_certificate(tmp_path, ["192.168.1.20"])
    assert b.fingerprint == a.fingerprint
    c = tls.ensure_certificate(tmp_path, ["192.168.1.30"])
    assert c.fingerprint != a.fingerprint
    if os.name == "posix":
        assert (c.key_path.stat().st_mode & 0o777) == 0o600


def test_own_certificate_must_match_its_key(tmp_path):
    from web import tls

    one = tls.generate(tmp_path / "one", ["localhost"])
    two = tls.generate(tmp_path / "two", ["localhost"])
    own = tls.load_own_certificate(str(one.cert_path), str(one.key_path))
    assert not own.generated and own.fingerprint == one.fingerprint
    with pytest.raises(tls.CertificateError):
        tls.load_own_certificate(str(one.cert_path), str(two.key_path))
    with pytest.raises(tls.CertificateError):
        tls.load_own_certificate(str(tmp_path / "missing.pem"), str(one.key_path))


# ----------------------------------------------------------------------
# Settings from the browser
# ----------------------------------------------------------------------


def test_apply_settings_validates_every_key(home):
    from web.options import SettingsError, apply_settings

    settings = {"translate": True}
    apply_settings(settings, {"translate": False, "workers": 50, "glossary": "off",
                              "reader_font_pt": 20})
    assert settings["translate"] is False and settings["workers"] == 50
    assert settings["translation_glossary"] == "off" and settings["reader_font_pt"] == 20
    for bad in ({"workers": 0}, {"workers": "5"}, {"translate": "yes"}, {"glossary": "x"},
                {"reader_font_pt": 99}, {"backend": "nope"}):
        with pytest.raises(SettingsError):
            apply_settings(settings, bad)


def test_browser_can_set_an_existing_books_folder(home):
    from web.options import SettingsError, apply_settings

    books = home / "books"
    books.mkdir()
    settings = {}
    apply_settings(settings, {"output_dir": str(books)})
    assert settings["output_dir"] == str(books)
    apply_settings(settings, {"output_dir": "  "})
    assert settings["output_dir"] == ""
    apply_settings(settings, {"reader_theme": "sepia", "reader_mode": "scroll"})
    assert settings["reader_theme"] == "sepia" and settings["reader_mode"] == "scroll"
    with pytest.raises(SettingsError):
        apply_settings(settings, {"reader_theme": "neon"})
    for bad in ("books", str(home / "missing")):
        with pytest.raises(SettingsError):
            apply_settings(settings, {"output_dir": bad})


def test_settings_books_folder_is_used_immediately(session, home):
    books = home / "shelf"
    books.mkdir()
    ctx = _ctx(session)
    c = _signed_in(ctx)
    r = c.put("/api/settings", json={"output_dir": str(books)}, headers=H)
    assert r.status_code == 200
    assert session.output_dir == str(books)
    assert r.json()["output_dir"] == str(books)


def test_install_route_and_service_route(session, monkeypatch):
    from web import host

    ctx = _ctx(session)
    c = _signed_in(ctx)
    assert c.post("/api/install", json={"what": "nope"}, headers=H).status_code == 400
    monkeypatch.setattr(host, "install_user_service", lambda **_k: (0, "already installed:\n/unit"))
    ok = c.post("/api/service", headers=H)
    assert ok.status_code == 200
    assert "already installed" in ok.json()["message"]
    monkeypatch.setattr(host, "install_user_service", lambda **_k: (1, "Linux only"))
    bad = c.post("/api/service", headers=H)
    assert bad.status_code == 400 and bad.json()["detail"] == "Linux only"
    done = threading.Event()

    def body(_ctx):
        assert session.control.is_downloading is False
        done.set()
        return {"notes": "ok"}

    ctx.tasks.start("install", "Download Polish", body)
    assert done.wait(2)
    ctx.tasks.wait(2)
    assert session.control.is_downloading is False


class _Ok:
    returncode = 0
    stderr = ""
    stdout = ""


def test_install_service_leaves_an_existing_unit(tmp_path, monkeypatch):
    from web import host

    unit = tmp_path / "noveldownloader.service"
    unit.write_text("existing\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(host.sys, "platform", "linux")
    code, message = host.install_user_service(
        roots=[tmp_path], unit_dir=tmp_path / "new",
        runner=lambda cmd: calls.append(cmd) or _Ok(),
    )
    assert code == 0
    assert "already installed" in message
    assert calls == []
    assert unit.read_text(encoding="utf-8") == "existing\n"
    assert not (tmp_path / "new").exists()

    other = tmp_path / "other-units"
    other.mkdir()
    custom = other / "pi-books.service"
    custom.write_text("ExecStart=/usr/bin/python3 /home/joelsnl/HuaEPUB/app.py --headless\n",
                      encoding="utf-8")
    code, message = host.install_user_service(
        roots=[other], unit_dir=tmp_path / "new", runner=lambda cmd: calls.append(cmd) or _Ok(),
    )
    assert code == 0 and "already installed" in message and calls == []
    assert custom.read_text(encoding="utf-8").startswith("ExecStart=")


def test_install_service_writes_a_unit_when_none_exists(tmp_path, monkeypatch):
    from web import host

    monkeypatch.setattr(host.sys, "platform", "linux")
    dest = tmp_path / "user"
    code, _message = host.install_user_service(
        roots=[tmp_path / "empty"], unit_dir=dest, runner=lambda cmd: _Ok(),
    )
    assert code == 0
    text = (dest / "huaepub.service").read_text(encoding="utf-8")
    assert "HUAEPUB_SERVICE=1" in text and "--headless" in text
    again, message = host.install_user_service(
        roots=[dest], unit_dir=tmp_path / "other", runner=lambda cmd: (_ for _ in ()).throw(AssertionError(cmd)),
    )
    assert again == 0 and "already installed" in message
    assert text == (dest / "huaepub.service").read_text(encoding="utf-8")


def test_install_service_is_linux_only(tmp_path, monkeypatch):
    from web import host

    monkeypatch.setattr(host.sys, "platform", "win32")
    code, message = host.install_user_service(
        roots=[tmp_path], unit_dir=tmp_path / "user", runner=lambda cmd: _Ok(),
    )
    assert code == 1 and "Linux" in message
    assert not (tmp_path / "user").exists()


def test_job_options_never_start_downloads(home, monkeypatch):
    from web import options

    monkeypatch.setattr(options, "polish_ready", lambda: False)
    monkeypatch.setattr(options, "nmt_ready", lambda: False)
    opts = options.job_options({"translation_backend": "ctranslate2", "ollama_polish": True,
                                "workers": 9999})
    assert opts["backend"] == "google"
    assert opts["ollama_polish"] is False
    assert opts["workers"] == options.MAX_WORKERS


# ----------------------------------------------------------------------
# The task slot
# ----------------------------------------------------------------------


def test_one_task_at_a_time_and_exclusive(session):
    from web.tasks import Busy, TaskManager

    manager = TaskManager(session, file_roots=lambda: [])
    gate = threading.Event()

    def body(ctx):
        gate.wait(5)
        return {"ok": True}

    task = manager.start("single", "First", body)
    with pytest.raises(Busy):
        manager.start("single", "Second", body)
    with pytest.raises(Busy):
        with manager.exclusive():
            pass
    gate.set()
    manager.wait(5)
    assert task.snapshot()["state"] == "done"
    with manager.exclusive():
        with pytest.raises(Busy):
            manager.start("single", "During read", body)


def test_task_errors_are_short_and_cancel_clears_the_resume_point(session):
    from core.download_job import load_job, save_job
    from web.tasks import TaskManager

    manager = TaskManager(session, file_roots=lambda: [])

    def boom(ctx):
        raise RuntimeError("x" * 1000)

    t = manager.start("single", "Boom", boom)
    manager.wait(5)
    snap = t.snapshot()
    assert snap["state"] == "error" and len(snap["error"]) <= 300

    gate = threading.Event()

    def slow(ctx):
        save_job({"kind": "single", "title": "T", "chapters": []}, session.data_dir)
        gate.wait(5)
        return {"cancelled": ctx.control.cancel_requested}

    t2 = manager.start("single", "Slow", slow)
    time.sleep(0.1)
    assert manager.cancel()
    gate.set()
    manager.wait(5)
    assert t2.snapshot()["state"] == "cancelled"
    assert load_job(session.data_dir) is None


def test_files_are_only_served_from_the_books_folder(session, tmp_path):
    from web.context import book_roots
    from web.tasks import TaskManager

    manager = TaskManager(session, file_roots=lambda: book_roots(session))
    books = book_roots(session)[0]
    books.mkdir(parents=True, exist_ok=True)
    good = books / "Book.epub"
    good.write_bytes(b"PK")
    outside = tmp_path / "elsewhere.epub"
    outside.write_bytes(b"PK")
    entry = manager.register_file(str(good))
    assert entry and manager.file_for(entry["token"]) == good.resolve()
    assert manager.register_file(str(outside)) is None
    assert manager.file_for("nope") is None


# ----------------------------------------------------------------------
# HTTP: who may connect
# ----------------------------------------------------------------------


def test_lan_mode_refuses_public_addresses_and_foreign_hosts(session):
    ctx = _ctx(session)
    assert _client(ctx, client=("8.8.8.8", 1)).get("/login").status_code == 403
    assert _client(ctx, client=LAN, base="http://evil.example:8765").get("/login").status_code == 421
    assert _client(ctx, client=LAN, base="http://192.168.1.5:8765").get("/login").status_code == 200


def test_signed_out_requests_go_to_sign_in(session):
    c = _client(_ctx(session))
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/library").status_code == 401
    assert c.get("/static/app.js").status_code in (303, 401)
    login = c.get("/login")
    assert login.status_code == 200 and "Content-Security-Policy" in login.headers


def test_pages_revalidate_so_updates_reach_phones(session):
    c = _signed_in(_ctx(session))
    for path in ("/", "/static/app.css", "/static/library.js"):
        assert c.get(path).headers["Cache-Control"] == "no-cache", path
    assert c.get("/api/state").headers["Cache-Control"] == "no-store"
    assert _client(_ctx(session)).get("/login").headers["Cache-Control"] == "no-cache"


def test_library_entries_carry_the_reading_position(session):
    from core.reading import set_position

    url = "https://example.com/book/read"
    session.library_store.upsert_library(url, title="书", translated_title="Read Book",
                                         chapter_count=10)
    set_position(url, chapter_url=url + "/c/4", chapter_index=3, data_dir=session.data_dir)
    entries = _signed_in(_ctx(session)).get("/api/library").json()["entries"]
    assert [e["read_chapter"] for e in entries if e["url"] == url] == [4]


def test_library_entries_say_when_a_book_was_last_read(session):
    from core.reading import set_position

    read, unread = "https://example.com/book/r", "https://example.com/book/u"
    for url in (read, unread):
        session.library_store.upsert_library(url, title="书", translated_title="B", chapter_count=3)
    before = time.time()
    set_position(read, chapter_index=0, data_dir=session.data_dir)
    rows = {e["url"]: e for e in _signed_in(_ctx(session)).get("/api/library").json()["entries"]}
    assert rows[read]["read_at"] >= before and rows[unread]["read_at"] == 0


def test_code_link_and_login_set_a_cookie(session):
    ctx = _ctx(session)
    c = _client(ctx)
    r = c.get(f"/?code={ctx.secrets.lan_code}")
    assert r.status_code == 303 and COOKIE_NAME in r.headers.get("set-cookie", "")
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    c2 = _client(ctx)
    assert c2.post("/api/login", json={"secret": "wrongcode"}, headers=H).status_code == 401
    ok = c2.post("/api/login", json={"secret": ctx.secrets.lan_code}, headers=H)
    assert ok.status_code == 200
    assert c2.get("/api/session").json()["signed_in"] is True


def test_wrong_codes_are_rate_limited(session):
    ctx = _ctx(session)
    c = _client(ctx)
    for _ in range(5):
        assert c.post("/api/login", json={"secret": "nope"}, headers=H).status_code == 401
    r = c.post("/api/login", json={"secret": ctx.secrets.lan_code}, headers=H)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_state_changes_need_the_header_and_same_origin(session):
    c = _signed_in(_ctx(session))
    assert c.post("/api/task/cancel").status_code == 403
    bad = c.post("/api/task/cancel", headers={**H, "Origin": "http://evil.example"})
    assert bad.status_code == 403
    assert c.post("/api/task/cancel", headers=H).status_code == 409  # nothing running


def test_open_link_signs_in_the_host_browser_once(session):
    ctx = _ctx(session)
    token = ctx.open_links.issue()
    c = _client(ctx)
    first = c.get(f"/open/{token}")
    assert first.status_code == 303 and first.headers["location"] == "/"
    again = _client(ctx).get(f"/open/{token}")
    assert again.headers["location"] == "/login"


def test_remote_mode_uses_the_password_and_secure_cookies(session):
    ctx = _ctx(session, mode="remote", https=True, hsts=True)
    ctx.secrets.set_password("a long enough password")
    base = "https://203.0.113.9:8765"
    c = _client(ctx, client=("203.0.113.50", 1), base=base)
    assert c.get(f"/?code={ctx.secrets.lan_code}").headers["location"] == "/login"
    assert c.post("/api/login", json={"secret": ctx.secrets.lan_code}, headers=H).status_code == 401
    ok = c.post("/api/login", json={"secret": "a long enough password"}, headers=H)
    assert ok.status_code == 200
    assert "secure" in ok.headers["set-cookie"].lower()
    assert ok.headers.get("strict-transport-security")
    lan_cookie = ctx.secrets.make_cookie("lan")
    other = _client(ctx, client=("203.0.113.51", 1), base=base)
    other.cookies.set(COOKIE_NAME, lan_cookie)
    assert other.get("/api/state").status_code == 401


def test_remote_mode_refuses_plain_http():
    from web.context import ServerContext

    with pytest.raises(ValueError):
        ServerContext(session=None, tasks=None, secrets=None, mode="remote", https=False)


# ----------------------------------------------------------------------
# HTTP: the app
# ----------------------------------------------------------------------


def test_settings_round_trip_and_busy_lock(session):
    ctx = _ctx(session)
    c = _signed_in(ctx)
    data = c.get("/api/settings").json()
    assert "translate" in data
    r = c.put("/api/settings", json={"translate": False}, headers=H)
    assert r.status_code == 200
    assert session.settings["translate"] is False
    assert c.put("/api/settings", json={"output_dir": "/x"}, headers=H).status_code == 400

    gate = threading.Event()
    ctx.tasks.start("single", "Busy", lambda _ctx: gate.wait(5) and {})
    try:
        assert c.put("/api/settings", json={"clean": False}, headers=H).status_code == 409
        assert c.put("/api/settings", json={"reader_font_pt": 22}, headers=H).status_code == 200
    finally:
        gate.set()
        ctx.tasks.wait(5)


def test_single_flow_preview_build_and_download(session, monkeypatch):
    from core import tasks as core_tasks

    seen = {}

    def fake_run_single(sess, parser, info, chapters, output_path, translated_title, options,
                        *, emit, detail=None):
        seen["chapters"] = [c.title for c in chapters]
        seen["options"] = options
        emit(0.5, "Fetching chapters 1/2")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"PK\x03\x04 epub")
        return core_tasks.SingleResult(path=str(output_path))

    monkeypatch.setattr(core_tasks, "run_single", fake_run_single)
    ctx = _ctx(session)
    c = _signed_in(ctx)
    session.settings["translate"] = False
    p = c.post("/api/preview", json={"url": "https://example.com/book/1"}, headers=H)
    assert p.status_code == 200
    body = p.json()
    assert body["chapter_count"] == 5 and "url" not in str(body["chapters"])
    r = c.post("/api/single", json={"preview_id": body["preview_id"], "chapter_from": 2,
                                    "chapter_to": 3}, headers=H)
    assert r.status_code == 202
    snap = _wait_done(ctx)
    assert snap["state"] == "done", snap
    assert seen["chapters"] == ["Chapter 2", "Chapter 3"]
    files = snap["result"]["files"]
    assert len(files) == 1
    got = c.get(f"/api/files/{files[0]['token']}")
    assert got.status_code == 200 and got.content.startswith(b"PK")
    assert c.post("/api/single", json={"preview_id": body["preview_id"], "chapter_from": 4,
                                       "chapter_to": 2}, headers=H).status_code == 400


def test_second_job_gets_409(session):
    ctx = _ctx(session)
    c = _signed_in(ctx)
    gate = threading.Event()
    ctx.tasks.start("single", "Busy", lambda _ctx: gate.wait(5) and {})
    try:
        p = c.post("/api/preview", json={"url": "https://example.com/book/1"}, headers=H)
        assert p.status_code == 409
    finally:
        gate.set()
        ctx.tasks.wait(5)


def test_multi_lookup_caps_and_dedupes(session):
    from web import books

    c = _signed_in(_ctx(session))
    assert c.post("/api/multi/lookup", json={"urls": []}, headers=H).status_code == 400
    many = [f"https://example.com/book/{i}" for i in range(books.MAX_MULTI + 1)]
    assert c.post("/api/multi/lookup", json={"urls": many}, headers=H).status_code == 400


def test_resume_payload_and_discard(session):
    from core.download_job import load_job, save_job

    ctx = _ctx(session)
    c = _signed_in(ctx)
    assert c.get("/api/state").json()["resume"] is None
    save_job({"kind": "single", "title": "Half Done", "chapters": [],
              "info": {"title": "Half Done", "source_url": "https://example.com/b"}},
             session.data_dir)
    resume = c.get("/api/state").json()["resume"]
    assert resume and "Half Done" in str(resume)
    assert c.post("/api/resume/discard", headers=H).status_code == 200
    assert load_job(session.data_dir) is None


def test_library_list_and_reader_from_cache(session, monkeypatch):
    from web import reader_api

    fake = lambda _u: _Parser("<p>Second chapter text.</p>")  # noqa: E731
    monkeypatch.setattr(reader_api, "get_parser_for_url", fake)
    monkeypatch.setattr("core.parser.get_parser_for_url", fake)  # fetch_reader_chapter
    url = "https://example.com/book/9"
    chapters = [Chapter(title="Chapter 1", url=f"{url}/1"), Chapter(title="Chapter 2", url=f"{url}/2")]
    session.cache.put_chapter_list(url, chapters)
    session.cache.put_chapter(url, f"{url}/1", "Chapter 1", "<p>First chapter text.</p>")
    ctx = _ctx(session)
    c = _signed_in(ctx)
    lib = c.get("/api/library")
    assert lib.status_code == 200
    opened = c.post("/api/read/open", json={"url": url}, headers=H)
    assert opened.status_code == 200, opened.text
    book = opened.json()
    assert [ch["title"] for ch in book["chapters"]] == ["Chapter 1", "Chapter 2"]
    session.settings["translate"] = False
    ch = c.get(f"/api/read/{book['book_id']}/chapter/0")
    assert ch.status_code == 200 and "First chapter text." in ch.json()["html"]
    pos = c.post(f"/api/read/{book['book_id']}/position", json={"index": 1, "scroll": 0.4},
                 headers=H)
    assert pos.status_code == 200
    from core.reading import get_position

    saved = get_position(url, data_dir=session.data_dir)
    assert saved and saved["chapter_index"] == 1


def test_library_update_epub_and_remove(session, monkeypatch):
    from core import tasks as core_tasks
    from web.context import book_roots

    books = book_roots(session)[0]
    books.mkdir(parents=True, exist_ok=True)
    epub = books / "Shelf Book.epub"
    epub.write_bytes(b"PK\x03\x04 old")
    url = "https://example.com/book/shelf"
    session.library_store.upsert_library(url, title="书架", translated_title="Shelf Book",
                                         chapter_count=3, output_path=str(epub),
                                         epub_filename=epub.name)

    def fake_update(sess, entry, options, *, emit, detail=None):
        emit(0.5, "Fetching chapters 1/1")
        Path(entry.output_path).write_bytes(b"PK\x03\x04 new")
        return core_tasks.LibraryUpdateResult(up_to_date=False, display="Shelf Book",
                                              message="1 new chapter.", path=entry.output_path)

    monkeypatch.setattr(core_tasks, "run_library_update", fake_update)
    ctx = _ctx(session)
    c = _signed_in(ctx)
    listing = c.get("/api/library").json()
    shelf = listing["entries"] if isinstance(listing, dict) else listing
    assert any(e["url"] == url and e["has_epub"] for e in shelf)
    assert c.post("/api/library/update", json={"urls": ["https://nope"]}, headers=H).status_code == 404
    r = c.post("/api/library/update", json={"urls": [url]}, headers=H)
    assert r.status_code == 202
    snap = _wait_done(ctx)
    assert snap["state"] == "done" and snap["result"]["files"], snap
    got = c.post("/api/library/epub", json={"url": url}, headers=H)
    assert got.status_code == 200
    data = c.get(f"/api/files/{got.json()['file']['token']}")
    assert data.content.endswith(b"new")
    assert c.post("/api/library/update-all", headers=H).status_code == 400  # nothing checked
    removed = c.post("/api/library/remove", json={"urls": [url]}, headers=H)
    assert removed.status_code == 200 and removed.json()["removed"] == 1
    assert session.library_store.get_library_entry(url) is None
    assert not epub.exists()


def test_library_card_omits_runaway_english_subtitle(session):
    """A repeated English rendering must not be sent as the Chinese title."""
    runaway = "https://example.com/book/runaway"
    normal = "https://example.com/book/normal"
    session.library_store.upsert_library(
        runaway,
        title=("A cigarette smokes a smoke " * 80).strip(),
        translated_title="The Low-Key Prince: Summoning Black and White Impermanence at the Start",
        author="space the space the sky",
        chapter_count=431,
        description=("A cigarette smokes a smoke " * 80),
    )
    long_zh = "低调" * 80
    session.library_store.upsert_library(
        normal,
        title=long_zh,
        translated_title="The Low-Key Prince",
        author="Otaku Landlord",
        chapter_count=12,
    )
    entries = {
        e["url"]: e
        for e in _signed_in(_ctx(session)).get("/api/library").json()["entries"]
    }
    assert entries[runaway]["title"].startswith("The Low-Key Prince")
    assert entries[runaway]["title_original"] == ""
    assert entries[runaway]["author"] == "space the space the sky"
    assert entries[runaway]["chapters"] == 431
    assert entries[runaway]["description"]
    assert len(entries[runaway]["description"]) <= 800
    assert entries[normal]["description"] == ""
    original = entries[normal]["title_original"]
    assert original.startswith("低调")
    assert len(original) <= 120
    assert entries[normal]["title"] == "The Low-Key Prince"


def test_library_chapter_list_returns_every_title(session):
    url = "https://example.com/book/toc"
    session.library_store.upsert_library(url, title="书架", translated_title="Shelf", chapter_count=40)
    session.cache.put_chapter_list(url, [
        Chapter(title=f"第{i}章", url=f"{url}/{i}") for i in range(1, 41)
    ])
    c = _signed_in(_ctx(session))
    got = c.get("/api/library/chapters", params={"u": url})
    assert got.status_code == 200
    rows = got.json()["chapters"]
    assert len(rows) == 40
    assert rows[0] == {"n": 1, "title": "第1章"}
    assert rows[-1]["n"] == 40
    missing = c.get("/api/library/chapters", params={"u": "https://example.com/missing"})
    assert missing.status_code == 404


def test_library_chapter_list_prefers_stored_english_titles(session):
    url = "https://example.com/book/en-titles"
    session.library_store.upsert_library(url, title="书", translated_title="Book", chapter_count=2)
    session.cache.put_chapter_list(url, [
        Chapter(title="第1章 天赋剥离", url=f"{url}/1"),
        Chapter(title="第2章 大战", url=f"{url}/2"),
    ])
    session.cache.put_english_chapter_titles(url, [
        {"url": f"{url}/1", "title": "Chapter 1 Talent Stripped"},
        {"url": f"{url}/2", "title": ""},
    ])
    c = _signed_in(_ctx(session))
    rows = c.get("/api/library/chapters", params={"u": url}).json()["chapters"]
    assert rows[0]["title"] == "Chapter 1 Talent Stripped"
    assert rows[1]["title"] == "第2章 大战"
    session.cache.put_chapter_list(url, [
        Chapter(title="第1章 天赋剥离", url=f"{url}/1"),
        Chapter(title="第2章 大战", url=f"{url}/2"),
        Chapter(title="第3章 新", url=f"{url}/3"),
    ])
    rows = c.get("/api/library/chapters", params={"u": url}).json()["chapters"]
    assert [row["title"] for row in rows] == [
        "Chapter 1 Talent Stripped",
        "第2章 大战",
        "第3章 新",
    ]


def test_library_chapter_list_reads_english_from_local_epub_once(session, monkeypatch):
    from types import SimpleNamespace

    url = "https://example.com/book/epub-en"
    session.library_store.upsert_library(
        url, title="书", translated_title="Book", chapter_count=2, output_path="C:/books/book.epub",
    )
    session.cache.put_chapter_list(url, [
        Chapter(title="第1章", url=f"{url}/1"),
        Chapter(title="第2章", url=f"{url}/2"),
    ])
    calls = {"n": 0}

    def fake_load(_path, bodies=True):
        calls["n"] += 1
        return [
            SimpleNamespace(title="Chapter 1 Talent Stripped! System activated!"),
            SimpleNamespace(title="第2章 大战"),
        ]

    monkeypatch.setattr("web.library_api.find_local_epub", lambda **_k: Path("book.epub"))
    monkeypatch.setattr("web.library_api.load_epub_chapters", fake_load)
    c = _signed_in(_ctx(session))
    rows = c.get("/api/library/chapters", params={"u": url}).json()["chapters"]
    assert rows[0]["title"] == "Chapter 1 Talent Stripped! System activated!"
    assert rows[1]["title"] == "第2章"
    assert calls["n"] == 1
    again = c.get("/api/library/chapters", params={"u": url}).json()["chapters"]
    assert again[0]["title"] == rows[0]["title"]
    assert calls["n"] == 1
    stored = session.cache.get_english_chapter_titles(url)
    assert stored[0]["title"].startswith("Chapter 1 Talent")
    assert stored[1]["title"] == ""


# ----------------------------------------------------------------------
# Running the real server
# ----------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_host_starts_and_stops(session):
    pytest.importorskip("uvicorn")
    import requests

    from web.host import ServerHost, ServerStartError

    host = ServerHost(session, version="1.0", log=lambda _m: None)
    port = _free_port()
    status = host.start(mode="lan", port=port)
    try:
        assert host.running and status.local_url.endswith(f":{port}/")
        r = requests.get(f"http://127.0.0.1:{port}/api/session", timeout=5)
        assert r.status_code == 200 and r.json()["mode"] == "lan"
        link = host.open_link()
        assert link.startswith(status.local_url + "open/")
        assert f"?code={host.secrets.lan_code}" in host.phone_link()
    finally:
        host.stop()
    assert not host.running
    with pytest.raises(ServerStartError):
        host.start(mode="remote", port=_free_port())  # no password yet


def test_stop_file_ends_a_headless_wait(tmp_path):
    from web.host import (
        pid_alive, request_headless_stop, running_headless_pid, serve_until_stopped,
    )

    assert pid_alive(os.getpid())

    class _Host:
        def __init__(self):
            self.running = True

        def stop(self):
            self.running = False

    class _Session:
        def __init__(self):
            self.data_dir = tmp_path
            self.closed = False

        def close(self):
            self.closed = True

    host = _Host()
    session = _Session()
    worker = threading.Thread(
        target=serve_until_stopped, args=(host, session, threading.Event()), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        pid = None
        while time.monotonic() < deadline and pid is None:
            pid = running_headless_pid(tmp_path)
            time.sleep(0.05)
        assert pid == os.getpid()
        request_headless_stop(tmp_path)
        worker.join(5)
        assert not worker.is_alive()
    finally:
        request_headless_stop(tmp_path)
        worker.join(5)
    assert not host.running and session.closed
    assert running_headless_pid(tmp_path) is None

    gone = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    gone.kill()
    gone.wait(timeout=5)
    folder = tmp_path / "server"
    folder.mkdir(exist_ok=True)
    (folder / "headless.pid").write_text(str(gone.pid), encoding="utf-8")
    assert running_headless_pid(tmp_path) is None
    assert not (folder / "headless.pid").exists()


def test_linux_without_a_display_is_headless(monkeypatch):
    from web.host import headless_requested

    monkeypatch.setattr(sys, "platform", "linux")
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(key, raising=False)
    assert headless_requested([])
    monkeypatch.setenv("DISPLAY", ":0")
    assert not headless_requested([])
    assert headless_requested(["--headless"])
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert not headless_requested([])


def test_headless_serves_without_qt(tmp_path):
    pytest.importorskip("uvicorn")
    import json

    import requests

    port = _free_port()
    data = tmp_path / ".huaepub"
    data.mkdir()
    (data / "settings.json").write_text(
        json.dumps({"server_mode": "lan", "server_port": port}), encoding="utf-8")
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "sys.argv = ['huaepub', '--headless']\n"
        "import gui.app as appmod\n"
        "import web.host as hostmod\n"
        "real = hostmod.serve_headless\n"
        "def wrapped():\n"
        "    bad = [name for name in sys.modules if name == 'PySide6' or name.startswith('PySide6.')]\n"
        "    if bad:\n"
        "        raise SystemExit('Qt loaded before headless: ' + ', '.join(bad))\n"
        "    return real()\n"
        "hostmod.serve_headless = wrapped\n"
        "raise SystemExit(appmod.run())\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    out_parts: list[str] = []
    err_parts: list[str] = []

    def _drain(stream, dest):
        for line in stream:  # line by line, so the test can wait for the banner
            dest.append(line)

    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, out_parts)),
        threading.Thread(target=_drain, args=(proc.stderr, err_parts)),
    ]
    for thread in threads:
        thread.start()
    seen = None
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                got = requests.get(f"http://127.0.0.1:{port}/api/session", timeout=1)
            except requests.RequestException:
                time.sleep(0.1)
                continue
            if got.status_code == 200:
                seen = got
                break
        # The server answers before the banner is printed: wait for it before stopping
        # the process, or a fast kill (TerminateProcess on Windows) loses the line.
        while time.monotonic() < deadline and proc.poll() is None:
            if "Access code: " in "".join(out_parts):
                break
            time.sleep(0.05)
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
    text = "".join(out_parts)
    err = "".join(err_parts)
    assert seen is not None and seen.json()["mode"] == "lan", (proc.returncode, text, err)
    marker = "Access code: "
    assert marker in text, text
    code = text.split(marker, 1)[1].splitlines()[0].strip()
    log = data / "logs" / "huaepub.log"
    logged = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    assert code and code not in logged



def test_remote_server_speaks_https_only(session):
    pytest.importorskip("uvicorn")
    import requests

    from web.host import ServerHost

    host = ServerHost(session, version="1.0", log=lambda _m: None)
    host.secrets.set_password("remote password 123")
    port = _free_port()
    status = host.start(mode="remote", port=port)
    try:
        assert status.https and status.fingerprint
        r = requests.get(f"https://127.0.0.1:{port}/api/session", timeout=5, verify=False)
        assert r.status_code == 200 and r.json()["mode"] == "remote"
        with pytest.raises(requests.exceptions.RequestException):
            requests.get(f"http://127.0.0.1:{port}/api/session", timeout=5)
    finally:
        host.stop()


# ----------------------------------------------------------------------
# Boundaries and packaging
# ----------------------------------------------------------------------


def test_web_never_loads_qt_and_core_never_loads_web_or_gui():
    code = (
        "import sys, web.server, web.host, web.books, web.tasks, web.library_api, "
        "web.reader_api, web.tls, web.auth\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in ('PySide6', 'gui')]\n"
        "assert not bad, bad\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    for folder in ("core", "parsers", "web"):
        for path in (ROOT / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if folder != "web":
                assert "import web" not in text and "from web" not in text, path
            assert "from gui" not in text and "import gui" not in text, path
            assert "PySide6" not in text, path


def test_packaging_ships_server_mode():
    build = (ROOT / "build.py").read_text(encoding="utf-8")
    assert '"web"' in build
    assert "uvicorn" in build
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"web*"' in pyproject and "static/*" in pyproject
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for name in ("fastapi", "uvicorn", "qrcode", "cryptography"):
        assert name in reqs, name
    from core.updater import SOURCE_UPDATE_ITEMS

    assert "web" in SOURCE_UPDATE_ITEMS
    assert not (ROOT / "web" / "__main__.py").exists()


def test_an_open_book_counts_as_reading_until_it_goes_quiet():
    from web.reader_api import ReaderStore

    store = ReaderStore()
    assert not store.active()
    item = store.put(object())
    assert store.active()
    assert store.get(item.id) is item
    item.seen = time.monotonic() - 181
    assert not store.active()


def test_card_description_turns_site_markup_into_lines():
    from web.library_api import card_description

    raw = "[Cultivation] + [Stable]<br /> Han Xuanji &amp; <b>friends</b>.<br/><br/>  Next   line"
    assert card_description(raw) == "[Cultivation] + [Stable]\nHan Xuanji & friends.\nNext line"
