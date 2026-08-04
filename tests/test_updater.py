"""Tests for core.updater."""

import hashlib
import sys
import types

from core import updater


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            'tag_name': 'v9.9.9',
            'body': None,
            'html_url': 'https://example.com/release',
        }


class FakeSession:
    def __init__(self, *args, **kwargs):
        self.headers = {}

    def get(self, *args, **kwargs):
        return FakeResponse()


class TestCheckForUpdatesNullBody:
    def test_null_release_body_does_not_crash(self, monkeypatch):
        monkeypatch.setattr(updater, '__version__', '1.0.0')

        # Force the requests fallback so we can inject a fake Session.
        curl_cffi = types.ModuleType('curl_cffi')
        curl_cffi_requests = types.ModuleType('curl_cffi.requests')

        def _boom(*args, **kwargs):
            raise ImportError('forced')

        curl_cffi_requests.Session = _boom
        monkeypatch.setitem(sys.modules, 'curl_cffi', curl_cffi)
        monkeypatch.setitem(sys.modules, 'curl_cffi.requests', curl_cffi_requests)

        fake_requests = types.ModuleType('requests')
        fake_requests.Session = FakeSession
        monkeypatch.setitem(sys.modules, 'requests', fake_requests)

        has_update, latest, message = updater.check_for_updates()
        assert has_update is True
        assert latest == '9.9.9'
        assert 'No release notes available.' in message


class TestRequireChecksum:
    def test_fails_closed_without_sums(self):
        class Sess:
            def get(self, *a, **k):
                raise AssertionError("should not download sums when absent")

        release = {"assets": [{"name": "NovelDownloader-windows.zip", "browser_download_url": "https://x"}]}
        ok, msg = updater._require_checksum(Sess(), release, "NovelDownloader-windows.zip", b"data")
        assert ok is False
        assert "SHA256SUMS" in msg

    def test_mismatch(self):
        class Resp:
            def raise_for_status(self):
                return None
            text = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  NovelDownloader-windows.zip\n"

        class Sess:
            def get(self, *a, **k):
                return Resp()

        release = {
            "assets": [
                {"name": "SHA256SUMS.txt", "browser_download_url": "https://example/sums"},
            ]
        }
        ok, msg = updater._require_checksum(Sess(), release, "NovelDownloader-windows.zip", b"data")
        assert ok is False
        assert "checksum mismatch" in msg.lower()

    def test_ok(self):
        payload = b"hello-update"
        digest = hashlib.sha256(payload).hexdigest()

        class Resp:
            def raise_for_status(self):
                return None
            text = f"{digest}  NovelDownloader-windows.zip\n"

        class Sess:
            def get(self, *a, **k):
                return Resp()

        release = {
            "assets": [
                {"name": "SHA256SUMS.txt", "browser_download_url": "https://example/sums"},
            ]
        }
        ok, msg = updater._require_checksum(Sess(), release, "NovelDownloader-windows.zip", payload)
        assert ok is True
        assert msg == digest
