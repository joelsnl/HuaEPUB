"""Tests for core.notify (Windows toast injection hardening)."""

from core import notify


class TestNotifyWindows:
    def test_payloads_are_base64_not_raw_interpolated(self, monkeypatch):
        calls = []

        def fake_popen(args, **kwargs):
            calls.append(args)
            class P:
                pass
            return P()

        monkeypatch.setattr(notify.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(notify.sys, "platform", "win32")

        evil_title = 'Hi"; $(calc.exe) #'
        evil_msg = "done $(whoami)"
        notify._notify_windows(evil_title, evil_msg)

        assert calls
        script = calls[0][calls[0].index("-Command") + 1]
        # Untrusted text must not appear raw in the PowerShell command
        assert "$(calc.exe)" not in script
        assert "$(whoami)" not in script
        assert "FromBase64String" in script
        assert "@'" in script  # literal here-string for XML skeleton
        assert notify._b64(evil_title) in script
        assert notify._b64(evil_msg) in script
