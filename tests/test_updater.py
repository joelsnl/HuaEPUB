"""Tests for core.updater."""

import hashlib
import sys
import types
from pathlib import Path

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

        release = {"assets": [{"name": "HuaEPUB-windows.zip", "browser_download_url": "https://x"}]}
        ok, msg = updater._require_checksum(Sess(), release, "HuaEPUB-windows.zip", b"data")
        assert ok is False
        assert "SHA256SUMS" in msg

    def test_mismatch(self):
        class Resp:
            def raise_for_status(self):
                return None
            text = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  HuaEPUB-windows.zip\n"

        class Sess:
            def get(self, *a, **k):
                return Resp()

        release = {
            "assets": [
                {"name": "SHA256SUMS.txt", "browser_download_url": "https://example/sums"},
            ]
        }
        ok, msg = updater._require_checksum(Sess(), release, "HuaEPUB-windows.zip", b"data")
        assert ok is False
        assert "checksum mismatch" in msg.lower()

    def test_ok(self):
        payload = b"hello-update"
        digest = hashlib.sha256(payload).hexdigest()

        class Resp:
            def raise_for_status(self):
                return None
            text = f"{digest}  HuaEPUB-windows.zip\n"

        class Sess:
            def get(self, *a, **k):
                return Resp()

        release = {
            "assets": [
                {"name": "SHA256SUMS.txt", "browser_download_url": "https://example/sums"},
            ]
        }
        ok, msg = updater._require_checksum(Sess(), release, "HuaEPUB-windows.zip", payload)
        assert ok is True
        assert msg == digest


class TestReplacementHelper:
    def test_windows_helper_retries_and_avoids_stop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater.sys, "platform", "win32")
        new_exe = tmp_path / "_new_HuaEPUB.exe"
        old_exe = tmp_path / "HuaEPUB.exe"
        new_exe.write_bytes(b"new")
        old_exe.write_bytes(b"old")

        script = updater._create_replacement_helper(new_exe, old_exe, tmp_path, pid=12345)
        text = script.read_text(encoding="utf-8")
        assert script.name == "_update_helper.ps1"
        assert "$ErrorActionPreference = \"Continue\"" in text
        assert "for ($i = 1; $i -le 90; $i++)" in text
        assert "Start-Process -FilePath $oldExe" in text
        assert "$pidWait" in text
        # Must not wait on PowerShell's automatic $PID by mistake
        assert "Get-Process -Id $pidWait" in text
        cfg = (tmp_path / "_update_helper.json").read_text(encoding="utf-8")
        assert '"pid": 12345' in cfg

    def test_posix_helper_retries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater.sys, "platform", "linux")
        new_exe = tmp_path / "_new_HuaEPUB"
        old_exe = tmp_path / "HuaEPUB"
        new_exe.write_bytes(b"new")
        old_exe.write_bytes(b"old")

        script = updater._create_replacement_helper(new_exe, old_exe, tmp_path, pid=99)
        text = script.read_text(encoding="utf-8")
        assert script.name == "_update_helper.py"
        assert "for i in range(1, 91)" in text
        assert "os.spawnv" in text
        assert Path(script).exists()


class TestSwapRunningExeWindows:
    def test_renames_then_replaces(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater.sys, "platform", "win32")
        old_exe = tmp_path / "NovelDownloader.exe"
        new_exe = tmp_path / "_new_NovelDownloader.exe"
        old_exe.write_bytes(b"old-bytes")
        new_exe.write_bytes(b"new-bytes")

        backup = updater._swap_running_exe_windows(new_exe, old_exe)
        assert old_exe.read_bytes() == b"new-bytes"
        assert backup.read_bytes() == b"old-bytes"
        assert not new_exe.exists()


class TestPostSwapRelaunchHelper:
    def test_writes_hidden_wait_script(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater.sys, "platform", "win32")
        exe = tmp_path / "HuaEPUB.exe"
        backup = tmp_path / "_update_backup.exe"
        exe.write_bytes(b"new")
        backup.write_bytes(b"old")
        script = updater._create_post_swap_relaunch_helper(exe, backup, tmp_path, pid=4242)
        text = script.read_text(encoding="utf-8")
        assert script.name == "_update_relaunch.ps1"
        assert "Start-Sleep" in text
        assert "timeout /t" not in text.lower()
        assert "Start-Process" in text
        assert '"pid": 4242' in (tmp_path / "_update_relaunch.json").read_text(encoding="utf-8")
