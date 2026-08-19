"""Tests for core.updater."""

import hashlib
import sys
import types
from pathlib import Path

from core import updater
from core.updater import SOURCE_UPDATE_ITEMS

_GITHUB_SUMS = (
    "https://github.com/joelsnl/HuaEPUB/releases/download/v9.9.9/SHA256SUMS.txt"
)


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
                {"name": "SHA256SUMS.txt", "browser_download_url": _GITHUB_SUMS},
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
                {"name": "SHA256SUMS.txt", "browser_download_url": _GITHUB_SUMS},
            ]
        }
        ok, msg = updater._require_checksum(Sess(), release, "HuaEPUB-windows.zip", payload)
        assert ok is True
        assert msg == digest

    def test_rejects_non_github_sums_host(self):
        class Sess:
            def get(self, *a, **k):
                raise AssertionError("must not fetch a non-GitHub checksum URL")

        release = {
            "assets": [
                {
                    "name": "SHA256SUMS.txt",
                    "browser_download_url": "https://evil.example/SHA256SUMS.txt",
                },
            ]
        }
        ok, msg = updater._require_checksum(
            Sess(), release, "HuaEPUB-windows.zip", b"data"
        )
        assert ok is False
        assert "SHA256SUMS" in msg


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

    def test_posix_helper_is_shell_not_frozen_python(self, tmp_path, monkeypatch):
        for platform in ("linux", "darwin"):
            monkeypatch.setattr(updater.sys, "platform", platform)
            new_exe = tmp_path / f"_new_HuaEPUB_{platform}"
            old_exe = tmp_path / f"HuaEPUB_{platform}"
            new_exe.write_bytes(b"new")
            old_exe.write_bytes(b"old")

            script = updater._create_replacement_helper(new_exe, old_exe, tmp_path, pid=99)
            text = script.read_text(encoding="utf-8")
            assert script.name == "_update_helper.sh"
            assert text.startswith("#!/bin/sh")
            # Primary path: python3 owns replace (paths never in shell vars)
            assert "python3 - " in text
            assert "hashlib.sha256" in text
            assert "Staged binary checksum" in text
            # Fallback still strips PyInstaller env; never env -i
            assert "env -i" not in text
            assert "-u SSL_CERT_FILE" in text
            assert "com.apple.quarantine" in text
            cfg = (tmp_path / "_update_helper.json").read_text(encoding="utf-8")
            assert '"sha256"' in cfg
            assert '"pid": 99' in cfg

    def test_windows_helper_rehashes_staged_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater.sys, "platform", "win32")
        new_exe = tmp_path / "_new_HuaEPUB.exe"
        old_exe = tmp_path / "HuaEPUB.exe"
        new_exe.write_bytes(b"new")
        old_exe.write_bytes(b"old")
        script = updater._create_replacement_helper(new_exe, old_exe, tmp_path, pid=7)
        text = script.read_text(encoding="utf-8")
        assert "Get-FileHash" in text
        assert "checksum mismatch" in text.lower()

    def test_posix_launch_uses_shell_not_sys_executable(self, tmp_path, monkeypatch):
        for platform in ("linux", "darwin"):
            monkeypatch.setattr(updater.sys, "platform", platform)
            monkeypatch.setattr(updater, "is_frozen", lambda: True)
            monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "HuaEPUB"))
            calls = []

            def fake_popen(args, **kwargs):
                calls.append((args, kwargs))
                class P:
                    pass
                return P()

            monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
            script = tmp_path / "_update_helper.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            updater._launch_replacement_script(script)
            assert calls, platform
            interp = calls[-1][0][0]
            assert interp != str(tmp_path / "HuaEPUB"), platform
            assert Path(interp).name in ("bash", "sh"), platform
            assert calls[-1][0][1] == str(script)

    def test_posix_interpreter_skips_frozen_exe(self, tmp_path, monkeypatch):
        fake_app = tmp_path / "HuaEPUB"
        fake_app.write_bytes(b"app")
        monkeypatch.setattr(updater, "is_frozen", lambda: True)
        monkeypatch.setattr(updater.sys, "executable", str(fake_app))
        chosen = updater._posix_helper_interpreter()
        assert Path(chosen).resolve() != fake_app.resolve()
        assert Path(chosen).name in ("bash", "sh")


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
        assert "SSL_CERT_FILE" in text
        assert '"pid": 4242' in (tmp_path / "_update_relaunch.json").read_text(encoding="utf-8")


class TestSourceUpdateItems:
    def test_includes_gui(self):
        assert "gui" in SOURCE_UPDATE_ITEMS
        assert "core" in SOURCE_UPDATE_ITEMS
        assert "app.py" in SOURCE_UPDATE_ITEMS
