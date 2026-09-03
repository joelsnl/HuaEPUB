"""Tests for core.updater."""

import hashlib
from pathlib import Path

import pytest

from core import updater
from core.branding import UPDATER_USER_AGENT
from core.security import validate_github_asset_host
from core.updater import SOURCE_UPDATE_ITEMS

_GITHUB_SUMS = (
    "https://github.com/joelsnl/HuaEPUB/releases/download/v9.9.9/SHA256SUMS.txt"
)


@pytest.fixture
def reset_updater_check():
    updater._reset_updater_check_state()
    yield
    updater._reset_updater_check_state()


class TestCheckForUpdatesNullBody:
    def test_null_release_body_does_not_crash(self, monkeypatch, reset_updater_check):
        monkeypatch.setattr(updater, '__version__', '1.0.0')

        def fake_fetch(session, *, timeout):
            return 200, {
                'tag_name': 'v9.9.9',
                'body': None,
                'html_url': 'https://example.com/release',
            }

        monkeypatch.setattr(updater, '_fetch_latest_release', fake_fetch)
        has_update, latest, message = updater.check_for_updates(force=True)
        assert has_update is True
        assert latest == '9.9.9'
        assert 'No release notes available.' in message


class TestCheckForUpdatesFastPath:
    def test_uses_short_timeout_and_github_host_pin(
        self, monkeypatch, reset_updater_check
    ):
        captured = {}

        class Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"tag_name": "v1.0.0", "body": ""}

        def fake_safe(session, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["timeout"] = kwargs.get("timeout")
            captured["extra_check"] = kwargs.get("extra_check")
            captured["allow_http"] = kwargs.get("allow_http")
            captured["resolve_dns"] = kwargs.get("resolve_dns")
            return Resp()

        monkeypatch.setattr(updater, "__version__", "1.0.0")
        monkeypatch.setattr(updater, "safe_http_request", fake_safe)
        monkeypatch.setattr(updater, "_check_http_session", lambda: object())
        has_update, latest, message = updater.check_for_updates(force=True)
        assert has_update is False
        assert latest == "1.0.0"
        assert "latest version" in message
        assert captured["method"] == "GET"
        assert captured["url"] == updater.GITHUB_API_URL
        assert captured["timeout"] == updater.CHECK_TIMEOUT
        assert captured["timeout"][0] < 15
        assert captured["extra_check"] is validate_github_asset_host
        assert captured["allow_http"] is False
        assert captured["resolve_dns"] is False

    def test_does_not_download_sha256sums(self, monkeypatch, reset_updater_check):
        monkeypatch.setattr(updater, "__version__", "1.0.0")

        def fake_fetch(session, *, timeout):
            return 200, {
                "tag_name": "v1.0.0",
                "body": "",
                "assets": [
                    {
                        "name": "SHA256SUMS.txt",
                        "browser_download_url": _GITHUB_SUMS,
                    }
                ],
            }

        def boom(*a, **k):
            raise AssertionError("check must not download SHA256SUMS")

        monkeypatch.setattr(updater, "_fetch_latest_release", fake_fetch)
        monkeypatch.setattr(updater, "_get_expected_checksum", boom)
        monkeypatch.setattr(updater, "_require_checksum", boom)
        has_update, latest, _message = updater.check_for_updates(force=True)
        assert has_update is False
        assert latest == "1.0.0"

    def test_coalesces_parallel_checks(self, monkeypatch, reset_updater_check):
        import threading

        calls = []
        started = threading.Event()
        release = threading.Event()

        def slow_fetch(session, *, timeout):
            calls.append(timeout)
            started.set()
            release.wait(timeout=2)
            return 200, {"tag_name": "v9.9.9", "body": "notes"}

        monkeypatch.setattr(updater, "__version__", "1.0.0")
        monkeypatch.setattr(updater, "_fetch_latest_release", slow_fetch)

        results = [None, None]

        def run(idx):
            results[idx] = updater.check_for_updates(force=True)

        t1 = threading.Thread(target=run, args=(0,))
        t2 = threading.Thread(target=run, args=(1,))
        t1.start()
        assert started.wait(timeout=2)
        t2.start()
        release.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
        assert calls == [updater.CHECK_TIMEOUT]
        assert results[0] is not None and results[1] is not None
        assert results[0][0] is True and results[1][0] is True
        assert results[0][1] == "9.9.9"

    def test_cache_skips_second_get(self, monkeypatch, reset_updater_check):
        calls = []

        def fake_fetch(session, *, timeout):
            calls.append(1)
            return 200, {"tag_name": "v1.0.0", "body": ""}

        monkeypatch.setattr(updater, "__version__", "1.0.0")
        monkeypatch.setattr(updater, "_fetch_latest_release", fake_fetch)
        first = updater.check_for_updates(force=False)
        second = updater.check_for_updates(force=False)
        assert first == second
        assert calls == [1]

    def test_force_bypasses_cache(self, monkeypatch, reset_updater_check):
        calls = []

        def fake_fetch(session, *, timeout):
            calls.append(1)
            return 200, {"tag_name": "v1.0.0", "body": ""}

        monkeypatch.setattr(updater, "__version__", "1.0.0")
        monkeypatch.setattr(updater, "_fetch_latest_release", fake_fetch)
        updater.check_for_updates(force=False)
        updater.check_for_updates(force=True)
        assert calls == [1, 1]

    def test_does_not_cache_failures(self, monkeypatch, reset_updater_check):
        calls = []

        def boom_fetch(session, *, timeout):
            calls.append(1)
            raise RuntimeError("offline")

        monkeypatch.setattr(updater, "_fetch_latest_release", boom_fetch)
        first = updater.check_for_updates(force=False)
        second = updater.check_for_updates(force=False)
        assert first[0] is False
        assert "Failed to check" in first[2]
        assert second[0] is False
        assert calls == [1, 1]

    def test_new_session_prefers_ipv4(self, monkeypatch):
        seen = {}

        class Sess:
            headers = {}

        def fake_create(*, ipv4=False, pool_size=None):
            seen["ipv4"] = ipv4
            return Sess()

        monkeypatch.setattr("core.parser.create_http_session", fake_create)
        session = updater._new_updater_session()
        assert seen["ipv4"] is True
        assert session.headers.get("User-Agent") == UPDATER_USER_AGENT

    def test_download_api_uses_safe_http_and_not_check_timeout(
        self, monkeypatch, reset_updater_check, tmp_path
    ):
        captured = {}

        class Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "tag_name": "v9.9.9",
                    "assets": [],
                }

        def fake_safe(session, method, url, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            captured["url"] = url
            captured["extra_check"] = kwargs.get("extra_check")
            return Resp()

        monkeypatch.setattr(updater, "safe_http_request", fake_safe)
        monkeypatch.setattr(updater, "_new_updater_session", lambda: object())
        monkeypatch.setattr(updater, "get_app_dir", lambda: tmp_path)
        monkeypatch.setattr(updater, "is_frozen", lambda: True)
        ok, msg = updater.download_update()
        assert ok is False
        assert "No prebuilt asset" in msg
        assert captured["url"] == updater.GITHUB_API_URL
        assert captured["timeout"] == updater.DOWNLOAD_API_TIMEOUT
        assert captured["extra_check"] is validate_github_asset_host


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
        assert "Unblock-File" in text
        assert "$pidWait" in text
        assert "PYINSTALLER_RESET_ENVIRONMENT" in text
        assert "_PYI_" in text
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
            assert "trap '' HUP" in text
            assert "os.spawnve" not in text
            assert "spawn_detached" in text
            assert "os.execvpe" in text
            # Bare Mach-O must not go through Launch Services (opens Terminal.app)
            assert "open -n" not in text
            assert "nohup" in text
            assert "python3 helper failed" in text
            assert "PYINSTALLER_RESET_ENVIRONMENT" in text
            assert '_PYI_' in text
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
            assert calls[-1][1].get("start_new_session") is True
            assert calls[-1][1].get("stdin") is updater.subprocess.DEVNULL
            env = calls[-1][1].get("env")
            assert env is not None
            assert "PYINSTALLER_RESET_ENVIRONMENT" not in env
            assert not any(k.startswith("_PYI_") for k in env)


    def test_windows_launch_uses_shellexecute(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater.sys, "platform", "win32")
        seen = []

        def fake_shell(file, params, cwd):
            seen.append((file, params, cwd))
            return 42

        monkeypatch.setattr(updater, "_win_shell_execute", fake_shell)
        monkeypatch.setattr(
            updater, "_windows_powershell",
            lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        )

        def boom(*a, **k):
            raise AssertionError("Popen must not run when ShellExecute succeeds")

        monkeypatch.setattr(updater.subprocess, "Popen", boom)
        script = tmp_path / "_update_helper.ps1"
        script.write_text("# ps1\n", encoding="utf-8")
        updater._win_start_ps1(script, cwd=str(tmp_path))
        assert seen
        file, params, cwd = seen[0]
        assert "powershell" in file.lower()
        assert "-File" in params
        assert str(script.resolve()) in params
        assert cwd == str(tmp_path)

    def test_windows_launch_falls_back_to_cmd_start(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater.sys, "platform", "win32")
        monkeypatch.setattr(updater, "_win_shell_execute", lambda *a, **k: 2)
        monkeypatch.setattr(
            updater, "_windows_powershell",
            lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        )
        monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
        calls = []

        def fake_popen(*args, **kwargs):
            calls.append((args, kwargs))
            class P:
                pass
            return P()

        monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
        script = tmp_path / "_update_helper.ps1"
        script.write_text("# ps1\n", encoding="utf-8")
        updater._win_start_ps1(script, cwd=str(tmp_path))
        assert calls
        argv = calls[0][0][0] if calls[0][0] else calls[0][1].get("args")
        assert argv[0].lower().endswith("cmd.exe")
        assert argv[1] == "/c"
        assert 'start ""' in argv[2]
        assert "-File" in argv[2]
        assert str(script.resolve()) in argv[2]
        flags = calls[0][1].get("creationflags", 0)
        detached = getattr(updater.subprocess, "DETACHED_PROCESS", 0x00000008)
        assert flags & detached
        assert calls[0][1].get("shell") in (False, None)
        env = calls[0][1].get("env")
        assert env is not None
        assert not any(k.startswith("_PYI_") for k in env)

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
        assert "ErrorAction Stop" in text
        assert "Unblock-File" in text
        assert "SSL_CERT_FILE" in text
        assert "PYINSTALLER_RESET_ENVIRONMENT" in text
        assert "_PYI_" in text
        cfg = (tmp_path / "_update_relaunch.json").read_text(encoding="utf-8")
        assert '"pid": 4242' in cfg
        assert '"args"' in cfg


class TestSourceRelaunch:
    def test_schedules_helper_instead_of_asking_for_manual_restart(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(updater, "get_app_dir", lambda: tmp_path)
        monkeypatch.setattr(updater, "is_frozen", lambda: False)
        monkeypatch.setattr(updater.os, "getpid", lambda: 4242)
        scheduled = []

        def fake_schedule():
            scheduled.append(True)

        monkeypatch.setattr(updater, "_schedule_relaunch_after_exit", fake_schedule)
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("# new\n", encoding="utf-8")
        ok, msg = updater._update_source_app(src, tmp_path)
        assert ok
        assert scheduled == [True]
        assert "reopen" in msg.lower()
        assert "please restart" not in msg.lower()


class TestSourceUpdateItems:
    def test_includes_gui(self):
        assert "gui" in SOURCE_UPDATE_ITEMS
        assert "core" in SOURCE_UPDATE_ITEMS
        assert "app.py" in SOURCE_UPDATE_ITEMS
        assert "build.py" in SOURCE_UPDATE_ITEMS


class TestRelaunchEnv:
    def test_strips_pyinstaller_ipc_and_marks_independent(self, monkeypatch):
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
        monkeypatch.setenv("_PYI_ARCHIVE_FILE", "/tmp/HuaEPUB")
        monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/tmp/_MEI123")
        monkeypatch.setenv("_MEIPASS2", "/tmp/_MEI123")
        monkeypatch.setenv("SSL_CERT_FILE", "/tmp/_MEI123/cacert.pem")
        monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI123")
        helper = updater._env_for_external_helper()
        assert "_PYI_ARCHIVE_FILE" not in helper
        assert "_PYI_APPLICATION_HOME_DIR" not in helper
        assert "_MEIPASS2" not in helper
        assert "SSL_CERT_FILE" not in helper
        assert "LD_LIBRARY_PATH" not in helper
        assert helper.get("PYINSTALLER_RESET_ENVIRONMENT") != "1"
        relaunch = updater._env_for_app_relaunch()
        assert relaunch.get("PYINSTALLER_RESET_ENVIRONMENT") == "1"
        assert "_PYI_ARCHIVE_FILE" not in relaunch
