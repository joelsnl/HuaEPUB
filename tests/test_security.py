"""Tests for core.security helpers."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from core.security import (
    UnsafeURLError,
    fetch_cover_bytes,
    github_asset_host_ok,
    is_allowed_epub_path,
    is_fetch_url_safe,
    safe_epub_basename,
    safe_extract_zip,
    safe_http_request,
    validate_fetch_url,
    validate_github_asset_host,
    validate_libretranslate_url,
    validate_ollama_url,
    validate_update_helper_paths,
    write_secret_file,
    write_update_helper_config,
)


class TestValidateFetchUrl:
    def test_blocks_file_scheme(self):
        with pytest.raises(UnsafeURLError):
            validate_fetch_url("file:///etc/passwd", resolve_dns=False)

    def test_blocks_loopback_literal(self):
        with pytest.raises(UnsafeURLError):
            validate_fetch_url("http://127.0.0.1/x", resolve_dns=False)

    def test_blocks_private_literal(self):
        with pytest.raises(UnsafeURLError):
            validate_fetch_url("http://192.168.1.10/book", resolve_dns=False)

    def test_blocks_localhost_name(self):
        with pytest.raises(UnsafeURLError):
            validate_fetch_url("http://localhost/x", resolve_dns=False)

    def test_blocks_credentials_in_url(self):
        with pytest.raises(UnsafeURLError):
            validate_fetch_url("https://user:pass@example.com/", resolve_dns=False)

    def test_allows_https_public_without_dns(self):
        validate_fetch_url("https://example.com/book/1", resolve_dns=False)
        assert is_fetch_url_safe("https://example.com/book/1", resolve_dns=False)


class TestLibreTranslateUrl:
    def test_rejects_loopback(self):
        with pytest.raises(UnsafeURLError):
            validate_libretranslate_url("http://127.0.0.1:5000")


class TestOllamaUrl:
    def test_accepts_loopback(self):
        assert validate_ollama_url("http://127.0.0.1:11434/") == "http://127.0.0.1:11434"
        assert validate_ollama_url("http://localhost:11434") == "http://localhost:11434"

    def test_rejects_non_local(self):
        with pytest.raises(UnsafeURLError):
            validate_ollama_url("https://example.com/ollama")
        with pytest.raises(UnsafeURLError):
            validate_ollama_url("http://192.168.1.10:11434")

    def test_fetch_url_loopback_opt_in(self):
        validate_fetch_url("http://127.0.0.1:11434/api/chat", allow_loopback=True)
        with pytest.raises(UnsafeURLError):
            validate_fetch_url("http://127.0.0.1:11434/api/chat")


class TestSafeHttpRequest:
    def test_blocks_redirect_to_private(self, monkeypatch):
        import core.security as sec

        def fake_validate(url, **kwargs):
            if "127." in url or "192.168." in url or "localhost" in url:
                raise UnsafeURLError(f"Blocked URL host: {url}")

        monkeypatch.setattr(sec, "validate_fetch_url", fake_validate)

        class Resp:
            def __init__(self, status, location=None):
                self.status_code = status
                self.headers = {"Location": location} if location else {}
                self.content = b""
                self.text = ""

        class Sess:
            def request(self, method, url, allow_redirects=False, timeout=30, **kwargs):
                assert allow_redirects is False
                return Resp(302, "http://127.0.0.1/secret")

        with pytest.raises(UnsafeURLError, match="127.0.0.1|Blocked"):
            safe_http_request(Sess(), "GET", "https://example.com/a")

    def test_follows_safe_redirect(self, monkeypatch):
        import core.security as sec

        monkeypatch.setattr(
            sec, "validate_fetch_url", lambda url, **kwargs: None
        )

        class Resp:
            def __init__(self, status, location=None):
                self.status_code = status
                self.headers = {"Location": location} if location else {}
                self.content = b"ok"
                self.text = "ok"

        class Sess:
            def __init__(self):
                self.urls = []

            def request(self, method, url, allow_redirects=False, timeout=30, **kwargs):
                self.urls.append(url)
                if url.endswith("/a"):
                    return Resp(302, "https://example.com/b")
                return Resp(200)

        sess = Sess()
        resp = safe_http_request(sess, "GET", "https://example.com/a")
        assert resp.status_code == 200
        assert sess.urls == [
            "https://example.com/a",
            "https://example.com/b",
        ]

    def test_resolve_dns_false_skips_lookup_on_first_hop(self, monkeypatch):
        import socket
        import core.security as sec

        def boom(*a, **k):
            raise AssertionError("DNS should not run on first hop")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        # Real validate_fetch_url with resolve_dns=False must not touch DNS.

        class Resp:
            status_code = 200
            headers = {}

        class Sess:
            def request(self, method, url, allow_redirects=False, timeout=30, **kwargs):
                return Resp()

        resp = sec.safe_http_request(
            Sess(),
            "GET",
            "https://translate.googleapis.com/translate_a/single",
            allow_http=False,
            resolve_dns=False,
        )
        assert resp.status_code == 200

    def test_redirect_hop_still_resolves_dns(self, monkeypatch):
        import core.security as sec

        seen = []

        def fake_validate(url, **kwargs):
            seen.append((url, kwargs.get("resolve_dns")))

        monkeypatch.setattr(sec, "validate_fetch_url", fake_validate)

        class Resp:
            def __init__(self, status, location=None):
                self.status_code = status
                self.headers = {"Location": location} if location else {}

        class Sess:
            def request(self, method, url, allow_redirects=False, timeout=30, **kwargs):
                if url.endswith("/first"):
                    return Resp(302, "https://example.com/second")
                return Resp(200)

        sec.safe_http_request(
            Sess(),
            "GET",
            "https://translate.googleapis.com/first",
            resolve_dns=False,
        )
        assert seen[0] == ("https://translate.googleapis.com/first", False)
        assert seen[1] == ("https://example.com/second", True)


class TestSafeExtractZip:
    def test_rejects_zip_slip(self, tmp_path):
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../escape.txt", "nope")
        with pytest.raises(ValueError):
            safe_extract_zip(zip_path, tmp_path / "out")

    def test_extracts_safe_member(self, tmp_path):
        zip_path = tmp_path / "ok.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("HuaEPUB.exe", b"MZ")
            zf.writestr("readme.txt", b"hi")
        out = tmp_path / "out"
        written = safe_extract_zip(zip_path, out, allowed_names={"HuaEPUB.exe"})
        assert any(p.name == "HuaEPUB.exe" for p in written)
        assert not (out / "readme.txt").exists()

    def test_rejects_total_uncompressed_over_limit(self, tmp_path):
        zip_path = tmp_path / "bomb.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.bin", b"x" * 100)
            zf.writestr("b.bin", b"y" * 100)
        with pytest.raises(ValueError, match="uncompressed size"):
            safe_extract_zip(
                zip_path,
                tmp_path / "out",
                max_member_bytes=1000,
                max_total_bytes=150,
            )


class TestWriteSecretFile:
    def test_writes_content(self, tmp_path):
        path = tmp_path / "secret.json"
        write_secret_file(path, '{"token":"x"}')
        assert path.read_text(encoding="utf-8") == '{"token":"x"}'


class TestUpdateHelperConfig:
    def test_rejects_metachar_paths(self, tmp_path):
        with pytest.raises(ValueError, match="metachar"):
            validate_update_helper_paths(
                tmp_path / "a$(id)",
                tmp_path / "b",
                tmp_path,
            )

    def test_writes_sha256_and_restricts_paths(self, tmp_path):
        new_exe = tmp_path / "_new_HuaEPUB"
        old_exe = tmp_path / "HuaEPUB"
        new_exe.write_bytes(b"new")
        old_exe.write_bytes(b"old")
        digest = hashlib.sha256(b"new").hexdigest()
        cfg = tmp_path / "_update_helper.json"
        write_update_helper_config(
            cfg, new_exe=new_exe, old_exe=old_exe, pid=42, sha256=digest
        )
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["pid"] == 42
        assert data["sha256"] == digest
        assert Path(data["new_exe"]).name == "_new_HuaEPUB"


class TestGithubAssetHost:
    def test_allows_github_and_cdn(self):
        validate_github_asset_host(
            "https://github.com/joelsnl/HuaEPUB/releases/download/v1/x.zip"
        )
        validate_github_asset_host(
            "https://objects.githubusercontent.com/github-production-release-asset-2e65be/x"
        )
        assert github_asset_host_ok("release-assets.githubusercontent.com")

    def test_rejects_other_hosts(self):
        with pytest.raises(UnsafeURLError):
            validate_github_asset_host("https://evil.example/x.zip")
        assert not github_asset_host_ok("github.io")


class TestEpubPaths:
    def test_basename_strips_directories(self):
        assert safe_epub_basename(r"..\..\Windows\foo.epub") == "foo.epub"
        assert safe_epub_basename("foo.exe") == ""
        assert safe_epub_basename("foo.exe", fallback_title="Book") == "Book.epub"

    def test_allowed_path_stays_under_root(self, tmp_path):
        books = tmp_path / "books"
        books.mkdir()
        good = books / "A.epub"
        good.write_bytes(b"x")
        outside = tmp_path / "B.epub"
        outside.write_bytes(b"y")
        assert is_allowed_epub_path(good, [books])
        assert not is_allowed_epub_path(outside, [books])


class TestFetchCoverBytes:
    def test_rejects_oversize(self, monkeypatch):
        import core.security as sec

        monkeypatch.setattr(sec, "safe_http_request", lambda *a, **k: type("R", (), {
            "status_code": 200,
            "content": b"x" * (sec.MAX_COVER_BYTES + 1),
            "raise_for_status": lambda self: None,
        })())
        with pytest.raises(UnsafeURLError, match="size limit"):
            fetch_cover_bytes(object(), "https://example.com/cover.jpg")

    def test_extra_check_runs_on_redirect(self, monkeypatch):
        import core.security as sec

        monkeypatch.setattr(sec, "validate_fetch_url", lambda url, **kwargs: None)

        class Resp:
            def __init__(self, status, location=None):
                self.status_code = status
                self.headers = {"Location": location} if location else {}
                self.content = b"ok"

        class Sess:
            def request(self, method, url, allow_redirects=False, timeout=30, **kwargs):
                if url.endswith("/a"):
                    return Resp(302, "https://evil.example/b")
                return Resp(200)

        hops = []

        def extra(url):
            hops.append(url)
            if "evil.example" in url:
                raise UnsafeURLError("not github")

        with pytest.raises(UnsafeURLError, match="not github"):
            safe_http_request(
                Sess(), "GET", "https://github.com/a", extra_check=extra
            )
        assert hops[-1].startswith("https://evil.example")
