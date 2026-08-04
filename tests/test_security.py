"""Tests for core.security helpers."""

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from core.security import (
    UnsafeURLError,
    is_fetch_url_safe,
    safe_extract_zip,
    validate_fetch_url,
    validate_libretranslate_url,
    write_secret_file,
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
            zf.writestr("NovelDownloader.exe", b"MZ")
            zf.writestr("readme.txt", b"hi")
        out = tmp_path / "out"
        written = safe_extract_zip(zip_path, out, allowed_names={"NovelDownloader.exe"})
        assert any(p.name == "NovelDownloader.exe" for p in written)
        assert not (out / "readme.txt").exists()


class TestWriteSecretFile:
    def test_writes_content(self, tmp_path):
        path = tmp_path / "secret.json"
        write_secret_file(path, '{"token":"x"}')
        assert path.read_text(encoding="utf-8") == '{"token":"x"}'
